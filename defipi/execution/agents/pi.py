"""Pi command adapter and managed browser sessions for final-eval-adapter-v1."""
from __future__ import annotations

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import argparse
import base64
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from defipi.execution.session_runtime import (
    isolated_runtime,
    session_environment,
    runtime_script,
    pi_package_directory,
)
import re
import shutil
import signal
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

from PIL import Image
from defipi.execution.runtime_paths import executable

from defipi.execution.agents.playwright import PlaywrightMcpSession
from defipi.configuration.execution import validate_execution_request
from defipi.jsonio import write_json

ROOT = _PACKAGE_ROOT
RUNTIME = Path(__file__).with_name("pi_runtime")
VERSION = "pi-agent-adapter-v13-tool-count-observe-only/session-v1"
ACCESS_POLICY = "native-tools-post-run-audit-v1"
PI_VERSION = "0.99.2"


def read_jsonl(path):
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            # Preserve an interrupted final line in the original event file.
            continue
    return rows


def stop_process(process, timeout=8):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def model_binding(target, *, container=False):
    """Resolve Pi's native model from the caller's complete explicit binding."""
    from defipi.configuration.execution import validate_native_target
    validate_native_target(target, "pi-agent", container=container)
    settings = target["client_settings"]
    return {"provider": target["backend"], "model": target["model"],
            "api": target["api"], "reasoning_effort": target["reasoning_effort"],
            "pi_model": target["pi_model"], **settings,
            "request_parameters": settings["request_parameters"] if container or target["backend"] == "openrouter" else {}}


def native_base_url(base_url, api):
    """Anthropic's SDK appends /v1/messages; retain any proxy capability path."""
    if api != "anthropic-messages":
        return base_url
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit(parsed._replace(path=path))


def validate_request(request, *, container=False, container_inner=False):
    if request.get("schema_version") != "final-eval-adapter-v1":
        raise ValueError("Unsupported Pi adapter protocol")
    if request["experiment"].get("harness_type") != "pi-agent":
        raise ValueError("Pi adapter requires harness_type=pi-agent")
    if request["input"]["kind"] not in ("html", "url"):
        raise ValueError("Pi adapter supports HTML and URL inputs")
    validate_execution_request(request, container=container, container_inner=container_inner)
    model_binding(request["target"], container=container)
    if request["experiment"].get("attack_method") == "url_prompt":
        defense = request.get("url_prompt_defense")
        if request["input"]["kind"] != "url":
            raise ValueError("URL defensive prompt requires URL input")
        if (not isinstance(defense, dict) or defense.get("placement") != "system"
                or not isinstance(defense.get("prompt"), str) or not defense["prompt"].strip()):
            raise ValueError("URL defensive prompt requires non-empty prompt and placement=system")
    settings = request["tool_config"]
    profile = settings["tool_profile"]
    if profile not in {"native", "html-read-only"}:
        raise ValueError("Unknown Pi tool profile")
    if profile == "html-read-only" and request["input"]["kind"] != "html":
        raise ValueError("html-read-only requires HTML input")
    if settings.get("input_policy") != ACCESS_POLICY:
        raise ValueError("Bind the Pi post-run access audit policy")
    for key in ("max_tool_calls", "trajectory_timeout_seconds"):
        if type(settings.get(key)) is not int or settings[key] <= 0:
            raise ValueError(f"tool_config.{key} must be a positive integer")
    if request["limits"].get("api_concurrency") != 1:
        raise ValueError("Pi trajectories require serial model requests")


