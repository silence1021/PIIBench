"""URL/OFF adapter for native DeepSeek Harness (DSH)."""
from __future__ import annotations

from defipi.execution.agents.common import (
    dsh_search_target,
    native_trace,
    read_jsonl,
    stop_process,
    validate_request,
    write_json,
)

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from defipi.execution.session_runtime import isolated_runtime, session_environment, runtime_script, workspace_mapping
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from defipi.execution.api.native_model_proxy import NativeModelProxy
from defipi.execution.runtime_paths import NODE_MODULES
from defipi.execution.agents.playwright import PlaywrightMcpSession

ROOT = _PACKAGE_ROOT
RUNTIME = Path(__file__).with_name("native_runtime")
VERSION = "native-url-agent-v10-tool-count-observe-only"
SYSTEM_PROMPT_POLICY = "native-unmodified"
DSH_WEB_TOOLS_POLICY = "native-tools-with-official-playwright-mcp"
DSH_ACCESS_POLICY = "native-tools-post-run-audit-v1"
DSH_VERSION = "0.1.2-rc.1"
DSH_MODULES = NODE_MODULES
PROTOCOL = "final-eval-adapter-v1"


def prompt_for(request):
    return request["question"] + "\n\nPage URL: " + request["input"]["content"]


def dsh_provider(request):
    target = request["target"]
    if target["backend"] == "deepseek" and target["api"] == "openai-completions":
        return "deepseek-official"
    return target["backend"]


def dsh_patch(request, proxy_url, bridge_path, work):
    settings, target = request["tool_config"], request["target"]
    model = {"id": target["model"]}
    # Catalog models keep their metadata. Custom names require explicit capacity
    # and reasoning capabilities rather than guessing a context/output limit.
    model.update(settings["dsh_model_metadata"])
    native_base = proxy_url.removesuffix("/v1") if target["api"] == "anthropic-messages" else proxy_url
    provider = {"api": target["api"], "baseURL": native_base, "apiKeyEnv": "LOCAL_PROXY_KEY",
                "models": [model], "transport": "sse", "retryPolicy": {"mode": "normal", "maxRetries": 0}}
    # JSON is a YAML subset and avoids interpolation of arbitrary model text.
    patches = [{"id": name, "disabled": True} for name in
               ("headless-startup", "headless-runner", "session-title-llm", "llm-retry")]
    if dsh_provider(request) == "deepseek-official":
        metadata = settings["dsh_model_metadata"]
        catalog_model = {"id": target["model"]}
        for key in ("contextWindow", "maxTokens"):
            if key in metadata:
                catalog_model[key] = metadata[key]
        if "input" in metadata:
            catalog_model["inputModalities"] = metadata["input"]
        native_config = {"baseURL": proxy_url, "apiKeyEnv": "LOCAL_PROXY_KEY",
                         "models": [catalog_model], "retryPolicy": {"mode": "normal", "maxRetries": 0}}
        if target.get("reasoning_effort"):
            native_config["reasoningEffort"] = target["reasoning_effort"]
        if target.get("max_response_tokens"):
            native_config["maxTokens"] = target["max_response_tokens"]
        patches.append({"id": "llm-deepseek", "config": native_config})
    else:
        patches.append({"id": "llm-pi-ai", "config": {"providers": {target["backend"]: provider}}})
    search = dsh_search_target(request)
    search_config = {"baseURL": proxy_url + "/native-web-search", "apiKeyEnv": "LOCAL_PROXY_KEY",
                     "model": search["model"]}
    if search.get("max_response_tokens"):
        search_config["maxTokens"] = search["max_response_tokens"]
    patches += [{"id": "web-search-deepseek", "config": search_config},
                {"id": "agent-default-model", "config": {"provider": dsh_provider(request), "model": target["model"]}},
                {"insert": [{"id": "browser-client", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                    "serverName": "playwright", "transport": "stdio", "command": shutil.which("node"),
                    "args": [str(runtime_script(RUNTIME / "browser_observer.mjs", Path(bridge_path).parent)), str(bridge_path)], "cwd": str(work),
                    "toolCallTimeoutMs": settings["mcp_tool_timeout_seconds"] * 1000, "failOnStartupError": True, "reconnect": {"enabled": False}}}]}]
    return patches


