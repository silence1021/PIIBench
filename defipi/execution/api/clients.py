#!/usr/bin/env python3
"""
Provider abstraction for text generation across supported API backends.
"""

from abc import ABC, abstractmethod
from defipi.configuration.schema import Field, Problem, validate_config
import asyncio
from contextlib import nullcontext
import copy
import logging
import os
import random
import re
import threading
import time
from urllib.parse import urlparse


SUPPORTED_BACKENDS = {"openai", "anthropic", "deepseek", "openrouter"}
OPENAI_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
DEEPSEEK_REASONING_EFFORTS = ("none", "low", "high", "max")
OPENROUTER_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
REASONING_EFFORT_CHOICES = tuple(sorted(
    set(OPENAI_REASONING_EFFORTS) | set(DEEPSEEK_REASONING_EFFORTS) | set(OPENROUTER_REASONING_EFFORTS)))
logger = logging.getLogger(__name__)
# Natural completion of a plain-text Chat Completions or Claude Messages response.
COMPLETE_TEXT_FINISH_REASONS = ("stop", "end_turn")

_RATE_LIMIT_DELAY_RE = re.compile(
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|millisecond|milliseconds|s|sec|secs|second|seconds)\b",
    re.IGNORECASE,
)


CLIENT_SETTINGS_FIELDS = {
    "base_url": Field(str),
    "api_key_env": Field(str),
    "request_timeout_seconds": Field((int, float), nullable=True, minimum=0),
    "request_parameters": Field(dict),
    "omit_parameters": Field(list, items=Field(str)),
    "request_headers": Field(dict, values=Field(str)),
}
_WIRE_FIELDS = {
    "openai": {"reasoning", "reasoning_effort", "top_p", "frequency_penalty", "presence_penalty"},
    "anthropic": {"thinking", "output_config", "top_p", "top_k"},
    "deepseek": {"thinking", "output_config", "reasoning_effort", "top_p", "frequency_penalty", "presence_penalty"},
    "openrouter": {"temperature", "provider", "reasoning", "top_p", "top_k", "frequency_penalty", "presence_penalty"},
}
_OMIT_FIELDS = {"temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens",
                "max_output_tokens", "reasoning_effort", "reasoning", "thinking", "output_config",
                "frequency_penalty", "presence_penalty"}


def validate_client_settings(settings, backend, *, fields=CLIENT_SETTINGS_FIELDS):
    """Validate explicit transport and provider controls without loading resources."""
    infer_llm_backend(backend=backend)
    def rules(data, problems):
        for key in ("base_url", "api_key_env"):
            if type(data.get(key)) is str and not data[key].strip():
                problems.append(Problem(key, "must be nonempty"))
        base = data.get("base_url")
        if type(base) is str:
            parsed = urlparse(base)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                problems.append(Problem("base_url", "must be an absolute HTTP(S) URL without credentials"))
        timeout = data.get("request_timeout_seconds")
        if type(timeout) in (int, float) and timeout <= 0:
            problems.append(Problem("request_timeout_seconds", "must be positive or null"))
        extras = data.get("request_parameters")
        if type(extras) is dict:
            for key in extras:
                if key not in _WIRE_FIELDS[backend]:
                    problems.append(Problem(f"request_parameters.{key}", f"unsupported for backend={backend}"))
            nested = {
                "provider": {"only": Field(list, required=False, items=Field(str)),
                             "order": Field(list, required=False, items=Field(str)),
                             "allow_fallbacks": Field(bool, required=False),
                             "require_parameters": Field(bool, required=False)},
                "reasoning": {"effort": Field(str, required=False), "exclude": Field(bool, required=False),
                              "enabled": Field(bool, required=False), "max_tokens": Field(int, required=False, minimum=1)},
                "thinking": {"type": Field(str, choices=("adaptive", "enabled", "disabled")),
                             "budget_tokens": Field(int, required=False, minimum=1)},
                "output_config": {"effort": Field(str)},
            }
            for key, value in extras.items():
                if key in nested:
                    try:
                        validate_config(value, nested[key], prefix=f"request_parameters.{key}")
                    except ValueError as exc:
                        problems.extend(exc.problems)
                elif key == "reasoning_effort":
                    try:
                        validate_reasoning_effort(backend, value)
                    except ValueError as exc:
                        problems.append(Problem("request_parameters.reasoning_effort", str(exc)))
                elif key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
                    try:
                        validate_config({key: value}, {key: Field((int, float))}, prefix="request_parameters")
                    except ValueError as exc:
                        problems.extend(exc.problems)
                elif key == "top_k" and (type(value) is not int or value < 1):
                    problems.append(Problem("request_parameters.top_k", "must be a positive integer"))
            reasoning = extras.get("reasoning")
            if type(reasoning) is dict and "effort" in reasoning:
                try:
                    validate_reasoning_effort(backend, reasoning["effort"])
                except ValueError as exc:
                    problems.append(Problem("request_parameters.reasoning.effort", str(exc)))
        omitted = data.get("omit_parameters")
        if type(omitted) is list:
            if any(type(key) is str and key not in _OMIT_FIELDS for key in omitted):
                problems.append(Problem("omit_parameters", "contains unsupported request fields"))
            if len(set(map(str, omitted))) != len(omitted):
                problems.append(Problem("omit_parameters", "must contain unique fields"))
            if type(extras) is dict and set(extras) & set(map(str, omitted)):
                problems.append(Problem("omit_parameters", "cannot omit configured request_parameters"))
        headers = data.get("request_headers")
        if type(headers) is dict:
            for key, value in headers.items():
                if type(key) is str and (not key.strip() or key.lower() in ("authorization", "x-api-key", "host", "content-length")):
                    problems.append(Problem(f"request_headers.{key}", "invalid header or credentials must use api_key_env"))
                if type(value) is str and ("\n" in value or "\r" in value):
                    problems.append(Problem(f"request_headers.{key}", "must not contain newlines"))
    return validate_config(settings, fields, rules=(rules,))