def collect_result(request, evidence, browser, pi_code):
    events = read_jsonl(evidence / "events.jsonl")
    provider = read_jsonl(evidence / "provider-events.jsonl")
    assistants = [e["message"] for e in events if e.get("type") == "message_end"
                  and e.get("message", {}).get("role") == "assistant"]
    final = assistants[-1] if assistants else {}
    answer = "\n".join(item.get("text", "") for item in final.get("content", []) if item.get("type") == "text")
    starts = {e["toolCallId"]: e for e in events if e.get("type") == "tool_execution_start"}
    trace = []
    for event in events:
        if event.get("type") == "tool_execution_end":
            start = starts.get(event["toolCallId"], {})
            trace.append({"type": "tool", "name": event.get("toolName"),
                          "input": start.get("args", {}), "output": event.get("result"),
                          "parent_call_id": start.get("parentToolCallId"),
                          "model_visible": not bool(start.get("parentToolCallId")),
                          "is_error": event.get("isError", False), "call_id": event["toolCallId"]})
    usage = {key: sum((m.get("usage") or {}).get(key, 0) for m in assistants)
             for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens")}
    usage["pi_catalog_cost_estimate_usd"] = sum((m.get("usage") or {}).get("cost", {}).get("total", 0) for m in assistants)
    if request["target"]["pi_model"] is not None:
        # The per-run model registration has no verified catalog price.
        usage["pi_catalog_cost_estimate_usd"] = None
    count = sum(e["type"] == "request" for e in provider)
    tool_calls = max(sum(e["type"] == "tool_call" for e in provider),
                     sum(e.get("type") == "tool_execution_start" for e in events))
    budget_stop = next((e for e in provider if e["type"] == "budget_stop"), None)
    threshold = request["tool_config"]["max_tool_calls"]
    tool_budget = {"unit": "tool_call", "limit": threshold, "threshold": threshold,
                   "used": tool_calls, "exceeded": tool_calls > threshold,
                   "excess": max(0, tool_calls - threshold),
                   "enforcement": "observe_only"}
    settled = any(e.get("type") == "agent_settled" for e in events)
    response = {
        "schema_version": "final-eval-adapter-v1", "adapter_version": VERSION,
        "harness_type": "pi-agent", "model": request["target"]["model"], "pi_version": PI_VERSION,
        "status": "completed", "final_answer": answer, "finish_reason": final.get("stopReason"),
        "trace": trace, "usage": usage, "model_requests": count, "agent_settled": settled,
        "tool_calls": tool_calls, "tool_budget": tool_budget,
        "tool_profile": request["tool_config"]["tool_profile"],
        "tool_policy": ACCESS_POLICY, "input_policy": ACCESS_POLICY,
        "tool_policy_blocks": [e for e in provider if e["type"] == "tool_policy_block"],
        "tool_output_paths": [e for e in provider if e["type"] == "tool_output_path"],
        "provider_usage": [e for e in provider if e.get("usage")],
        "provider_routing": [e for e in provider if e.get("type") == "provider_routing"],
        "usage_complete": count == len(assistants),
        "pi_exit_code": pi_code, "observation_representations": [], "retryable": False,
        "transport_errors": [event for event in provider if event.get("type") == "transport_error"],
        "provider_error_message": final.get("errorMessage"),
        "provider_response_id": final.get("responseId"),
    }
    if request["input"]["kind"] == "html":
        for tool in trace:
            if tool["name"] == "read" and not tool["is_error"] and tool["model_visible"]:
                requested = Path(tool["input"].get("path", ""))
                if not requested.is_absolute():
                    requested = evidence.parent / "work" / requested
                if requested.resolve() == (evidence.parent / "work/input.html").resolve():
                    response["observation_representations"] = ["html_file_text"]
    elif browser and browser.session:
        observations = set()
        for tool in trace:
            if tool["is_error"] or not tool["model_visible"] or not str(tool["name"]).startswith("mcp__playwright__"):
                continue
            text = "\n".join(c.get("text", "") for c in (tool["output"] or {}).get("content", []) if c.get("type") == "text")
            if "[Snapshot](" in text:
                observations.add("playwright_mcp_snapshot_path")
            elif "### Snapshot" in text or "### Page state" in text or "- Page Snapshot:" in text:
                observations.add("playwright_mcp_accessibility_snapshot")
            if tool["name"].endswith("__browser_evaluate") or "__browser_run_code" in tool["name"]:
                observations.add("dom_javascript_result")
            if tool["name"].endswith("__browser_take_screenshot"):
                observations.add("screenshot_path")
            if tool["name"].endswith("__browser_navigate"):
                observations.add("navigation_status")
        for tool in trace:
            if tool["name"] == "read" and not tool["is_error"] and tool["model_visible"]:
                requested = Path(tool["input"].get("path", ""))
                if not requested.is_absolute():
                    requested = evidence.parent / "work" / requested
                if requested.suffix in (".yml", ".yaml") and requested.resolve().is_relative_to(browser.directory / "artifacts"):
                    observations.add("playwright_mcp_accessibility_snapshot")
        response["observation_representations"] = sorted(observations)
        if any(item.get("type") == "image" for t in trace if t["model_visible"]
               for item in (t.get("output") or {}).get("content", [])):
            response["observation_representations"].append("screenshot_image")
        documents = read_jsonl(browser.directory / "navigation.jsonl")
        requested_url = request["input"]["content"]
        match = next((e for e in documents if e.get("type") == "document" and e.get("url") == requested_url
                      and 200 <= e.get("status", 0) < 300), None)
        if match:
            response.update(visited_url=match["url"])
        response["browser"] = dict(browser.session)
        version_file = browser.directory / "browser-version.json"
        if version_file.exists():
            response["browser"].update(json.loads(version_file.read_text()))
    # Describe the actual surface without requiring a particular tool or access
    # route. Native Shell output is reported separately from browser navigation.
    observations = set(response["observation_representations"])
    for tool in trace:
        if tool["is_error"] or not tool["model_visible"]:
            continue
        if tool["name"] in ("bash", "powershell"):
            observations.add("terminal_output")
        elif tool["name"] == "read":
            observations.add("native_file_read_result")
        elif tool["name"] == "codemode":
            observations.add("native_code_result")
    response["observation_representations"] = sorted(observations)
    verification = "unknown"
    if response.get("visited_url"):
        verification = "browser_document_observed"
    response["page_verification_status"] = verification
    response["page_verification_source"] = "playwright_navigation_capture" if response.get("visited_url") else None
    audit_path = evidence.parent / "access-audit.json"
    write_json(audit_path, {
        "policy": ACCESS_POLICY,
        "tool_profile": request["tool_config"]["tool_profile"],
        "enforcement": "supplied-html-read-only" if request["tool_config"].get("tool_profile") == "html-read-only" else "none",
        "affects_run_status": False,
        "working_directory": str(evidence.parent / "work"),
        "input_kind": request["input"]["kind"], "page_verification_status": verification,
        "visited_url": response.get("visited_url"),
        "tool_records": [{"trace_index": i, "tool": tool["name"], "call_id": tool["call_id"],
                          "parent_call_id": tool["parent_call_id"], "model_visible": tool["model_visible"],
                          "input": tool["input"], "reported_error": tool["is_error"]}
                         for i, tool in enumerate(trace)],
        "coverage": "Native Pi tool arguments and results are retained in the trace. Arguments are not proof "
                    "of file access. Shell/code and indirect reads require review of the original trace; "
                    "this is not an OS-level access trace.",
    })
    response["access_audit_path"] = str(audit_path)
    if budget_stop:
        response.update(status="failed", error="shared_model_request_budget_exhausted", budget_stop=budget_stop)
    elif not settled or final.get("stopReason") != "stop" or pi_code != 0:
        known = [e for e in provider if e["type"] == "response" and e["status"] >= 400]
        status = known[-1]["status"] if known else None
        response.update(status="failed" if status or count == 0 else "unresolved",
                        error=f"provider_http_{status}" if status else "pi_did_not_complete",
                        retryable=bool(status in (408, 429) or (status and status >= 500)))
    elif (any(item.get("type") == "toolCall" for item in final.get("content", []))
          or re.search(r"<[｜|]*DSML[｜|]*\s*(?:calls|function_calls|invoke)\b", answer)):
        # A provider may serialize a pending tool invocation as ordinary text
        # and still report stop. This is not a completed contact answer.
        response.update(status="unresolved", error="unfinished_tool_call_in_final_answer")
    elif not answer:
        response.update(status="failed", error="missing_answer")
    return response


def save_trajectory(request, evidence, response):
    """Export observable model/tool turns; the original Pi JSONL stays authoritative."""
    evidence = Path(evidence).resolve()
    events_path = evidence / "events.jsonl"
    steps, calls, malformed_lines, returned_images = [], {}, [], []

    def save_image(block, line, index, call_id, model_visible):
        item = {"call_id": call_id, "content_index": index, "mimeType": block.get("mimeType"),
                "model_visible": model_visible,
                "raw_events_path": str(events_path), "raw_events_line": line,
                "path": None, "width": None, "height": None, "contact_visibility": "not_reviewed"}
        try:
            data = base64.b64decode(block["data"], validate=True)
            with Image.open(io.BytesIO(data)) as opened:
                item.update(width=opened.width, height=opened.height)
                suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}.get(opened.format, ".bin")
                opened.verify()
            path = evidence.parent / "returned_images" / f"event_{line:06d}_image_{index:02d}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            item["path"] = str(path)
        except (KeyError, ValueError, TypeError, OSError) as exc:
            # A malformed image must not discard the rest of an interrupted run.
            item["archive_error"] = f"{type(exc).__name__}: {exc}"
        returned_images.append(item)
        return item

    def new_step():
        step = {"turn": len(steps), "assistant": {"content": []}, "assistant_complete": False,
                "tools": [], "turn_complete": False}
        steps.append(step)
        return step

    def compact_media(value, line):
        if isinstance(value, list):
            return [compact_media(item, line) for item in value]
        if isinstance(value, dict):
            if value.get("type") == "image":
                return {"type": "image", "mimeType": value.get("mimeType"),
                        "raw_events_path": str(events_path), "raw_events_line": line}
            return {key: compact_media(item, line) for key, item in value.items()}
        return value

    if events_path.exists():
        with events_path.open(encoding="utf-8") as stream:
            for line, raw in enumerate(stream, 1):
                try:
                    event = json.loads(raw)
                except ValueError:
                    malformed_lines.append(line)
                    continue
                kind = event.get("type")
                if kind == "turn_start":
                    new_step()
                    continue
                if kind not in {"message_end", "message_update", "tool_execution_start", "tool_execution_end", "turn_end"}:
                    continue
                if kind == "message_end" and event.get("message", {}).get("role") != "assistant":
                    continue
                step = steps[-1] if steps else new_step()
                if kind == "message_end":
                    step.update(assistant=compact_media(event["message"], line), assistant_complete=True,
                                assistant_event_line=line)
                elif kind == "message_update":
                    # Retain streamed text if the process ends before message_end.
                    update = event.get("assistantMessageEvent") or {}
                    if update.get("type") in {"thinking_delta", "text_delta"}:
                        block_type = update["type"].removesuffix("_delta")
                        index = update["contentIndex"]
                        content = step["assistant"]["content"]
                        while len(content) <= index:
                            content.append({})
                        block = content[index]
                        block["type"] = block_type
                        block[block_type] = block.get(block_type, "") + update.get("delta", "")
                        step["assistant_event_line"] = line
                elif kind in {"tool_execution_start", "tool_execution_end"}:
                    call_id = event["toolCallId"]
                    if call_id not in calls:
                        calls[call_id] = {"call_id": call_id, "name": event.get("toolName"),
                                          "arguments": None, "result": None, "status": "started"}
                        step["tools"].append(calls[call_id])
                    tool = calls[call_id]
                    if kind == "tool_execution_start":
                        tool.update(arguments=event.get("args", {}), start_event_line=line,
                                    parent_call_id=event.get("parentToolCallId"),
                                    model_visible=not bool(event.get("parentToolCallId")))
                    else:
                        tool.update(result=compact_media(event.get("result"), line), end_event_line=line,
                                    is_error=event.get("isError", False), status="completed")
                        tool["returned_images"] = []
                        for index, block in enumerate((event.get("result") or {}).get("content", [])):
                            if block.get("type") == "image":
                                item = save_image(block, line, index, call_id, tool.get("model_visible", True))
                                tool["returned_images"].append(item)
                                tool["result"]["content"][index].update(item)
                        tool["image_blocks_returned"] = len(tool["returned_images"])
                elif kind == "turn_end":
                    step["turn_complete"] = True

    thinking = [block for step in steps for block in step["assistant"]["content"]
                if block.get("type") == "thinking"]
    readable = [block["thinking"] for block in thinking if block.get("thinking") and not block.get("redacted")]
    reasoning = {"source": "provider_returned_thinking", "text_available": bool(readable),
                 "text_blocks": len(readable), "text_characters": sum(map(len, readable)),
                 "redacted_blocks": sum(bool(block.get("redacted")) for block in thinking)}
    path = evidence.parent / "trace.json"
    image_index_path = evidence.parent / "returned_images/index.json"
    visible_images = [item for item in returned_images if item["model_visible"]]
    image_summary = {"image_result_returned": bool(visible_images), "image_blocks_returned": len(visible_images),
                     "provider_image_delivery": "unknown", "returned_images_index_path": str(image_index_path)}
    write_json(image_index_path, {"schema_version": "pi-returned-images-v1", **image_summary,
                                  "returned_images": returned_images})
    trace = {
        "schema_version": "pi-agent-trajectory-v1", "adapter_version": response["adapter_version"],
        "case_id": request.get("case_id"), "model": response["model"], "harness_type": "pi-agent",
        "status": response["status"], "answer": response["final_answer"], "error": response.get("error"),
        "steps": steps, "reasoning": reasoning, "usage": response["usage"],
        "model_requests": response["model_requests"],
        "transport_errors": response["transport_errors"],
        "provider_error_message": response["provider_error_message"],
        "provider_response_id": response["provider_response_id"],
        "tool_calls": response["tool_calls"], "tool_budget": response["tool_budget"],
        "tool_policy": response["tool_policy"], "tool_policy_blocks": response["tool_policy_blocks"],
        "tool_output_paths": response["tool_output_paths"],
        "access_audit_path": response["access_audit_path"],
        "page_verification_status": response["page_verification_status"],
        "model_responses": sum(step["assistant_complete"] for step in steps),
        "agent_settled": response["agent_settled"], "browser_closed": response.get("browser_closed"),
        "observation_representations": response["observation_representations"],
        "raw_events_path": str(events_path), "malformed_event_lines": malformed_lines,
        "raw_session_paths": [str(p) for p in sorted((evidence / "sessions").glob("*.jsonl"))],
        "model_request_paths": [str(p) for p in sorted((evidence / "requests").glob("*.json"))],
        "browser_artifacts_dir": str(evidence.parent / "browser/artifacts") if request["input"]["kind"] == "url" else None,
        "trace_path": str(path), **image_summary,
    }
    write_json(path, trace)
    return trace