def dsh_access_audit(events, trace, browser):
    """Describe observed accesses after execution; findings never deny tools or fail a run."""
    runtime = next((row for row in events if row.get("type") == "runtime"), {})
    artifact_root = (browser.directory / "artifacts").resolve() if browser else None
    observations = {(row.get("session_id"), row.get("call_id")): row for row in events
                    if row.get("type") == "native_file_observation"}
    files, review, counts = [], [], {}
    for call in trace:
        name, args = str(call.get("name")), call.get("input")
        counts[name] = counts.get(name, 0) + 1
        args = args if isinstance(args, dict) else {}
        if name in {"read", "read_image", "write", "edit", "glob", "grep"}:
            observation = observations.get((call.get("session_id"), call.get("call_id")), {})
            target = observation.get("target", {})
            requested = args.get("file_path") or args.get("path")
            canonical = target.get("targetKey")
            basis = "native_fs_target"
            if not isinstance(canonical, str) or not Path(canonical).is_absolute():
                canonical, basis = None, "unresolved"
                if isinstance(requested, str) and runtime.get("cwd"):
                    try:
                        canonical = str((Path(runtime["cwd"]) / Path(requested).expanduser()).resolve())
                        basis = "post_run_path_resolution"
                    except (OSError, RuntimeError, ValueError):
                        pass
            outside = (not Path(canonical).is_relative_to(artifact_root)
                       if canonical and artifact_root else None)
            files.append({"name": name, "call_id": call.get("call_id"), "session_id": call.get("session_id"),
                          "requested_path": requested, "resolved_path": canonical, "path_basis": basis,
                          "outside_browser_artifacts": outside, "succeeded": not call["is_error"],
                          "native_observation": observation.get("observation"), "event_line": call["event_line"]})
        elif ((name not in {"web_search", "web_fetch"} and not name.startswith("mcp__playwright__"))
              or name.endswith(("__browser_run_code", "__browser_run_code_unsafe", "__browser_file_upload"))
              or "paths" in args or "filename" in args):
            review.append({"name": name, "call_id": call.get("call_id"), "session_id": call.get("session_id"),
                           "input": args, "event_line": call["event_line"]})
    outside_reads = [row for row in files if row["name"] in {"read", "read_image"}
                     and row["succeeded"] and row["outside_browser_artifacts"] is True]
    return {"policy": DSH_ACCESS_POLICY, "enforcement": "none", "affects_run_status": False,
            "browser_artifacts_directory": str(artifact_root) if artifact_root else None,
            "tool_call_counts": counts, "file_accesses": files,
            "successful_reads_outside_browser_artifacts": outside_reads,
            "calls_for_review": review,
            "native_file_observations": list(observations.values()),
            "coverage": "Native file tool paths and results are recorded. Shell/code and other native calls require "
                        "review of their archived arguments/results; this is not an OS-level file-access trace."}