def validate_scalar_client_settings(settings, backend, *, temperature, reasoning_effort):
    """Reject two configuration sources for one scalar generation control."""
    validate_client_settings(settings, backend)
    extra = settings["request_parameters"]
    duplicates = []
    if temperature is not None and "temperature" in extra:
        duplicates.append("temperature")
    if reasoning_effort is not None:
        if "reasoning_effort" in extra:
            duplicates.append("reasoning_effort")
        if type(extra.get("reasoning")) is dict and "effort" in extra["reasoning"]:
            duplicates.append("reasoning.effort")
    if duplicates:
        from defipi.configuration.schema import ValidationError
        raise ValidationError([Problem(f"request_parameters.{key}", "duplicates the role generation control") for key in duplicates])
    return settings


_UNSET = object()


def validate_generation_options(backend, temperature, max_tokens, reasoning_effort=_UNSET):
    if reasoning_effort is _UNSET:
        if backend != "anthropic":
            raise ValueError("reasoning_effort must be explicitly supplied for this backend")
        reasoning_effort = None
    validate_config({"temperature": temperature, "max_tokens": max_tokens}, {
        "temperature": Field((int, float), nullable=True, minimum=0, maximum=2),
        "max_tokens": Field(int, nullable=True, minimum=1)})
    validate_reasoning_effort(backend, reasoning_effort)


def apply_client_settings(request, settings):
    request = dict(request)
    extra = copy.deepcopy(settings["request_parameters"])
    duplicate = set(request) & set(extra)
    if duplicate:
        raise ValueError("Duplicate request controls: " + ", ".join(sorted(duplicate)))
    for key in settings["omit_parameters"]:
        request.pop(key, None)
    if extra:
        request["extra_body"] = extra
    if settings["request_headers"]:
        request["extra_headers"] = dict(settings["request_headers"])
    return request


def _sdk_options(settings, backend):
    validate_client_settings(settings, backend)
    key = os.environ.get(settings["api_key_env"])
    if not key:
        raise RuntimeError(f"{settings['api_key_env']} is not set")
    options = dict(api_key=key, base_url=settings["base_url"],
                   timeout=settings["request_timeout_seconds"], max_retries=0)
    if backend == "anthropic":
        options["auth_token"] = ""
    else:
        # Empty strings suppress constructor environment lookups. Clear these
        # attributes after construction so absent organization/project headers
        # are omitted, rather than transmitted as empty values.
        options.update(organization="", project="")
    return options


