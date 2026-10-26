"""OpenRouter URL evaluation with the official server-side web_fetch."""
from __future__ import annotations

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit

import httpx

from defipi.configuration.execution import validate_execution_request
from defipi.jsonio import write_json

VERSION = "openrouter-web-fetch-v12-optional-provider-routing"
ROOT = _PACKAGE_ROOT
DSML = re.compile(r"<[｜|]*DSML[｜|]*\s+(?:calls|invoke)\b")
FETCH_NAMES = {"openrouter:web_fetch", "web_fetch", "openrouter_web_fetch"}
# Verified against OpenRouter's model endpoint inventory on 2026-10-23.
# Exact routing IDs only; public response model and provider remain independently checked.
ROUTING_MODEL_ALIASES = {
    "deepseek/deepseek-v4.1-flash": ("deepseek/deepseek-v4.1-flash-20260910",),
}


def model_parameters(request):
    parameters = request["target"]["client_settings"]["request_parameters"]
    if "provider" in parameters:
        provider = parameters["provider"]
        selected = provider.get("only")
        if (not isinstance(selected, list) or len(selected) != 1
                or not isinstance(selected[0], str) or not selected[0].strip()):
            raise ValueError("OpenRouter URL evaluation requires one explicit provider")
        if provider.get("allow_fallbacks") is not False:
            raise ValueError("OpenRouter URL evaluation requires allow_fallbacks=false")
    name = request["tool_config"]["provider_name"]
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError("OpenRouter URL evaluation requires a non-empty provider_name or null")
    return parameters


def normalized_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An absolute HTTP(S) page URL without credentials is required")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                       parsed.path.rstrip("/") or "/", parsed.query, ""))


def build_request(request):
    validate_execution_request(request)
    if request.get("schema_version") != "final-eval-adapter-v1":
        raise ValueError("Unsupported OpenRouter URL adapter protocol")
    axes, target = request["experiment"], request["target"]
    if (axes.get("mode") != "api" or axes.get("harness_type") is not None
            or axes.get("web_injection_channel") != "url"
            or request["input"]["kind"] != "url"):
        raise ValueError("OpenRouter URL adapter requires API/URL with no harness")
    if target["backend"] != "openrouter" or target["api"] != "openai-completions":
        raise ValueError("OpenRouter URL requires the explicit OpenRouter Chat Completions transport")
    normalized_url(request["input"]["content"])
    if axes.get("attack_method") not in ("off", "url_prompt"):
        raise ValueError("OpenRouter URL adapter supports OFF and url_prompt")
    cap = request["tool_config"]["max_tool_calls"]
    if type(cap) is not int or not 1 <= cap <= 20:
        raise ValueError("web_fetch limit must be between 1 and 20")
    parameters = model_parameters(request)
    messages = []
    if axes["attack_method"] == "url_prompt":
        defense = request.get("url_prompt_defense")
        if (not isinstance(defense, dict) or defense.get("placement") != "system"
                or not isinstance(defense.get("prompt"), str) or not defense["prompt"].strip()):
            raise ValueError("URL defensive prompt requires non-empty prompt and placement=system")
        messages.append({"role": "system", "content": defense["prompt"]})
    messages.append({"role": "user", "content": request["question"] +
                     "\n\nPage URL: " + request["input"]["content"]})
    # The platform owns the whole tool loop. There is no client DSH invocation
    # and no second client request that prematurely forces tool_choice=none.
    body = {
        "model": target["model"],
        **parameters,
        "messages": messages,
        "tools": [{"type": "openrouter:web_fetch",
                   "parameters": {"engine": "openrouter", "max_uses": cap}}],
        "max_tool_calls": cap,
        "tool_choice": "auto",
        "stream": False,
    }
    if request["tool_config"]["capture_upstream_debug"]:
        body.update(stream=True, debug={"echo_upstream_body": True})
    return body


def json_object(value):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def is_rate_limited(raw):
    """Inspect provider error objects, never page content or answer text."""
    errors = [raw.get("error"), *(raw.get("stream_errors") or [])]
    for error in errors:
        if not isinstance(error, dict):
            continue
        metadata = error.get("metadata") or {}
        if str(error.get("code")) == "429" or (
                isinstance(metadata, dict) and metadata.get("error_type") == "rate_limit_exceeded"):
            return True
    return False