def collect_result(request, evidence, browser, exit_code, error=None):
    harness = request["experiment"]["harness_type"]
    events = read_jsonl(evidence / "events.jsonl")
    bridge = read_jsonl(evidence / "browser-events.jsonl")
    provider = read_jsonl(evidence / "provider/events.jsonl")
    trace = native_trace(harness, events)
    usage = {}
    result_path = evidence / "dsh-result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    answer = result.get("answer", "")
    settled = result.get("end", {}).get("kind") == "completed" and not result.get("pending_tool_call")
    for event in events:
        chunk = event.get("data", {}).get("chunk", {})
        if event.get("type") == "assistant/chunk" and chunk.get("type") == "usage":
            for key, value in (chunk.get("usage") or {}).items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
    documents = read_jsonl(browser.directory / "navigation.jsonl") if browser else []
    documents += [row for row in events if row.get("type") == "native_web_document"
                  and row.get("body_kind") == "html" and not row.get("truncated")]
    match = next((row for row in documents if row.get("type") in {"document", "native_web_document"}
                  and row.get("url") == request["input"]["content"] and 200 <= row.get("status", 0) < 300), None)
    count = sum(row.get("type") == "request" for row in provider)
    native_calls = sum(e.get("type") == "tool/call" for e in events)
    observed_calls = {(e.get("session_id"), e.get("call_id")) for e in events
                      if e.get("type") in {"native_tool_attempt", "native_tool_result"}}
    native_calls = max(native_calls, len(observed_calls))
    tools = max(native_calls, sum(row.get("type") == "tool_start" for row in bridge))
    budget_stop = next((row for row in bridge + provider + events if row.get("type") == "budget_stop"), None)
    representations = set()
    for item in trace:
        if item["is_error"]:
            continue
        name = str(item["name"])
        blocks = (item.get("output") or {}).get("content", [])
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if name == "web_search":
            representations.add("native_web_search_event")
            if blocks:
                representations.add("native_web_search_results")
        if name == "web_fetch":
            representations.add("native_web_fetch_rendered_text")
        if name in {"read", "read_image"}:
            representations.add("native_file_read")
        if not name.startswith("mcp__") and name not in {"web_search", "web_fetch"}:
            representations.add("native_tool_result")
        if "### Snapshot" in text or "- Page Snapshot:" in text:
            representations.add("playwright_mcp_accessibility_snapshot")
        if "[Snapshot](" in text:
            representations.add("playwright_mcp_snapshot_path")
        if name.endswith("__browser_navigate"):
            representations.add("navigation_status")
        if name.endswith(("__browser_evaluate", "__browser_run_code")):
            representations.add("dom_javascript_result")
        if name.endswith("__browser_read_artifact"):
            representations.add("browser_artifact_content")
        if any(block.get("type") == "image" for block in blocks):
            representations.add("screenshot_image")
    response = {"schema_version": PROTOCOL, "adapter_version": VERSION, "harness_type": harness,
                "model": request["target"]["model"], "status": "completed", "final_answer": answer,
                "finish_reason": "stop" if settled else None, "trace": trace, "usage": usage,
                "model_requests": count, "tool_calls": tools, "retryable": False,
                "model_request_budget": {"unit": "http_request", "limit": request["tool_config"]["max_model_requests"], "used": count},
                "tool_budget": {"unit": "tool_call", "limit": request["tool_config"]["max_tool_calls"],
                                "threshold": request["tool_config"]["max_tool_calls"],
                                "used": tools, "exceeded": tools > request["tool_config"]["max_tool_calls"],
                                "excess": max(0, tools - request["tool_config"]["max_tool_calls"]),
                                "enforcement": "observe_only"},
                "observation_representations": sorted(representations), "native_exit_code": exit_code,
                "tool_policy": DSH_ACCESS_POLICY,
                "tool_policy_violations": [],
                "native_events_path": str(evidence / "events.jsonl"),
                "browser_transport_trace_path": str(evidence / "browser-events.jsonl"),
                "provider_evidence_dir": str(evidence / "provider"), "provider_image_delivery": "see_archived_request_bodies"}
    audit_path = evidence / "access-audit.json"
    write_json(audit_path, dsh_access_audit(events, trace, browser))
    response.update(native_web_tools_policy=DSH_WEB_TOOLS_POLICY,
                    input_policy=DSH_ACCESS_POLICY, page_verification_status="unknown",
                    access_audit_path=str(audit_path),
                    native_model_provider=dsh_provider(request),
                    native_search_binding=dsh_search_target(request),
                    native_web_documents=[row for row in documents if row.get("type") == "native_web_document"])
    response["provider_request_counts"] = {
        kind: sum(row.get("type") == "request" and row.get("request_kind", "agent_model") == kind for row in provider)
        for kind in ("agent_model", "native_web_search", "native_file_api")}
    response["native_warnings"] = [e["item"]["message"] for e in events if e.get("item", {}).get("type") == "error"]
    response["transport_errors"] = [row for row in provider if row.get("type") == "transport_error"]
    if match:
        response.update(visited_url=match["url"],
                        page_verification_source=match.get("source", "playwright_mcp_document"),
                        page_verification_status="target_document_observed")
    if budget_stop:
        response.update(status="failed", error=budget_stop["reason"])
    elif error or exit_code != 0 or not settled or any(e.get("type") == "malformed_event" for e in events):
        known = [row for row in provider if row.get("type") == "response" and row["status"] >= 400]
        response.update(status="failed" if known or count == 0 else "unresolved",
                        error=error or (f"provider_http_{known[-1]['status']}" if known else "native_agent_did_not_complete"),
                        retryable=bool(known and (known[-1]["status"] in {408, 429} or known[-1]["status"] >= 500)))
    elif not answer.strip():
        response.update(status="failed", error="missing_final_answer")
    elif re.search(r"<[｜|]*DSML[｜|]*\s*(?:calls|function_calls|invoke)\b", answer):
        response.update(status="unresolved", error="unfinished_tool_call_in_final_answer")
    return response