def _clear_implicit_headers(client, backend):
    if backend == "anthropic":
        client.auth_token = None
    else:
        client.organization = None
        client.project = None
    return client


def build_sdk_client(settings, backend):
    """Construct the explicit synchronous SDK transport used by MUZZLE."""
    options = _sdk_options(settings, backend)
    if backend == "anthropic":
        from anthropic import Anthropic
        return _clear_implicit_headers(Anthropic(**options), backend)
    from openai import OpenAI
    return _clear_implicit_headers(OpenAI(**options), backend)


def infer_llm_backend(backend, *, model=None):
    token = backend
    if token is None or token == "":
        raise ValueError(
            f"LLM backend is required for model={model!r}. "
            "Pass an explicit backend such as openai, anthropic, deepseek, or openrouter."
        )
    if type(token) is not str or token not in SUPPORTED_BACKENDS:
        raise ValueError(f"Unsupported LLM backend: {backend}")
    return token


def validate_reasoning_effort(backend, effort, *, role="LLM"):
    """Validate explicit reasoning controls supported by the selected provider."""
    if effort is None:
        return
    if backend == "openai" and effort in OPENAI_REASONING_EFFORTS:
        return
    if backend == "deepseek" and effort in DEEPSEEK_REASONING_EFFORTS:
        return
    if backend == "openrouter" and effort in OPENROUTER_REASONING_EFFORTS:
        return
    if backend == "openai":
        allowed = ", ".join(OPENAI_REASONING_EFFORTS)
    elif backend == "deepseek":
        allowed = ", ".join(DEEPSEEK_REASONING_EFFORTS)
    elif backend == "openrouter":
        allowed = ", ".join(OPENROUTER_REASONING_EFFORTS)
    else:
        allowed = "none"
    raise ValueError(f"{role} reasoning_effort is unsupported for backend={backend}; allowed: {allowed}")


def _normalize_usage(prompt_tokens=None, completion_tokens=None, total_tokens=None):
    prompt = int(prompt_tokens) if isinstance(prompt_tokens, int) else None
    completion = int(completion_tokens) if isinstance(completion_tokens, int) else None
    total = int(total_tokens) if isinstance(total_tokens, int) else None
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _extract_anthropic_text(content_blocks):
    if isinstance(content_blocks, str):
        return content_blocks.strip()
    pieces = []
    for block in content_blocks or []:
        if isinstance(block, dict):
            block_type = str(block.get("type") or "").strip().lower()
            text = block.get("text")
        else:
            block_type = str(getattr(block, "type", "") or "").strip().lower()
            text = getattr(block, "text", None)
        if block_type == "text" and isinstance(text, str) and text.strip():
            pieces.append(text.strip())
    return "\n".join(pieces).strip()


def _extract_anthropic_web_urls(content_blocks):
    urls = []
    for block in content_blocks or []:
        if isinstance(block, dict):
            block_type = str(block.get("type") or "").strip().lower()
            content = block.get("content")
        else:
            block_type = str(getattr(block, "type", "") or "").strip().lower()
            content = getattr(block, "content", None)

        if block_type == "web_fetch_tool_result":
            if isinstance(content, dict):
                url = content.get("url")
            else:
                url = getattr(content, "url", None)
            if isinstance(url, str) and url.strip():
                urls.append(url.strip())

        if block_type == "web_search_tool_result":
            if isinstance(content, dict):
                result_blocks = content.get("results") or []
            else:
                result_blocks = getattr(content, "results", None) or []
            for result in result_blocks:
                if isinstance(result, dict):
                    url = result.get("url")
                else:
                    url = getattr(result, "url", None)
                if isinstance(url, str) and url.strip():
                    urls.append(url.strip())

        citations = None
        if isinstance(block, dict):
            citations = block.get("citations")
        else:
            citations = getattr(block, "citations", None)
        for citation in citations or []:
            if isinstance(citation, dict):
                cited_url = citation.get("url")
            else:
                cited_url = getattr(citation, "url", None)
            if isinstance(cited_url, str) and cited_url.strip():
                urls.append(cited_url.strip())

    deduped = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def _coerce_retry_delay_seconds(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        delay = float(value)
        return delay if delay >= 0.0 else None

    text = str(value or "").strip()
    if not text:
        return None

    lowered = text.lower()
    try:
        if lowered.endswith("ms"):
            delay = float(lowered[:-2].strip()) / 1000.0
            return delay if delay >= 0.0 else None
        delay = float(lowered)
        return delay if delay >= 0.0 else None
    except ValueError:
        return None


def _header_lookup(headers, name):
    if headers is None:
        return None
    header_name = str(name or "").strip().lower()
    if not header_name:
        return None

    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key or "").strip().lower() == header_name:
                return value
        return None

    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return value
        value = getter(header_name)
        if value is not None:
            return value

    items = getattr(headers, "items", None)
    if callable(items):
        try:
            for key, value in items():
                if str(key or "").strip().lower() == header_name:
                    return value
        except Exception:
            return None
    return None


