"""Text execution, Filter and target-response caching for final evaluation."""
import copy
import threading

from defipi.execution.api.clients import (
    build_text_client, extract_finish_reason, validate_generation_options,
    validate_scalar_client_settings,
)
from defipi.evaluation.llm_filter import (
    LLMHTMLFilter, FILTER_PROMPT_VARIANTS, normalize_filter_prompt_override,
    resolve_filter_prompt_profile, resolve_filter_prompt_variant,
)
from defipi.jsonio import content_key, open_store
from defipi.configuration.schema import Field, validate_config

def _usage_to_dict(usage):
    if not isinstance(usage, dict):
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


_UNSET = object()


class LLMEvaluator:
    def __init__(self, *, model, backend, temperature, client_settings,
                 max_retries, retry_backoff_seconds, max_response_tokens,
                 required_finish_reasons, reasoning_effort,
                 observation_format, enable_filter,
                 html_dir=None, scenario=None, cache_path=None,
                 observation_renderer=None, request_limiter=None, filter_stage=_UNSET,
                 filter_model=None, filter_backend=None, filter_temperature=_UNSET,
                 filter_client_settings=None, filter_prompt_variant=None,
                 filter_prompt_profile=None, filter_prompt_override=None,
                 filter_cache_path=None, filter_max_response_tokens=_UNSET,
                 filter_reasoning_effort=_UNSET):
        validate_generation_options(backend, temperature, max_response_tokens, reasoning_effort)
        validate_scalar_client_settings(client_settings, backend, temperature=temperature, reasoning_effort=reasoning_effort)
        validate_config({"max_retries": max_retries, "retry_backoff_seconds": retry_backoff_seconds,
                         "enable_filter": enable_filter,
                         "required_finish_reasons": list(required_finish_reasons) if type(required_finish_reasons) is tuple else required_finish_reasons}, {
            "max_retries": Field(int, minimum=1), "retry_backoff_seconds": Field((int, float), minimum=0),
            "enable_filter": Field(bool), "required_finish_reasons": Field(list, items=Field(str))})
        self.client_settings = client_settings
        self.filter_client_settings = filter_client_settings
        for name, value in (("filter_stage", filter_stage), ("filter_temperature", filter_temperature),
                            ("filter_max_response_tokens", filter_max_response_tokens),
                            ("filter_reasoning_effort", filter_reasoning_effort)):
            if enable_filter and value is _UNSET:
                raise ValueError(f"{name} is required when enable_filter=true")
        filter_stage = None if filter_stage is _UNSET else filter_stage
        filter_temperature = None if filter_temperature is _UNSET else filter_temperature
        filter_max_response_tokens = None if filter_max_response_tokens is _UNSET else filter_max_response_tokens
        filter_reasoning_effort = None if filter_reasoning_effort is _UNSET else filter_reasoning_effort
        if type(model) is not str or not model.strip():
            raise ValueError("model must be a nonempty string")
        self.model = model
        self.backend = backend
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.filter_reasoning_effort = filter_reasoning_effort
        self.request_limiter = request_limiter
        self.html_dir = html_dir
        if scenario is not None and scenario not in {"academic", "news"}:
            raise ValueError(f"Unsupported scenario: {scenario}")
        self.scenario = scenario
        self.cache_path = cache_path
        self.observation_format = observation_format
        self.observation_renderer = observation_renderer
        if observation_format not in {"html", "axtree"}:
            raise ValueError(f"Unsupported observation_format: {observation_format}")
        if observation_format == "axtree" and observation_renderer is None:
            raise ValueError("AXTree observations require a run-owned renderer")
        if enable_filter and filter_stage not in ("html_before_render", "ax_after_render"):
            raise ValueError(f"Unsupported filter_stage: {filter_stage}")
        if filter_stage == "ax_after_render" and observation_format != "axtree":
            raise ValueError("ax_after_render requires observation_format=axtree")
        self.filter_stage = filter_stage
        self.enable_filter = enable_filter
        self.filter_model = filter_model
        self.filter_backend = filter_backend
        self.filter_temperature = filter_temperature
        self.filter_prompt_override = filter_prompt_override
        self.filter_prompt_variant = filter_prompt_variant
        self.filter_prompt_profile = filter_prompt_profile
        if self.enable_filter:
            validate_generation_options(filter_backend, filter_temperature, filter_max_response_tokens, filter_reasoning_effort)
            validate_scalar_client_settings(filter_client_settings, filter_backend, temperature=filter_temperature, reasoning_effort=filter_reasoning_effort)
            if not filter_model:
                raise ValueError("filter_model is required when enable_filter=true")
            self.filter_prompt_override = normalize_filter_prompt_override(filter_prompt_override)
            if self.filter_prompt_override is None:
                self.filter_prompt_profile = resolve_filter_prompt_profile(filter_prompt_profile)
                if filter_prompt_variant not in FILTER_PROMPT_VARIANTS:
                    raise ValueError("filter_prompt_variant must be explicit")
        self.filter_cache_path = filter_cache_path
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_response_tokens = max_response_tokens
        self.filter_max_response_tokens = filter_max_response_tokens
        self.required_finish_reasons = tuple(required_finish_reasons)
        self.client = None
        self.filter = None
        self._cache = open_store(cache_path)
        self._state_lock = threading.RLock()
        self._init_client()
        self._init_filter()

    def _init_client(self):
        self.client = build_text_client(
            model=self.model,
            backend=self.backend,
            client_settings=self.client_settings,
            max_retries=self.max_retries,
            retry_backoff_seconds=self.retry_backoff_seconds,
            request_limiter=self.request_limiter,
        )

    def _init_filter(self):
        if not self.enable_filter:
            self.filter = None
            return
        self.filter = LLMHTMLFilter(
            client_settings=self.filter_client_settings,
            model=self.filter_model,
            backend=self.filter_backend,
            temperature=self.filter_temperature,
            prompt_variant=self.filter_prompt_variant,
            prompt_profile=self.filter_prompt_profile,
            prompt_override=self.filter_prompt_override,
            cache_path=self.filter_cache_path,
            max_retries=self.max_retries,
            retry_backoff_seconds=self.retry_backoff_seconds,
            max_response_tokens=self.filter_max_response_tokens,
            required_finish_reasons=self.required_finish_reasons,
            reasoning_effort=self.filter_reasoning_effort,
            request_limiter=self.request_limiter,
            input_format="axtree" if self.filter_stage == "ax_after_render" else "html",
        )

    def _resolved_filter_prompt_variant(self, html_content):
        if self.filter is not None:
            return self.filter.resolve_prompt_variant(html_content)
        return resolve_filter_prompt_variant(
            html_content,
            self.filter_prompt_variant,
        )

    def _cache_key(self, target_value, question, request=None, cache_context=None):
        payload = {
            "backend": getattr(self.client, "backend", self.backend),
            "model": self.model,
            "client_settings": self.client_settings,
            "temperature": self.temperature,
            # The evaluator reads HTML pages only; the key layout keeps existing target caches usable.
            "input_mode": "html",
            "question": question or "",
            "target": target_value or "",
            "request": request or {},
            "cache_context": cache_context or {},
        }
        return content_key(payload)

    def _build_html_request(self, html_content, question):
        prompt = (
            "Here is the HTML of a webpage. Answer the user's question based on that content.\n"
            f"HTML:\n{html_content}\n\n"
            f"Question: {question}\n"
            "Answer:"
        )
        return {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_response_tokens,
            **({"reasoning_effort": self.reasoning_effort} if self.reasoning_effort is not None else {}),
        }

    def _build_axtree_request(self, observation, question):
        return {
            "messages": [{"role": "user", "content": (
                "Here is the accessibility snapshot of a webpage. "
                "Answer the user's question based on that content.\n"
                f"{observation}\n\nQuestion: {question}\nAnswer:"
            )}],
            "temperature": self.temperature,
            "max_tokens": self.max_response_tokens,
            **({"reasoning_effort": self.reasoning_effort} if self.reasoning_effort is not None else {}),
        }

    def build_request(self, observation, question, *, observation_format):
        """Build a target request from an already prepared observation."""
        if observation_format == "html":
            return self._build_html_request(observation, question)
        if observation_format == "axtree":
            return self._build_axtree_request(observation, question)
        raise ValueError(f"Unsupported observation_format: {observation_format}")

    def execute(self, request):
        return self.client.generate(**{**request, "reasoning_effort": self.reasoning_effort})

    def _apply_filter(self, html_content, cache_namespace=None, *, user_task=""):
        if self.filter is None:
            return html_content, {
                "filter_enabled": False,
                "filter_prompt_variant": None,
                "filter_prompt_variant_requested": None,
                "filter_prompt_variant_resolved": None,
                "filter_prompt_variant_expected": None,
                "filter_prompt_profile": None,
                "filter_finish_reason": None,
                "filter_cache_hit": False,
                "filter_prompt_tokens": None,
                "filter_completion_tokens": None,
                "filter_total_tokens": None,
                "filter_error": None,
                "filter_raw_response": None,
            }
        if cache_namespace is None:
            result = self.filter.filter_html(html_content, user_task=user_task)
        else:
            result = self.filter.filter_html(html_content, cache_namespace=cache_namespace, user_task=user_task)
        sanitized_html = str(result.get("sanitized_html") or "")
        resolved_variant = result.get("prompt_variant")
        return sanitized_html, {
            "filter_enabled": True,
            "filter_prompt_variant": resolved_variant,
            "filter_prompt_variant_requested": (
                result.get("prompt_variant_requested")
                or self.filter_prompt_variant
            ),
            "filter_prompt_variant_resolved": resolved_variant,
            "filter_prompt_variant_expected": (
                self._resolved_filter_prompt_variant(html_content)
            ),
            "filter_prompt_profile": result.get("prompt_profile") or self.filter_prompt_profile,
            "filter_finish_reason": result.get("finish_reason"),
            "filter_cache_hit": bool(result.get("cache_hit")),
            "filter_prompt_tokens": result.get("prompt_tokens"),
            "filter_completion_tokens": result.get("completion_tokens"),
            "filter_total_tokens": result.get("total_tokens"),
            "filter_error": result.get("error"),
            "filter_raw_response": result.get("raw_response"),
        }

    def _normalize_response_payload(self, response):
        response = response or {}
        return {
            "text": str(response.get("text") or "").strip(),
            "usage": _usage_to_dict(response.get("usage")),
            "raw_response": copy.deepcopy(response.get("raw_response")),
            "finish_reason": extract_finish_reason(response),
            "error": str(response.get("error") or "").strip() or None,
        }

    def _cache_entry_usable(self, cached):
        if not isinstance(cached, dict):
            return False
        if not str(cached.get("answer") or "").strip():
            return False
        if (
            self.required_finish_reasons
            and cached.get("finish_reason") not in self.required_finish_reasons
        ):
            return False
        return True

    def _cache_response(self, cache_key, response):
        if not self.cache_path or not cache_key:
            return
        if response.get("error") or not str(response.get("text") or "").strip():
            return
        if (
            self.required_finish_reasons
            and response.get("finish_reason") not in self.required_finish_reasons
        ):
            return
        self._cache.put(cache_key, {
            "answer": response.get("text") or "",
            "usage": response.get("usage") or {},
            "raw_response": copy.deepcopy(response.get("raw_response")),
            "finish_reason": response.get("finish_reason"),
        })

    def get_filter_usage_summary(self):
        if self.filter is None:
            return None
        return self.filter.get_usage_summary()