@isolated_runtime
def run_adapter(request_path, response_path, *, container=False):
    request = json.loads(Path(request_path).read_text())
    if request["experiment"]["harness_type"] != "dsh":
        raise ValueError("This adapter is for DSH only")
    validate_request(request, container=container, container_inner=container)
    response_path = Path(response_path).resolve()
    base, harness = response_path.parent, request["experiment"]["harness_type"]
    evidence = base / f"{harness}-evidence"
    if response_path.exists() or evidence.exists():
        raise ValueError("Refusing to overwrite a native harness attempt")
    evidence.mkdir(parents=True)
    settings, target = request["tool_config"], request["target"]
    # Work outside the repository, so ancestor project instructions cannot enter
    # the subject. Config homes are per attempt.
    temporary = tempfile.TemporaryDirectory(prefix="session-", dir=response_path.parent)
    work = Path(temporary.name)
    config_home = work / "config"
    config_home.mkdir()
    node = shutil.which("node")
    browser = proxy = agent = None
    failure = None
    started = time.monotonic()
    previous = {}
    def interrupted(signum, _frame):
        raise InterruptedError(f"Native adapter interrupted by signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, interrupted)
    try:
        modules = Path(settings["dsh_modules"])
        if json.loads((modules / "@deepseek-ai/dsh/package.json").read_text())["version"] != DSH_VERSION:
            raise ValueError(f"Expected DSH {DSH_VERSION}")
        from defipi.config import load_local_env
        load_local_env()
        key = os.environ.get(target["client_settings"]["api_key_env"])
        if not key:
            raise ValueError(f"{target['client_settings']['api_key_env']} is unavailable")
        search_target = dsh_search_target(request)
        search_key = os.environ.get(search_target["client_settings"]["api_key_env"])
        if not search_key:
            raise ValueError(f"{search_target['client_settings']['api_key_env']} is required for the native DSH search interface")
        native_search = {"target": search_target, "api_key": search_key}
        proxy = NativeModelProxy(target, key, evidence / "provider", settings["max_model_requests"],
                                 connect_timeout_seconds=settings["proxy_connect_timeout_seconds"], preserve_native_parameters=False, native_search=native_search,
                                 native_files=dsh_provider(request) == "deepseek-official").start()
        browser = PlaywrightMcpSession(base / "browser", node=node, chrome=settings["chrome_executable"], startup_timeout_seconds=settings["mcp_startup_timeout_seconds"])
        browser.start()
        # Native read resolves MCP's artifacts/... links from the session cwd.
        (work / "artifacts").symlink_to(browser.directory / "artifacts", target_is_directory=True)
        bridge_path = evidence / "browser-config.json"
        write_json(bridge_path, {"mcp_url": browser.session["mcp_url"], "mcp_sdk": settings["mcp_sdk"],
                                 "artifacts_dir": str(browser.directory / "artifacts"),
                                 "events_path": str(evidence / "browser-events.jsonl"), "tool_timeout_seconds": settings["mcp_tool_timeout_seconds"], "max_tool_calls": settings["max_tool_calls"]})
        prompt = prompt_for(request)
        (evidence / "prompt.txt").write_text(prompt)
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        env = session_environment(env, work)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["LOCAL_PROXY_KEY"] = "local-proxy-only"
        env.update(DSH_HOME=str(config_home), DSH_AGENTS_HOME=str(config_home / "agents"),
                   DSH_TELEMETRY_DISABLED="1")
        patch_path = evidence / "dsh.patch.json"
        write_json(patch_path, dsh_patch(request, proxy.base_url, bridge_path, work))
        selection = {"provider": dsh_provider(request), "model": target["model"]}
        if target.get("reasoning_effort"):
            selection["reasoningEffort"] = target["reasoning_effort"]
        if target.get("max_response_tokens"):
            selection["maxTokens"] = target["max_response_tokens"]
        session_config = evidence / "dsh-config.json"
        write_json(session_config, {"dsh_modules": str(modules), "patch_path": str(patch_path),
                                    "selection": selection, "prompt": prompt,
                                    "max_tool_calls": settings["max_tool_calls"],
                                    "events_path": str(evidence / "events.jsonl"), "result_path": str(evidence / "dsh-result.json")})
        command = [node, str(runtime_script(RUNTIME / "dsh_session.mjs", evidence)), str(session_config)]
        write_json(evidence / "invocation.json", {"argv": command, "cwd": str(work), "harness": harness,
                                                   "adapter_version": VERSION, "model_binding": target,
                                                   "system_prompt_policy": SYSTEM_PROMPT_POLICY})
        with (evidence / "stdout.log").open("wb") as stdout, (evidence / "stderr.log").open("wb") as stderr:
            agent = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                                     stdout=stdout, stderr=stderr, start_new_session=True)
            agent.communicate(input=b"", timeout=settings["trajectory_timeout_seconds"])
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        stop_process(agent)
        cleaned = browser.close() if browser else True
        if proxy:
            proxy.close()
        for name in ("sessions", "attachments"):
            if (config_home / name).exists():
                shutil.copytree(config_home / name, evidence / "dsh-home" / name)
        shutil.copytree(work, evidence / "workspace-artifacts", symlinks=True,
                        ignore=shutil.ignore_patterns("config", "home", "cache", "data", "state", "tmp"))
        workspace_mapping(evidence, work)
        response = collect_result(request, evidence, browser, agent.returncode if agent else None, failure)
        response.update(browser_closed=cleaned, wall_seconds=round(time.monotonic() - started, 3),
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        system_prompt_policy=SYSTEM_PROMPT_POLICY,
                        harness_build=f"dsh@{DSH_VERSION}")
        if not cleaned:
            response.update(status="unresolved", error="browser_cleanup_incomplete", retryable=False)
        trajectory_path = evidence / "trajectory.json"
        write_json(trajectory_path, {"schema_version": "native-agent-trajectory-v1", "answer": response["final_answer"],
                                     "model_requests": response["model_requests"],
                                     "tool_calls": response["tool_calls"], "tool_budget": response["tool_budget"],
                                     "native_events_path": response["native_events_path"], "tools": response["trace"],
                                     "browser_transport_trace_path": response["browser_transport_trace_path"],
                                     "provider_evidence_dir": response["provider_evidence_dir"],
                                     "access_audit_path": response["access_audit_path"]})
        response["trajectory_path"] = str(trajectory_path)
        write_json(response_path, response)
        temporary.cleanup()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return response