def _extract_retry_delay_seconds(exc):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    for header_name in ("retry-after-ms", "x-ratelimit-reset-requests-ms", "x-ratelimit-reset-tokens-ms"):
        raw_value = _header_lookup(headers, header_name)
        delay = _coerce_retry_delay_seconds(raw_value)
        if delay is not None:
            if isinstance(raw_value, str) and raw_value.strip().lower().endswith("ms"):
                return delay
            return delay / 1000.0
    delay = _coerce_retry_delay_seconds(_header_lookup(headers, "retry-after"))
    if delay is not None:
        return delay

    text = str(exc or "")
    match = _RATE_LIMIT_DELAY_RE.search(text)
    if not match:
        return None
    magnitude = float(match.group(1))
    unit = str(match.group(2) or "").strip().lower()
    if unit.startswith("ms") or unit.startswith("millisecond"):
        return magnitude / 1000.0
    return magnitude


def _is_rate_limit_error(exc):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code == 429:
        return True
    class_name = exc.__class__.__name__.lower()
    if "ratelimit" in class_name or "rate_limit" in class_name:
        return True
    text = str(exc or "").lower()
    return "rate limit" in text or "429" in text and "limit" in text


def _as_plain_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    return None


def _extract_raw_response_payload(resp):
    payload = _as_plain_dict(resp)
    if payload is not None:
        return payload
    return {"repr": repr(resp)}


def extract_finish_reason(response):
    """Return the provider completion reason from a normalized response."""
    response = response or {}
    direct = response.get("finish_reason")
    if direct:
        return str(direct)
    raw_response = response.get("raw_response")
    if not isinstance(raw_response, dict):
        return None
    choices = raw_response.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict) and choice.get("finish_reason"):
            return str(choice["finish_reason"])
    incomplete = raw_response.get("incomplete_details")
    if isinstance(incomplete, dict) and incomplete.get("reason"):
        return str(incomplete["reason"])
    stop_reason = raw_response.get("stop_reason")
    if stop_reason:
        return str(stop_reason)
    status = raw_response.get("status")
    return str(status) if status else None


def _extract_openai_chat_text(resp):
    payload = _as_plain_dict(resp) or {}
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    chunks = []
    for block in content or []:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


def _extract_openai_chat_usage(resp):
    payload = _as_plain_dict(resp) or {}
    usage = payload.get("usage") or {}
    return _normalize_usage(
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
    )


class RequestDeadlineExceeded(TimeoutError):
    """A single network attempt exceeded its total elapsed-time limit."""