def retry_after_seconds(value, now=None):
    """Accept Retry-After delay seconds or an HTTP date; invalid hints are absent."""
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, (deadline - (now or datetime.now(timezone.utc))).total_seconds())
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def response_metadata(result):
    received_at = datetime.now(timezone.utc)
    headers = {key.lower(): value for key, value in result.headers.items()
               if key.lower() in {"x-generation-id", "x-request-id", "content-type", "date", "retry-after"}
               or key.lower().startswith("x-ratelimit-")}
    delay = retry_after_seconds(headers.get("retry-after"), received_at)
    return {"status_code": result.status_code, "headers": headers,
            "received_at": received_at.isoformat(),
            "retry_after_at": (received_at + timedelta(seconds=delay)).isoformat() if delay is not None else None}


def tool_trace(message):
    trace = []
    for item in message.get("reasoning_details") or []:
        if not isinstance(item, dict) or item.get("type") != "reasoning.server_tool_call":
            continue
        arguments, output = json_object(item.get("arguments")), json_object(item.get("result"))
        status = output.get("status") if output else None
        http_status = output.get("httpStatus", output.get("http_status")) if output else None
        failed = (status != "completed" or bool(output.get("error"))
                  or (http_status is not None and not (type(http_status) is int and 200 <= http_status < 300))) if output else True
        trace.append({"type": "tool", "name": item.get("tool_name"),
                      "call_id": item.get("tool_call_id") or item.get("id"),
                      "input": arguments, "output": output, "is_error": failed,
                      "raw_arguments": item.get("arguments"), "raw_result": item.get("result"),
                      "evidence_source": "message.reasoning_details.reasoning.server_tool_call"})
    return trace


def parse_stream(text):
    """Retain echoed upstream requests and score only the final model turn."""
    raw = {"choices": [{"message": {"role": "assistant", "content": ""},
                        "finish_reason": None}], "upstream_debug_bodies": [],
           "stream_finished": False, "stream_errors": []}
    content, all_content = [], []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            raw["stream_finished"] = True
            continue
        event = json_object(payload)
        if event is None:
            continue
        if isinstance(event.get("error"), dict):
            raw["stream_errors"].append(event["error"])
        for key in ("id", "model", "provider", "usage", "openrouter_metadata", "error"):
            if event.get(key) is not None:
                raw[key] = event[key]
        body = (event.get("debug") or {}).get("echo_upstream_body")
        if isinstance(body, str):
            body = json_object(body)
        if isinstance(body, dict):
            raw["upstream_debug_bodies"].append(body)
            content = []
            raw["choices"][0]["finish_reason"] = None
        for choice in event.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
                all_content.append(delta["content"])
            if choice.get("finish_reason") is not None:
                raw["choices"][0]["finish_reason"] = choice["finish_reason"]
            if delta.get("tool_calls"):
                raw["choices"][0]["message"]["tool_calls"] = delta["tool_calls"]
        # A new echoed request starts a later turn, so earlier tool calls are
        # already executed and must not be mistaken for an unfinished final turn.
        if isinstance(body, dict):
            raw["choices"][0]["message"].pop("tool_calls", None)
    raw["choices"][0]["message"]["content"] = "".join(content)
    raw["streamed_content_all_turns"] = "".join(all_content)
    return raw


def upstream_tool_trace(bodies):
    """Recover tool messages actually sent upstream, without reading reasoning."""
    trace, seen = [], set()
    for index, body in enumerate(bodies):
        calls = {}
        for message in body.get("messages") or []:
            if message.get("role") == "assistant":
                calls.update({c["id"]: c for c in message.get("tool_calls") or [] if c.get("id")})
            elif message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                call = calls.get(call_id, {}).get("function", {})
                result = message.get("content")
                if not call or not isinstance(result, str) or (call_id, result) in seen:
                    continue
                seen.add((call_id, result))
                record = {"type": "reasoning.server_tool_call", "tool_name": call.get("name"),
                          "tool_call_id": call_id, "arguments": call.get("arguments"), "result": result}
                tool = tool_trace({"reasoning_details": [record]})[0]
                tool.update(evidence_source="debug.echo_upstream_body.messages[role=tool]",
                            upstream_debug_index=index)
                trace.append(tool)
    return trace


def target_url_evidence(trace, url):
    expected = normalized_url(url)
    for tool in trace:
        if tool.get("name") not in FETCH_NAMES or tool.get("is_error"):
            continue
        output = tool.get("output")
        if (not isinstance(output, dict) or output.get("status") != "completed"
                or not isinstance(output.get("content"), str) or not output["content"].strip()):
            continue
        try:
            matches = normalized_url(output.get("url", "")) == expected
        except (ValueError, TypeError, AttributeError):
            matches = False
        if matches:
            return {"type": "openrouter_web_fetch_result", "url": output["url"],
                    "tool_use_id": tool.get("call_id")}
    return None