def adapter_config(harness, *, tool_config, timeout_seconds, max_model_requests):
    if harness != "dsh":
        raise ValueError("This adapter is for DSH only")
    sdk = DSH_MODULES / "@modelcontextprotocol/sdk"
    settings = {**tool_config,
                "max_model_requests": max_model_requests,
                "mcp_sdk": str(sdk), "input_policy": DSH_ACCESS_POLICY,
                "system_prompt_policy": SYSTEM_PROMPT_POLICY}
    settings.update(dsh_modules=str(DSH_MODULES),
                    native_web_tools_policy=DSH_WEB_TOOLS_POLICY)
    return {"harness_type": harness, "build": f"{VERSION}/{harness}@{DSH_VERSION}",
            "command": [sys.executable, str(ROOT / "cli/internal/native_agent_adapter.py"), "--request", "{request_path}", "--response", "{response_path}"],
            "supported_inputs": ["url"],
            "timeout_seconds": timeout_seconds + 90, "tool_config": settings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request")
    parser.add_argument("--response")
    parser.add_argument("--print-binding", choices=("dsh",))
    args = parser.parse_args(argv)
    if args.print_binding:
        print(json.dumps(adapter_config(args.print_binding), ensure_ascii=False, indent=2))
        return 0
    if not args.request or not args.response:
        parser.error("--request and --response are required")
    run_adapter(args.request, args.response)
    return 0
