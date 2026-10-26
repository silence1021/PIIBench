"""Anthropic provider-native URL evaluation using the basic web_fetch tool."""
from __future__ import annotations

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

import httpx

from defipi.config import load_local_env
from defipi.configuration.execution import validate_execution_request
from defipi.jsonio import write_json

VERSION = "anthropic-native-url-v2-answer-validity"
ROOT = _PACKAGE_ROOT


def same_url(left, right):
    def normalize(value):
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                           parsed.path.rstrip("/") or "/", parsed.query, ""))
    return normalize(left) == normalize(right)


def build_request(request):
    validate_execution_request(request)
    axes, target = request["experiment"], request["target"]
    if (request.get("schema_version") != "final-eval-adapter-v1"
            or axes.get("mode") != "api" or axes.get("harness_type") is not None
            or request["input"]["kind"] != "url" or target["backend"] != "anthropic"):
        raise ValueError("Anthropic native URL adapter requires the API/URL protocol")
    if axes.get("attack_method") not in ("off", "url_prompt"):
        raise ValueError("Anthropic URL adapter supports OFF and url_prompt")
    if urlsplit(request["input"]["content"]).scheme not in ("http", "https"):
        raise ValueError("Native URL input must be HTTP(S)")
    metadata = request["tool_config"]["model_metadata"]
    if metadata.get("id") != target["model"] or type(metadata.get("max_tokens")) is not int:
        raise ValueError("Bind matching Models.retrieve metadata for required max_tokens")
    limit = metadata["max_tokens"] if target["max_response_tokens"] is None else target["max_response_tokens"]
    if type(limit) is not int or not 0 < limit <= metadata["max_tokens"]:
        raise ValueError("Invalid Anthropic max_tokens")
    body = {
        "model": target["model"], "max_tokens": limit,
        "messages": [{"role": "user", "content": request["question"] +
                      "\n\nPage URL: " + request["input"]["content"]}],
        "tools": [{"type": "web_fetch_20250910", "name": "web_fetch",
                   "citations": {"enabled": True}}],
    }
    for key in ("thinking", "output_config", "temperature", "top_p"):
        if target.get(key) is not None:
            body[key] = target[key]
    for parameter in target["client_settings"]["omit_parameters"]:
        body.pop(parameter, None)
    body.update(target["client_settings"]["request_parameters"])
    if axes["attack_method"] == "url_prompt":
        defense = request.get("url_prompt_defense")
        if (not isinstance(defense, dict) or defense.get("placement") != "system"
                or not isinstance(defense.get("prompt"), str) or not defense["prompt"].strip()):
            raise ValueError("URL defensive prompt requires non-empty prompt and placement=system")
        body["system"] = defense["prompt"]
    return body


def target_url_evidence(trace, url):
    for tool in trace:
        content = tool.get("output")
        if (tool.get("name") != "web_fetch" or tool.get("is_error")
                or not isinstance(content, dict) or content.get("type") != "web_fetch_result"):
            continue
        source = (content.get("content") or {}).get("source") or {}
        if (isinstance(content.get("url"), str) and same_url(content["url"], url)
                and source.get("type") == "text" and isinstance(source.get("data"), str)
                and source["data"].strip()):
            return {"type": "native_web_fetch_result", "url": content["url"],
                    "tool_use_id": tool.get("call_id")}
    return None


def parse_response(request, raw):
    content = raw.get("content") or []
    calls = {b["id"]: b for b in content if b.get("type") == "server_tool_use" and b.get("id")}
    trace = []
    for block in content:
        if block.get("type") != "web_fetch_tool_result":
            continue
        call = calls.get(block.get("tool_use_id"))
        if not call or call.get("name") != "web_fetch":
            continue
        output = block.get("content")
        trace.append({"type": "tool", "name": call["name"], "call_id": call["id"],
                      "input": call.get("input"), "output": output,
                      "is_error": not isinstance(output, dict) or output.get("type") != "web_fetch_result"})
    evidence = target_url_evidence(trace, request["input"]["content"])
    stopped = raw.get("stop_reason") == "end_turn"
    refused = raw.get("stop_reason") == "refusal"
    answer = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    response = {
        "schema_version": "final-eval-adapter-v1", "adapter_version": VERSION,
        "model": raw.get("model"), "harness_type": None,
        "status": "completed" if (stopped and answer.strip()) or refused else "failed",
        "final_answer": answer,
        "finish_reason": "stop" if stopped else raw.get("stop_reason"),
        "trace": trace, "usage": raw.get("usage") or {}, "raw_response": raw,
        "model_requests": 1, "tool_calls": sum(c.get("name") == "web_fetch" for c in calls.values()),
        "retryable": False, "model_request_count_scope": "client-visible requests",
        "observation_representations": ["anthropic_native_web_fetch_text"] if trace else [],
        "native_url_evidence": evidence, "visited_url": evidence["url"] if evidence else None,
        "page_verification_status": "target_url_verified" if evidence else "unknown",
    }
    # Completion and fetch evidence are separate, as in other URL/API paths.
    if raw.get("error"):
        response.update(status="failed", error="native_api_error")
    elif not (stopped or refused):
        response["error"] = "native_response_not_finished"
    elif not answer.strip() and not refused:
        response["error"] = "no_complete_final_answer"
    return response


def run_adapter(request_path, response_path):
    request_path, response_path = Path(request_path).resolve(), Path(response_path).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    body = build_request(request)
    settings = request["target"]["client_settings"]
    endpoint = settings["base_url"].rstrip("/") + "/messages"
    evidence = response_path.parent / "native-api-evidence"
    if response_path.exists() or evidence.exists():
        raise ValueError("Refusing to overwrite a native URL attempt")
    evidence.mkdir(parents=True)
    write_json(evidence / "request.json", {"endpoint": endpoint, "body": body})
    load_local_env()
    api_key = os.environ.get(settings["api_key_env"])
    if not api_key:
        raise ValueError(f"{settings['api_key_env']} is unavailable")
    response = {"schema_version": "final-eval-adapter-v1", "adapter_version": VERSION,
                "model": request["target"]["model"], "harness_type": None,
                "final_answer": "", "trace": [], "usage": {}, "model_requests": 1,
                "observation_representations": [], "retryable": False}
    try:
        with httpx.Client(timeout=settings["request_timeout_seconds"], follow_redirects=False) as client:
            result = client.post(endpoint, json=body, headers={**settings["request_headers"],
                "x-api-key": api_key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"})
        (evidence / "response.txt").write_text(result.text, encoding="utf-8")
        if not result.is_success:
            response.update(status="failed", error=f"native_api_http_{result.status_code}",
                            retryable=result.status_code in (429, 500, 502, 503, 504))
        else:
            response = parse_response(request, result.json())
    except Exception as exc:
        response.update(status="unresolved", error=f"{type(exc).__name__}: {exc}")
    response["native_request_path"] = str(evidence / "request.json")
    write_json(response_path, response)
    return response


def adapter_config(*, tool_config, timeout_seconds):
    return {"harness_type": None, "build": VERSION,
            "command": [sys.executable, str(ROOT / "cli/internal/anthropic_url_adapter.py"),
                        "--request", "{request_path}", "--response", "{response_path}"],
            "supported_inputs": ["url"], "timeout_seconds": timeout_seconds,
            "tool_config": dict(tool_config)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args(argv)
    run_adapter(args.request, args.response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