def tool_input_audit_status(texts):
    if any(re.fullmatch(r"<redacted [^<>\n]+ from debug output>", text) for text in texts):
        return "unknown_redacted"
    return "known" if texts else "unknown"


def response_base(request):
    return {"schema_version": "final-eval-adapter-v1", "adapter_version": VERSION,
            "model": request["target"]["model"], "harness_type": None,
            "status": "unresolved", "final_answer": "", "trace": [], "usage": {},
            "model_requests": 1, "client_inference_requests": 1,
            "provider_internal_model_requests": None,
            "model_request_count_scope": "client-visible HTTP attempts; server-loop model generations are not exposed",
            "retryable": False, "observation_representations": [],
            "rate_limited": False, "rate_limit_source": None,
            "native_url_evidence": None, "visited_url": None,
            "page_verification_status": "unknown", "tool_input_audit_status": "unknown",
            "raw_http_body_available": False, "complete_upstream_messages_available": False}


def parse_response(request, raw):
    response = response_base(request)
    limited = is_rate_limited(raw)
    source = "stream" if "stream_finished" in raw else "body"
    if limited:
        response.update(rate_limited=True, rate_limit_source=source,
                        error=f"openrouter_{source}_429")
    metadata = raw.get("openrouter_metadata") or {}
    endpoints = metadata.get("endpoints")
    available = endpoints.get("available", []) if isinstance(endpoints, dict) else []
    selected = [e for e in available if isinstance(e, dict) and e.get("selected") is True]
    provider = (selected[0].get("provider") if len(selected) == 1 else None
                ) if "endpoints" in metadata else raw.get("provider")
    # Routing metadata reports display names, not request routing slugs. Keep
    # their expected identity explicit rather than guessing a slug/name mapping.
    selected_model = selected[0].get("model") if len(selected) == 1 else None
    response.update(raw_response=raw, model=raw.get("model"), provider=provider,
                    reported_provider=raw.get("provider"),
                    provider_evidence="openrouter_metadata" if "endpoints" in metadata else "response.provider",
                    provider_label_conflict=bool(selected and raw.get("provider") != provider),
                    usage=raw.get("usage") or {})
    choices = raw.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        if not limited:
            response["error"] = "missing_assistant_message"
        return response
    choice, message = choices[0], choices[0]["message"]
    answer = message.get("content") or message.get("refusal") or ""
    if isinstance(answer, list):
        answer = "\n".join(b.get("text", "") for b in answer if isinstance(b, dict) and b.get("type") == "text")
    trace = tool_trace(message)
    traced_ids = {t.get("call_id") for t in trace}
    trace.extend(t for t in upstream_tool_trace(raw.get("upstream_debug_bodies") or [])
                 if t.get("call_id") not in traced_ids)
    evidence = target_url_evidence(trace, request["input"]["content"])
    texts = [t["raw_result"] for t in trace
             if t["name"] in FETCH_NAMES and isinstance(t["raw_result"], str)]
    response.update(final_answer=answer, trace=trace, tool_calls=len(trace),
                    finish_reason=choice.get("finish_reason"), native_url_evidence=evidence,
                    visited_url=evidence["url"] if evidence else None,
                    page_verification_status="target_url_verified" if evidence else "unknown",
                    tool_input_audit_status=tool_input_audit_status(texts),
                    observation_representations=["openrouter_server_web_fetch_text"] if trace else [],
                    tool_observation_texts=texts,
                    upstream_debug_bodies_available=bool(raw.get("upstream_debug_bodies")),
                    tool_trace_scope="Returned tool records and echoed upstream tool messages; full provider-side context is not guaranteed")
    if limited:
        # Partial server-loop output remains unresolved and is never replayed here.
        response["error"] = f"openrouter_{source}_429"
    elif raw.get("stream_finished") is False or raw.get("error"):
        response["error"] = "incomplete_or_failed_stream"
    elif not isinstance(answer, str) or not answer.strip() or choice.get("finish_reason") != "stop":
        response["error"] = "no_complete_final_answer"
    elif message.get("tool_calls") or DSML.search(answer):
        response["error"] = "tool_call_without_final_answer"
    elif (raw.get("model") != request["target"]["model"]
          or (selected_model is not None and selected_model not in
              {request["target"]["model"], *ROUTING_MODEL_ALIASES.get(request["target"]["model"], ())})
          or (request["tool_config"]["provider_name"] is not None
              and provider != request["tool_config"]["provider_name"])):
        response.update(status="invalid", error="model_or_provider_mismatch")
    else:
        # A complete answer can precede page access or have redacted tool evidence.
        response["status"] = "completed"
    return response


