"""Shared native-agent request validation and trajectory utilities."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
from urllib.parse import urlsplit

from defipi.execution.runtime_paths import NODE_MODULES, executable as native_executable
from defipi.jsonio import write_json

RUNTIME = Path(__file__).with_name("native_runtime")
SYSTEM_PROMPT_POLICY = "native-unmodified"
DSH_ACCESS_POLICY = "native-tools-post-run-audit-v1"
CODEX_VERSION = "0.160.0"
PROTOCOL = "final-eval-adapter-v1"


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    # File iteration preserves Unicode line separators inside JSON strings.
    with path.open(encoding="utf-8") as source:
        for line, raw in enumerate(source, 1):
            try:
                rows.append(json.loads(raw))
            except ValueError:
                rows.append({"type": "malformed_event", "line": line})
    return rows


def validate_request(request, *, container=False, container_inner=False):
    from defipi.configuration.execution import validate_execution_request
    validate_execution_request(request, container=container, container_inner=container_inner)
    axes, target, settings = request["experiment"], request["target"], request["tool_config"]
    if request.get("schema_version") != PROTOCOL or axes.get("harness_type") not in {"codex", "dsh", "claude"}:
        raise ValueError("Expected the native Codex/DSH/Claude adapter protocol")
    if (axes.get("mode") != "agent" or axes.get("attack_method") != "off"
            or axes.get("web_injection_channel") != "url" or request["input"]["kind"] != "url"):
        raise ValueError("These native adapters support Agent URL/OFF only")
    if urlsplit(request["input"]["content"]).scheme not in {"http", "https"}:
        raise ValueError("The task page must be an HTTP(S) URL")
    if request.get("url_prompt_defense"):
        raise ValueError("Native OFF must not carry an in-agent Filter prompt")
    if settings.get("system_prompt_policy") != SYSTEM_PROMPT_POLICY:
        raise ValueError("Native agents must retain each harness's native system prompt")
    for key in ("max_tool_calls", "trajectory_timeout_seconds", "max_model_requests"):
        if type(settings.get(key)) is not int or settings[key] < 1:
            raise ValueError(f"Explicit positive tool_config.{key} is required")
    if request["limits"].get("api_concurrency") != 1:
        raise ValueError("Native trajectories use serial model requests")
    required = ("model", "backend")
    for key in required:
        if not isinstance(target.get(key), str) or not target[key]:
            raise ValueError(f"Bind target.{key} before execution")


def dsh_search_target(request):
    settings, target = request["tool_config"], request["target"]
    return {**settings["dsh_search_target"], "max_response_tokens": target["max_response_tokens"]}


def native_trace(harness, events):
    trace = []
    if harness == "codex":
        for line, event in enumerate(events, 1):
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
                trace.append({"type": "tool", "name": f"mcp__{item.get('server')}__{item.get('tool')}",
                              "call_id": item.get("id"), "input": item.get("arguments", {}),
                              "output": item.get("result") or {"content": [{"type": "text", "text": str(item.get('error'))}]},
                              "is_error": bool(item.get("error") or item.get("status") == "failed"
                                               or (item.get("result") or {}).get("isError")), "event_line": line})
            elif event.get("type") == "item.completed" and item.get("type") == "web_search":
                results = item.get("results")
                trace.append({"type": "tool", "name": "web_search", "call_id": item.get("id"),
                              "input": {"query": item.get("query"), "action": item.get("action")},
                              "output": {"content": ([{"type": "text", "text": json.dumps(results, ensure_ascii=False)}]
                                                     if results is not None else [])},
                              "is_error": bool(item.get("error") or item.get("status") == "failed"),
                              "event_line": line, "native_item": item})
    else:
        observed = [(line, event) for line, event in enumerate(events, 1)
                    if event.get("type") == "native_tool_result"]
        if observed:
            return [{"type": "tool", "name": event["name"], "call_id": event["call_id"],
                     "session_id": event.get("session_id"), "root_call_id": event.get("root_call_id"),
                     "input": event["input"], "output": {"content": event["output"].get("content", [])},
                     "is_error": bool(event["output"].get("isError")), "event_line": line}
                    for line, event in observed]
        calls = {}
        for line, event in enumerate(events, 1):
            data = event.get("data") or {}
            if event.get("type") == "tool/call":
                calls[data["callId"]] = data
            elif event.get("type") == "tool/result":
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") != "tool-result":
                        continue
                    call = calls.get(block.get("toolCallId"), {})
                    trace.append({"type": "tool", "name": call.get("name"), "call_id": block.get("toolCallId"),
                                  "input": call.get("arguments", {}), "output": {"content": block.get("content", [])},
                                  "is_error": bool(block.get("isError")), "event_line": line})
    return trace


def codex_wire_tool_count(evidence):
    """Count native calls including rejected calls that never reach MCP."""
    count = 0
    for path in (evidence / "provider").glob("*.response.body"):
        calls = set()
        for line in path.read_text(errors="replace").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            items = ([event["item"]] if isinstance(event.get("item"), dict) else [])
            items += (event.get("response") or {}).get("output", [])
            for item in items:
                if item.get("type") in {"function_call", "custom_tool_call", "web_search_call", "computer_call"}:
                    calls.add(item.get("call_id") or item.get("id"))
        count += len(calls)
    return count


def stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