@isolated_runtime
def run_adapter(request_path, response_path, *, container=False):
    request_path, response_path = Path(request_path).resolve(), Path(response_path).resolve()
    request = json.loads(request_path.read_text())
    validate_request(request, container=container, container_inner=container)
    base = response_path.parent
    evidence, work, agent_dir = base / "pi-evidence", base / "work", base / "pi-config"
    if response_path.exists() or evidence.exists():
        raise ValueError("Refusing to overwrite a Pi attempt")
    for directory in (evidence, work, agent_dir):
        directory.mkdir(parents=True)
    settings, kind = request["tool_config"], request["input"]["kind"]
    binding = model_binding(request["target"], container=container)
    pi = executable("pi") if settings.get("pi_executable") is None else settings["pi_executable"]
    if not pi or subprocess.check_output([pi, "--version"], text=True, cwd=work,
            env=dict(os.environ, PI_CODING_AGENT_DIR=str(agent_dir))).strip() != PI_VERSION:
        raise ValueError(f"Pi {PI_VERSION} is required")
    from defipi.config import load_local_env
    inherited = dict(os.environ)
    load_local_env()
    key_env = binding["api_key_env"]
    if not os.environ.get(key_env):
        raise ValueError(f"{key_env} is unavailable")
    observer_config = {
        "evidence_dir": str(evidence), "work_dir": str(work), "case_id": request["case_id"],
        "input_kind": kind, "input_file": str(work / "input.html"),
        "tool_profile": settings["tool_profile"],
        "browser_artifacts_dir": str(base / "browser/artifacts"),
        "max_tool_calls": settings["max_tool_calls"],
        "provider_api": binding["api"], "provider": binding["provider"],
        "model": binding["model"], "request_parameters": binding["request_parameters"],
        "omit_parameters": request["target"]["pi_omit_parameters"],
        "reasoning_effort": binding["reasoning_effort"],
        "shared_request_budget": settings.get("shared_request_budget"),
    }
    if "max_response_tokens" in request["target"]:
        observer_config["max_response_tokens"] = request["target"]["max_response_tokens"]
    if observer_config["shared_request_budget"] and "directory" in observer_config["shared_request_budget"]:
        Path(observer_config["shared_request_budget"]["directory"]).mkdir(parents=True, exist_ok=True)
    write_json(base / "observer-config.json", observer_config)
    pi_settings = {
        "defaultProvider": binding["provider"], "defaultModel": binding["model"],
        "defaultThinkingLevel": "off" if binding["reasoning_effort"] == "none" else binding["reasoning_effort"],
        "compaction": {"enabled": False}, "retry": {"enabled": False, "provider": {"maxRetries": 0}},
        "cacheWarming": "off", "httpIdleTimeoutMs": settings["pi_http_idle_timeout_seconds"] * 1000,
    }
    write_json(agent_dir / "settings.json", pi_settings)
    # A provider-only override retains Pi's catalog metadata while binding the
    # explicitly configured endpoint and credential reference.
    provider = {"api": binding["api"], "baseUrl": native_base_url(binding["base_url"], binding["api"]), "apiKey": "$" + key_env,
                **({"headers": binding["request_headers"]} if binding["request_headers"] else {})}
    if binding["pi_model"] is not None:
        provider["models"] = [{"id": binding["model"], **binding["pi_model"]}]
    write_json(agent_dir / "models.json", {"providers": {binding["provider"]: provider}})
    if settings.get("model_proxy_url"):
        # Change only the transport; preserve the native model catalog.
        models_path = agent_dir / "models.json"
        models = json.loads(models_path.read_text()) if models_path.exists() else {"providers": {}}
        provider = models["providers"].setdefault(binding["provider"], {})
        provider.update(api=binding["api"], baseUrl=native_base_url(settings["model_proxy_url"], binding["api"]), apiKey="$" + key_env)
        write_json(models_path, models)
    (work / "artifacts").mkdir()
    env = dict(inherited, PI_CODING_AGENT_DIR=str(agent_dir), PI_SESSION_CONFIG=str(base / "observer-config.json"),
               PI_OFFLINE="1", PI_TELEMETRY="0", TMPDIR=str(work / "artifacts"))
    package_dir, package_source = pi_package_directory(pi, executable("pi"), base, PI_VERSION)
    env = session_environment(env, base)
    env["PI_PACKAGE_DIR"] = str(package_dir)
    env[key_env] = os.environ[key_env]
    command = [pi, "--offline", "--approve", "--provider", binding["provider"], "--model", binding["model"],
               "--thinking", pi_settings["defaultThinkingLevel"], "-e", str(runtime_script(RUNTIME / "observer.ts", base))]
    if settings.get("tool_profile") == "html-read-only":
        command += ["--tools", "read"]
    if request["experiment"].get("attack_method") == "url_prompt":
        # Use the frozen request text, rather than rereading the mutable prompt source.
        # Pi appends this to its own system prompt on every model turn.
        defense_path = evidence / "url-filter-system-prompt.txt"
        defense_path.write_text(request["url_prompt_defense"]["prompt"], encoding="utf-8")
        command += ["--append-system-prompt", str(defense_path)]
    browser = agent = None
    response = None
    started = time.monotonic()
    previous_handlers = {}
    def interrupted(signum, _frame):
        raise InterruptedError(f"Pi adapter interrupted by signal {signum}")
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, interrupted)
    try:
        if kind == "html":
            (work / "input.html").write_text(request["input"]["content"], encoding="utf-8")
            prompt = request["question"] + "\n\nInput file: ./input.html"
        else:
            browser = PlaywrightMcpSession(base / "browser", chrome=settings["chrome_executable"],
                startup_timeout_seconds=settings["mcp_startup_timeout_seconds"])
            browser.start()
            write_json(agent_dir / "mcp.json", browser.pi_config())
            prompt = request["question"] + "\n\nPage URL: " + request["input"]["content"]
        (evidence / "prompt.txt").write_text(prompt)
        # Validate that the explicit observer loaded before allowing inference.
        preflight = subprocess.run(command + ["--mode", "rpc", "--no-session"], cwd=work, env=env,
                                   input='{"id":"preflight","type":"get_commands"}\n',
                                   capture_output=True, text=True, timeout=30)
        (evidence / "preflight.jsonl").write_text(preflight.stdout)
        (evidence / "preflight.stderr.log").write_text(preflight.stderr)
        if preflight.returncode or not (evidence / "observer-loaded.json").exists():
            raise RuntimeError("Pi observer preflight failed")
        invoke = command + ["--mode", "json", "--session-dir", str(evidence / "sessions"), prompt]
        write_json(evidence / "invocation.json", {"argv": invoke, "cwd": str(work), "adapter_version": VERSION})
        with (evidence / "events.jsonl").open("wb") as stdout, (evidence / "stderr.log").open("wb") as stderr:
            agent = subprocess.Popen(invoke, cwd=work, env=env, stdout=stdout, stderr=stderr)
            agent.wait(timeout=settings["trajectory_timeout_seconds"])
        if browser:
            browser.close()
        response = collect_result(request, evidence, browser, agent.returncode)
    except Exception as exc:
        stop_process(agent)
        if browser:
            browser.close()
        provider = read_jsonl(evidence / "provider-events.jsonl")
        count = sum(e["type"] == "request" for e in provider)
        response = collect_result(request, evidence, browser, agent.returncode if agent else None)
        response.update(status="unresolved" if count else "failed", error=f"{type(exc).__name__}: {exc}",
                        retryable=count == 0)
    finally:
        stop_process(agent)
        cleaned = browser.close() if browser else True
        if response is not None:
            response["runtime_packages"] = [{"runtime_root": str(package_dir), "archive_root": str(package_source), "version": PI_VERSION}]
            response["browser_closed"] = cleaned
            response["wall_seconds"] = round(time.monotonic() - started, 3)
            response["completed_at"] = datetime.now(timezone.utc).isoformat()
            if not cleaned:
                response.update(status="unresolved", error="browser_cleanup_incomplete", retryable=False)
            trajectory = save_trajectory(request, evidence, response)
            response.update(trajectory_path=trajectory["trace_path"], reasoning=trajectory["reasoning"])
            for key in ("returned_images_index_path", "image_result_returned", "image_blocks_returned", "provider_image_delivery"):
                response[key] = trajectory[key]
            write_json(response_path, response)
            write_json(evidence / "exit.json", {"status": response["status"], "pi_exit_code": agent.returncode if agent else None,
                                              "browser_closed": cleaned, "model_requests": response["model_requests"],
                                              "tool_calls": response["tool_calls"], "tool_budget": response["tool_budget"]})
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return response


def adapter_config(*, tool_config, timeout_seconds):
    """Worker settings; public bindings wrap this in container_agent."""
    import sys
    return {
        "harness_type": "pi-agent", "build": VERSION + "/pi@" + PI_VERSION,
        "command": [sys.executable, str(ROOT / "cli/internal/pi_agent_adapter.py"), "--request", "{request_path}", "--response", "{response_path}"],
        "supported_inputs": ["html", "url"], "timeout_seconds": timeout_seconds + 90,
        "tool_config": {**tool_config,
                        "input_policy": ACCESS_POLICY},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request")
    parser.add_argument("--response")
    parser.add_argument("--print-binding", action="store_true")
    parser.add_argument("--container-image", help="Built shared agent image tag")
    parser.add_argument("--max-model-requests", type=int)
    args = parser.parse_args(argv)
    from defipi.execution.agents.container import adapter_config as container_config, run_adapter as run_container
    if args.print_binding:
        print(json.dumps(container_config("pi-agent", args.container_image,
                         max_model_requests=args.max_model_requests), ensure_ascii=False, indent=2))
        return 0
    if not args.request or not args.response:
        parser.error("--request and --response are required")
    run_container(args.request, args.response)
    return 0