def run_adapter(request_path, response_path):
    request_path, response_path = Path(request_path).resolve(), Path(response_path).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    body = build_request(request)
    if request["tool_config"].get("execution_authorized") is not True:
        raise ValueError("OpenRouter execution is paused; no model request was sent")
    evidence = response_path.parent / "openrouter-api-evidence"
    if response_path.exists() or evidence.exists():
        raise ValueError("Refusing to overwrite an OpenRouter URL attempt")
    from defipi.config import load_local_env
    load_local_env()
    binding = request["target"]["client_settings"]
    api_key = os.environ.get(binding["api_key_env"])
    if not api_key:
        raise ValueError(f"{binding['api_key_env']} is unavailable")
    base_url = binding["base_url"]
    endpoint = base_url.strip().rstrip("/") + "/chat/completions"
    evidence.mkdir(parents=True)
    request_headers = dict(binding["request_headers"])
    write_json(evidence / "request.json", {"endpoint": endpoint, "body": body,
                                          "headers": request_headers})
    response = response_base(request)
    metadata = None
    transport_error = None
    try:
        # No SDK retries: one client HTTP request contains the server-owned loop.
        with httpx.Client(timeout=binding["request_timeout_seconds"], follow_redirects=False) as client:
            headers = {**request_headers, "authorization": "Bearer " + api_key}
            # Save headers before consuming the body so stream interruptions cannot lose them.
            with client.stream("POST", endpoint, json=body, headers=headers) as result:
                metadata = response_metadata(result)
                write_json(evidence / "response_metadata.json", metadata)
                with (evidence / "response.txt").open("w", encoding="utf-8") as stream:
                    for chunk in result.iter_text():
                        stream.write(chunk)
                        stream.flush()
            response_text = (evidence / "response.txt").read_text(encoding="utf-8")
        if result.is_success:
            raw = (json_object(response_text) or parse_stream(response_text)) if body["stream"] else json.loads(response_text)
            response = parse_response(request, raw)
        else:
            response.update(status="failed", error=f"openrouter_http_{result.status_code}",
                            retryable=result.status_code == 429)
            raw = json_object(response_text) or {}
            response.update(raw_response=raw, rate_limited=is_rate_limited(raw),
                            rate_limit_source="body" if is_rate_limited(raw) else None)
            # A server-tool 5xx may follow paid internal steps. Keep its outcome
            # unresolved rather than transparently repeating the whole loop.
            if result.status_code >= 500:
                response.update(status="unresolved", error=f"unknown_server_loop_outcome_http_{result.status_code}")
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        transport_error = f"{type(exc).__name__}: {exc}"
        response.update(status="failed", error=type(exc).__name__, retryable=True)
    except Exception as exc:
        transport_error = f"{type(exc).__name__}: {exc}"
        response.update(status="unresolved", error=transport_error)
    partial = evidence / "response.txt"
    if transport_error and body["stream"] and partial.exists():
        partial_text = partial.read_text(encoding="utf-8")
        raw = json_object(partial_text) or parse_stream(partial_text)
        if is_rate_limited(raw):
            response = parse_response(request, raw)
    if transport_error:
        response["transport_error"] = transport_error
    if metadata is not None:
        deadline = metadata["retry_after_at"]
        response.update(http_status=metadata["status_code"], response_headers=metadata["headers"],
                        retry_after_seconds=max(0.0, (datetime.fromisoformat(deadline) -
                            datetime.now(timezone.utc)).total_seconds()) if deadline is not None else None)
        if metadata["status_code"] == 429:
            response.update(status="failed", error="openrouter_http_429", rate_limited=True,
                            rate_limit_source="http", retryable=True)
    response["native_request_path"] = str(evidence / "request.json")
    write_json(response_path, response)
    return response


def adapter_config(*, tool_config, timeout_seconds):
    return {"harness_type": None, "build": VERSION,
            "command": [sys.executable, str(ROOT / "cli/internal/openrouter_url_adapter.py"),
                        "--request", "{request_path}", "--response", "{response_path}"],
            "supported_inputs": ["url"], "timeout_seconds": timeout_seconds,
            "tool_config": dict(tool_config)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request")
    parser.add_argument("--response")
    parser.add_argument("--print-binding", action="store_true")
    args = parser.parse_args(argv)
    if args.print_binding:
        print(json.dumps(adapter_config(), ensure_ascii=False, indent=2))
        return 0
    if not args.request or not args.response:
        parser.error("--request and --response are required")
    run_adapter(args.request, args.response)
    return 0