class BaseTextClient(ABC):
    def __init__(self, model, backend, *, client_settings, max_retries, retry_backoff_seconds, request_limiter=None):
        self.model = model
        self.backend = backend
        self.request_limiter = request_limiter
        validate_config({"model": model, "max_retries": max_retries, "retry_backoff_seconds": retry_backoff_seconds}, {
            "model": Field(str), "max_retries": Field(int, minimum=1),
            "retry_backoff_seconds": Field((int, float), minimum=0)})
        if not model.strip():
            raise ValueError("model must be nonempty")
        self.client_settings = copy.deepcopy(validate_client_settings(client_settings, backend))
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.request_timeout_seconds = client_settings["request_timeout_seconds"]

    def _request_with_deadline(self, method_path, request, *, sdk_timeout_seconds=None):
        # Each attempt owns its event loop and HTTP client. Cancellation closes
        # its connection before returning/releasing the shared request slot;
        # no timed-out background thread keeps using the provider.
        async def run():
            async with self._async_client_class(**self._async_client_options) as client:
                _clear_implicit_headers(client, self.backend)
                method = client
                for part in method_path.split("."):
                    method = getattr(method, part)
                method_kwargs = dict(request)
                if sdk_timeout_seconds is not None:
                    method_kwargs["timeout"] = sdk_timeout_seconds
                try:
                    return await asyncio.wait_for(method(**method_kwargs), self.request_timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise RequestDeadlineExceeded(
                        f"{self.backend} model={self.model} exceeded "
                        f"{self.request_timeout_seconds}s hard request timeout"
                    ) from exc

        return asyncio.run(run())

    def _with_retries(self, fn, *, before_request_attempt=None):
        last_exc = None
        for attempt in range(1, self.max_retries + 1):
            if before_request_attempt is not None:
                before_request_attempt()
            try:
                with self.request_limiter if self.request_limiter is not None else nullcontext():
                    return fn()
            except Exception as exc:
                last_exc = exc
                logger.warning("%s model=%s request attempt %d/%d failed: %s: %s",
                               self.backend, self.model, attempt, self.max_retries,
                               type(exc).__name__, exc)
                if attempt >= self.max_retries:
                    break
                delay_seconds = 0.0
                if _is_rate_limit_error(exc):
                    suggested_delay = _extract_retry_delay_seconds(exc) or 0.0
                    base_delay = self.retry_backoff_seconds
                    delay_seconds = max(
                        suggested_delay,
                        base_delay * (2 ** (attempt - 1)),
                    )
                    delay_seconds += random.uniform(
                        0.0,
                        min(1.0, max(0.1, delay_seconds * 0.25)),
                    )
                elif self.retry_backoff_seconds > 0:
                    delay_seconds = self.retry_backoff_seconds * attempt
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
        raise RuntimeError(
            f"{self.backend} request failed after {self.max_retries} attempts: "
            f"{type(last_exc).__name__}: {last_exc}"
        ) from last_exc

    def _apply_settings(self, request):
        return apply_client_settings(request, self.client_settings)

    @abstractmethod
    def generate(
        self,
        *,
        messages,
        system=None,
        temperature,
        max_tokens,
        before_request_attempt=None,
    ):
        raise NotImplementedError


class OpenAITextClient(BaseTextClient):
    def __init__(self, model, *, client_settings, max_retries, retry_backoff_seconds, request_limiter=None):
        super().__init__(
            model=model,
            backend="openai",
            client_settings=client_settings,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            request_limiter=request_limiter,
        )
        try:
            from openai import AsyncOpenAI, OpenAI  # type: ignore
        except Exception as exc:
            raise RuntimeError("openai package is required for backend=openai") from exc
        self.client = build_sdk_client(self.client_settings, self.backend)
        self._async_client_class = AsyncOpenAI
        self._async_client_options = _sdk_options(self.client_settings, self.backend)

    def _build_request(self, *, messages, system=None, temperature, max_tokens,
                       reasoning_effort):
        validate_generation_options("openai", temperature, max_tokens, reasoning_effort)
        payload = list(messages or [])
        if system:
            payload = [{"role": "system", "content": system}] + payload
        request = {
            "model": self.model,
            "messages": payload,
        }
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort
        if temperature is not None and reasoning_effort in (None, "none"):
            request["temperature"] = temperature
        if max_tokens is not None:
            request["max_completion_tokens"] = max_tokens
        return self._apply_settings(request)

    def _parse_response(self, resp):
        usage = getattr(resp, "usage", None)
        return {
            "text": str(resp.choices[0].message.content or "").strip(),
            "usage": _normalize_usage(
                prompt_tokens=getattr(usage, "prompt_tokens", None) if usage is not None else None,
                completion_tokens=getattr(usage, "completion_tokens", None) if usage is not None else None,
                total_tokens=getattr(usage, "total_tokens", None) if usage is not None else None,
            ),
            "sources": [],
            "web_actions": [],
            "raw_response": _extract_raw_response_payload(resp),
        }

    def generate(
        self,
        *,
        messages,
        system=None,
        temperature,
        max_tokens,
        before_request_attempt=None,
        reasoning_effort,
    ):
        validate_generation_options("openai", temperature, max_tokens, reasoning_effort)
        request = self._build_request(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

        def _request():
            resp = self._request_with_deadline("chat.completions.create", request)
            return self._parse_response(resp)

        return self._with_retries(
            _request,
            before_request_attempt=before_request_attempt,
        )


class _OpenAICompatibleTextClient(BaseTextClient):
    """Shared Chat Completions transport for DeepSeek and OpenRouter."""

    def __init__(self, model, *, client_settings, max_retries, retry_backoff_seconds, request_limiter=None):
        super().__init__(
            model=model,
            backend=self.backend_name,
            client_settings=client_settings,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            request_limiter=request_limiter,
        )
        try:
            from openai import AsyncOpenAI, OpenAI  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"openai package is required for backend={self.backend}") from exc
        self.client = build_sdk_client(self.client_settings, self.backend)
        self._async_client_class = AsyncOpenAI
        self._async_client_options = _sdk_options(self.client_settings, self.backend)

    def _build_request(self, *, messages, system=None, temperature, max_tokens,
                       reasoning_effort):
        validate_generation_options(self.backend, temperature, max_tokens, reasoning_effort)
        payload = list(messages or [])
        if system:
            payload = [{"role": "system", "content": system}] + payload
        request = {
            "model": self.model,
            "messages": payload,
        }
        if temperature is not None:
            request["temperature"] = temperature
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            if self.backend == "openrouter":
                request["reasoning"] = {"effort": reasoning_effort}
            else:
                request["reasoning_effort"] = reasoning_effort
        # OpenRouter reasoning is an SDK extension, together with routing.
        if self.backend == "openrouter" and "reasoning" in request:
            effort = request.pop("reasoning")
            configured = copy.deepcopy(self.client_settings["request_parameters"])
            reasoning = configured.get("reasoning", {})
            if "effort" in reasoning:
                raise ValueError("reasoning effort must have only one configured source")
            configured["reasoning"] = {**reasoning, **effort}
            result = self._apply_settings(request)
            result["extra_body"] = configured
            return result
        return self._apply_settings(request)

    def generate(
        self,
        *,
        messages,
        system=None,
        temperature,
        max_tokens,
        before_request_attempt=None,
        reasoning_effort,
    ):
        validate_generation_options(self.backend, temperature, max_tokens, reasoning_effort)

        request = self._build_request(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

        def _request():
            resp = self._request_with_deadline("chat.completions.create", request)
            return {
                "text": _extract_openai_chat_text(resp),
                "usage": _extract_openai_chat_usage(resp),
                "sources": [],
                "web_actions": [],
                "raw_response": _extract_raw_response_payload(resp),
            }

        return self._with_retries(
            _request,
            before_request_attempt=before_request_attempt,
        )


class DeepSeekTextClient(_OpenAICompatibleTextClient):
    backend_name = "deepseek"


class OpenRouterTextClient(_OpenAICompatibleTextClient):
    backend_name = "openrouter"


class AnthropicTextClient(BaseTextClient):
    def __init__(self, model, *, client_settings, max_retries, retry_backoff_seconds, request_limiter=None):
        super().__init__(
            model=model,
            backend="anthropic",
            client_settings=client_settings,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            request_limiter=request_limiter,
        )
        try:
            from anthropic import AsyncAnthropic, Anthropic  # type: ignore
        except Exception as exc:
            raise RuntimeError("anthropic package is required for backend=anthropic") from exc
        self.client = build_sdk_client(self.client_settings, self.backend)
        self._async_client_class = AsyncAnthropic
        self._async_client_options = _sdk_options(self.client_settings, self.backend)
        self.metadata_request_attempts = 0
        self._model_limit_lock = threading.Lock()
        self._model_token_limit = None

    def _model_max_tokens(self):
        """Resolve and cache the provider-declared required Messages output limit."""
        with self._model_limit_lock:
            if self._model_token_limit is not None:
                return self._model_token_limit
            if not hasattr(self._async_client_class, "models"):
                raise RuntimeError(
                    "Anthropic SDK lacks Models.retrieve; cannot resolve the required "
                    "Messages max_tokens limit without an explicit max_tokens override"
                )

            def _metadata_attempt():
                self.metadata_request_attempts += 1

            info = self._with_retries(
                lambda: self._request_with_deadline(
                    "models.retrieve", {"model_id": self.model}
                ),
                before_request_attempt=_metadata_attempt,
            )
            value = (info.get("max_tokens") if isinstance(info, dict)
                     else getattr(info, "max_tokens", None))
            if type(value) is not int or value <= 0:
                raise RuntimeError(
                    f"Anthropic Models.retrieve returned no valid max_tokens limit for model={self.model!r}; "
                    "provide an explicit max_tokens override"
                )
            self._model_token_limit = value
            return value

    def _build_request(self, *, messages, system=None, temperature, max_tokens,
                       thinking=None,
                       output_config=None):
        validate_generation_options("anthropic", temperature, max_tokens)
        request = {
            "model": self.model,
            "messages": list(messages or []),
        }
        if temperature is not None:
            request["temperature"] = temperature
        if max_tokens is None:
            raise ValueError(
                "Anthropic Messages requires max_tokens; resolve the model limit or provide an explicit override"
            )
        request["max_tokens"] = max_tokens
        if thinking is not None:
            request["thinking"] = dict(thinking)
        if output_config is not None:
            request["output_config"] = dict(output_config)
        if system:
            request["system"] = system
        return self._apply_settings(request)

    def generate(
        self,
        *,
        messages,
        system=None,
        temperature,
        max_tokens,
        before_request_attempt=None,
        thinking=None,
        output_config=None,
        reasoning_effort=None,
    ):
        validate_reasoning_effort("anthropic", reasoning_effort)
        resolved_max_tokens = max_tokens
        if resolved_max_tokens is None:
            resolved_max_tokens = self._model_max_tokens()

        request = self._build_request(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=resolved_max_tokens,
            thinking=thinking,
            output_config=output_config,
        )

        def _request():
            resp = self._request_with_deadline(
                "messages.create",
                request,
                sdk_timeout_seconds=self.request_timeout_seconds,
            )
            usage = getattr(resp, "usage", None)
            return {
                "text": _extract_anthropic_text(getattr(resp, "content", None)),
                "usage": _normalize_usage(
                    prompt_tokens=getattr(usage, "input_tokens", None) if usage is not None else None,
                    completion_tokens=getattr(usage, "output_tokens", None) if usage is not None else None,
                    total_tokens=None,
                ),
                "sources": _extract_anthropic_web_urls(getattr(resp, "content", None)),
                "web_actions": [],
                "raw_response": _extract_raw_response_payload(resp),
            }

        return self._with_retries(
            _request,
            before_request_attempt=before_request_attempt,
        )


def build_text_client(model, backend, *, client_settings, max_retries, retry_backoff_seconds, request_limiter=None):
    resolved_backend = infer_llm_backend(model=model, backend=backend)
    if resolved_backend == "openai":
        return OpenAITextClient(
            model=model,
            client_settings=client_settings,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            request_limiter=request_limiter,
        )
    if resolved_backend == "anthropic":
        return AnthropicTextClient(
            model=model,
            client_settings=client_settings,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            request_limiter=request_limiter,
        )
    if resolved_backend == "deepseek":
        return DeepSeekTextClient(
            model=model,
            client_settings=client_settings,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            request_limiter=request_limiter,
        )
    if resolved_backend == "openrouter":
        return OpenRouterTextClient(
            model=model,
            client_settings=client_settings,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            request_limiter=request_limiter,
        )
    raise ValueError(f"Unsupported LLM backend: {resolved_backend}")


def api_refusal(response):
    """Read explicit provider refusal markers, never infer refusal from prose."""
    detected = extract_finish_reason(response) == "refusal"
    texts = []

    def inspect_message(message):
        nonlocal detected
        if not isinstance(message, dict):
            return
        refusal = message.get("refusal")
        if refusal is True or (isinstance(refusal, str) and refusal.strip()):
            detected = True
            if isinstance(refusal, str):
                texts.append(refusal.strip())
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "refusal":
                    detected = True
                    text = block.get("refusal") or block.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())

    inspect_message(response)
    raw = response.get("raw_response")
    if isinstance(raw, dict):
        inspect_message(raw)
        choices = raw.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            inspect_message(choices[0].get("message"))
        output = raw.get("output")
        if isinstance(output, list):
            for message in output:
                if isinstance(message, dict) and message.get("type") == "message":
                    inspect_message(message)
    return {"detected": detected, "texts": list(dict.fromkeys(texts))}
