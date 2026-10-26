"""Codex CLI URL/OFF adapter using native tools plus official Playwright MCP."""

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from defipi.execution.session_runtime import isolated_runtime, session_environment, runtime_script, workspace_mapping
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from defipi.execution.agents import common
PROXY_TOKEN = "local-proxy-only"
from defipi.execution.agents.tool_evidence import (
    ACCESS_POLICY,
    access_audit,
    content_blocks,
    native_wire_trace,
    target_url_evidence,
)
from defipi.execution.api.native_model_proxy import NativeModelProxy
from defipi.execution.agents.playwright import (
    PlaywrightMcpSession,
    start_browser,
)

ROOT = _PACKAGE_ROOT
VERSION = "codex-native-url-v6-tool-count-observe-only/session-v1"
TOOLS_POLICY = "runtime-defaults-native-tools-with-playwright-mcp"


def validate_request(request, *, container=False, container_inner=False):
    if request["experiment"].get("harness_type") != "codex":
        raise ValueError("This adapter is for Codex CLI only")
    settings = request["tool_config"]
    if settings.get("native_web_tools_policy") != TOOLS_POLICY:
        raise ValueError("Bind the Codex native-tools plus official Playwright MCP policy")
    if settings.get("input_policy") != ACCESS_POLICY:
        raise ValueError("Bind the Codex post-run access audit policy")
    common.validate_request(request, container=container, container_inner=container_inner)
    if not Path(settings["mcp_sdk"]).joinpath("dist/esm/client/index.js").is_file():
        raise ValueError("The configured MCP SDK is unavailable")


def prompt_for(request):
    return request["question"] + "\n\nPage URL: " + request["input"]["content"]

def codex_config(request, proxy_url):
    target = request["target"]
    quote = json.dumps
    sandbox = "workspace-write"
    lines = [f"model = {quote(target['model'])}", 'model_provider = "local"',
             'approval_policy = "never"', f"sandbox_mode = {quote(sandbox)}",
             "check_for_update_on_startup = false"]
    if target.get("reasoning_effort"):
        lines.append(f"model_reasoning_effort = {quote(target['reasoning_effort'])}")
    # Transport overrides preserve JSON request recording and the retry budget.
    # Native mode leaves tool discovery at CLI defaults.
    lines += ["[sandbox_workspace_write]", "network_access = true", "[features]"]
    for feature in ("enable_request_compression", "unbounded_connection_retries"):
        lines.append(f"{feature} = false")
    lines += ["[model_providers.local]", 'name = "Local provider"',
              f"base_url = {quote(proxy_url)}", 'wire_api = "responses"',
              'env_key = "LOCAL_PROXY_KEY"',
              "request_max_retries = 0", "stream_max_retries = 0", "supports_websockets = false"]
    return "\n".join(lines) + "\n"


def playwright_config(request, proxy_url, observer_path, work):
    quote = json.dumps
    return codex_config(request, proxy_url) + "\n".join([
        "[mcp_servers.playwright]", f"command = {quote(shutil.which('node'))}",
        "args = " + quote([str(runtime_script(common.RUNTIME / "browser_observer.mjs", Path(observer_path).parent)), str(observer_path)]),
        f"cwd = {quote(str(work))}", "required = true", 'default_tools_approval_mode = "approve"',
        f"startup_timeout_sec = {request['tool_config']['mcp_startup_timeout_seconds']}",
        f"tool_timeout_sec = {request['tool_config']['mcp_tool_timeout_seconds']}", "",
    ])


def collect_result(request, evidence, exit_code, error=None):
    events = common.read_jsonl(evidence / "events.jsonl")
    provider = common.read_jsonl(evidence / "provider/events.jsonl")
    trace = common.native_trace("codex", events)
    tool_ids = set()
    for line, event in enumerate(events, 1):
        item = event.get("item") or {}
        kind = item.get("type", "")
        if event.get("type") in {"item.started", "item.completed"} and kind in {
                "command_execution", "file_change", "mcp_tool_call", "web_search"} and item.get("id"):
            tool_ids.add(item["id"])
        if event.get("type") != "item.completed" or kind not in {"command_execution", "file_change"}:
            continue
        trace.append({"type": "tool", "name": kind, "call_id": item.get("id"),
                      "input": {"command": item.get("command")} if kind == "command_execution" else item.get("changes", []),
                      "output": {"content": content_blocks(item.get("aggregated_output", item.get("changes", [])))},
                      "is_error": item.get("status") == "failed" or item.get("exit_code", 0) not in (0, None),
                      "event_line": line, "native_item": item, "source": "native_cli_event"})
    trace += native_wire_trace(evidence)
    completed = [e for e in events if e.get("type") == "turn.completed"]
    settled = bool(completed) and not any(e.get("type") in {"turn.failed", "malformed_event"} for e in events)
    usage = {}
    for event in completed:
        for key, value in (event.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    final = evidence / "last-message.txt"
    answer = final.read_text() if final.exists() else ""
    count = sum(e.get("type") == "request" for e in provider)
    tool_count = max(len(tool_ids), common.codex_wire_tool_count(evidence))
    url_evidence = target_url_evidence(trace, request["input"]["content"])
    representations = {"codex_native_tool_output" for t in trace
                       if t.get("output_observed", True) and not t.get("is_error")}
    if any(t.get("name") == "command_execution" for t in trace):
        representations.add("native_command_output")
    if any(any(b.get("type") == "image" for b in t.get("output", {}).get("content", [])) for t in trace):
        representations.add("native_image_output")
    threshold = request["tool_config"]["max_tool_calls"]
    response = {
        "schema_version": common.PROTOCOL, "adapter_version": VERSION, "harness_type": "codex",
        "model": request["target"]["model"], "status": "completed", "finish_reason": "stop" if settled else None,
        "final_answer": answer, "trace": trace, "usage": usage, "model_requests": count, "tool_calls": tool_count,
        "retryable": False,
        "model_request_budget": {"unit": "http_request", "limit": request["tool_config"]["max_model_requests"], "used": count},
        "tool_budget": {"unit": "tool_call", "limit": threshold, "threshold": threshold,
                        "used": tool_count, "exceeded": tool_count > threshold,
                        "excess": max(0, tool_count - threshold), "enforcement": "observe_only"},
        "observation_representations": sorted(representations), "native_exit_code": exit_code,
        "native_web_tools_policy": TOOLS_POLICY, "system_prompt_policy": common.SYSTEM_PROMPT_POLICY,
        "input_policy": ACCESS_POLICY,
        "native_events_path": str(evidence / "events.jsonl"), "provider_evidence_dir": str(evidence / "provider"),
        "browser_transport_trace_path": None, "playwright_mcp_enabled": False,
        "native_url_evidence": url_evidence, "page_verification_source": "codex_native_tool_trace",
        "page_verification_status": "native_url_observed" if url_evidence else "unknown",
        "authentication_mode": request["target"].get("auth_mode", "api_key"),
        "billing_mode": "provider_api",
        "reasoning_effort": request["target"].get("reasoning_effort"),
        "transport_errors": [e for e in provider if e.get("type") == "transport_error"],
    }
    if url_evidence:
        response["visited_url"] = request["input"]["content"]
    budget = next((e for e in provider if e.get("type") == "budget_stop"), None)
    if budget:
        response.update(status="failed", error=budget["reason"])
    elif error or exit_code != 0 or not settled:
        known = [e for e in provider if e.get("type") == "response" and e["status"] >= 400]
        response.update(status="failed" if known or count == 0 else "unresolved",
                        error=error or (f"provider_http_{known[-1]['status']}" if known else "native_agent_did_not_complete"))
    elif not answer.strip():
        response.update(status="failed", error="missing_final_answer")
    # Access findings, including absent URL evidence or outside-workspace paths,
    # are reviewed separately from whether the native turn completed.
    invocation_path = evidence / "invocation.json"
    invocation = json.loads(invocation_path.read_text()) if invocation_path.exists() else {}
    audit_path = evidence / "access-audit.json"
    common.write_json(audit_path, access_audit(trace, invocation.get("cwd"), request["input"]["content"]))
    response["access_audit_path"] = str(audit_path)
    return response


def collect_playwright_result(request, evidence, browser, exit_code, error=None):
    response = collect_result(request, evidence, exit_code, error)
    observer = common.read_jsonl(evidence / "browser-events.jsonl")
    browser_calls = sum(row.get("type") == "tool_start" for row in observer)
    browser_trace = [row for row in observer if row.get("type") in {"tool_end", "transport_error"}]
    wire_calls = [tool for tool in response["trace"] if tool.get("source") == "native_responses_tool"]
    direct_mcp_calls = sum(str(tool["name"]).startswith("mcp__playwright__") for tool in wire_calls)
    response["tool_calls"] = max(response["tool_calls"], len(wire_calls) + browser_calls - direct_mcp_calls)
    threshold = request["tool_config"]["max_tool_calls"]
    response["tool_budget"].update(used=response["tool_calls"],
                                   exceeded=response["tool_calls"] > threshold,
                                   excess=max(0, response["tool_calls"] - threshold))
    response.update(playwright_mcp_enabled=True, browser_tool_calls=browser_calls,
                    browser_transport_trace_path=str(evidence / "browser-events.jsonl"),
                    browser_tool_trace=browser_trace, browser=dict(browser.session or {}) if browser else None)
    # Record nested MCP calls even when the native CLI only emits their code-mode wrapper.
    response["trace"] += [{"type": "tool", "name": row["name"], "call_id": row["call_id"],
                           "input": row.get("input", {}), "output": row.get("output", {"content": []}),
                           "output_observed": row["type"] == "tool_end",
                           "is_error": row.get("is_error", row["type"] == "transport_error"),
                           "transport_error": row.get("error"), "source": "playwright_mcp_observer",
                           "evidence_path": response["browser_transport_trace_path"]}
                          for row in browser_trace]
    response["native_url_evidence"] = target_url_evidence(response["trace"], request["input"]["content"])
    documents = common.read_jsonl(browser.directory / "navigation.jsonl") if browser else []
    match = next((row for row in documents if row.get("type") == "document"
                  and row.get("url") == request["input"]["content"] and 200 <= row.get("status", 0) < 300), None)
    if match:
        response.update(visited_url=match["url"],
                        page_verification_source="playwright_mcp_document",
                        page_verification_status="browser_document_observed")
    elif response["native_url_evidence"]:
        response.update(visited_url=request["input"]["content"], page_verification_status="native_url_observed")
    if any(row["type"] == "tool_end" and not row.get("is_error") for row in browser_trace):
        response["observation_representations"].append("playwright_mcp_tool_output")
    audit_path = Path(response["access_audit_path"])
    original_audit = json.loads(audit_path.read_text())
    audit = access_audit(response["trace"], original_audit["working_directory"], request["input"]["content"])
    audit.update(browser_transport_trace_path=response["browser_transport_trace_path"],
                 page_verification_status=response["page_verification_status"])
    common.write_json(audit_path, audit)
    return response


@isolated_runtime
def run_adapter(request_path, response_path, *, container=False):
    request = json.loads(Path(request_path).read_text())
    validate_request(request, container=container, container_inner=container)
    version = VERSION
    response_path = Path(response_path).resolve()
    evidence = response_path.parent / "codex-evidence"
    if response_path.exists() or evidence.exists():
        raise ValueError("Refusing to overwrite a native Codex attempt")
    evidence.mkdir(parents=True)
    settings, target = request["tool_config"], request["target"]
    temporary = tempfile.TemporaryDirectory(prefix="session-", dir=response_path.parent)
    root = Path(temporary.name)
    work, config_home = root / "workspace", root / "config"
    work.mkdir()
    config_home.mkdir(mode=0o700)
    proxy = agent = browser = None
    failure = None
    started = time.monotonic()
    previous = {}
    def interrupted(signum, _frame):
        raise InterruptedError(f"Native adapter interrupted by signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, interrupted)
    try:
        executable = settings["codex_executable"]
        actual = subprocess.check_output([executable, "--version"], text=True).strip()
        if actual != "codex-cli " + common.CODEX_VERSION:
            raise ValueError(f"Expected Codex CLI {common.CODEX_VERSION}; got {actual}")
        from defipi.config import load_local_env
        load_local_env()
        key = os.environ.get(target["client_settings"]["api_key_env"])
        if not key:
            raise ValueError(f"{target['client_settings']['api_key_env']} is unavailable")
        proxy = NativeModelProxy(target, key, evidence / "provider", settings["max_model_requests"],
                connect_timeout_seconds=settings["proxy_connect_timeout_seconds"], preserve_native_parameters=False).start()
        prompt = prompt_for(request)
        (evidence / "prompt.txt").write_text(prompt)
        browser, observer_path = start_browser(request, evidence)
        config_text = playwright_config(request, proxy.base_url, observer_path, work)
        (config_home / "config.toml").write_text(config_text)
        (evidence / "config.toml").write_text(config_text)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
        env = session_environment(env, root)
        env.update(CODEX_HOME=str(config_home), NO_PROXY="127.0.0.1,localhost")
        env["LOCAL_PROXY_KEY"] = PROXY_TOKEN
        command = [executable, "exec", "--strict-config", "--skip-git-repo-check", "--ephemeral", "--json",
                   "--color", "never", "-C", str(work), "--output-last-message", str(evidence / "last-message.txt"), "-"]
        common.write_json(evidence / "invocation.json", {"argv": command, "cwd": str(work), "harness": "codex",
                          "adapter_version": version, "model_binding": target,
                          "authentication_mode": target.get("auth_mode", "api_key"),
                          "native_web_tools_policy": settings["native_web_tools_policy"],
                          "input_policy": settings["input_policy"],
                          "system_prompt_policy": common.SYSTEM_PROMPT_POLICY, "playwright_mcp_enabled": True})
        with (evidence / "events.jsonl").open("wb") as stdout, (evidence / "stderr.log").open("wb") as stderr:
            agent = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                                     stdout=stdout, stderr=stderr, start_new_session=True)
            agent.communicate(input=prompt.encode(), timeout=settings["trajectory_timeout_seconds"])
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        common.stop_process(agent)
        cleaned = browser.close() if browser else True
        if proxy:
            proxy.close()
        try:
            # Only task artifacts are archived; the adjacent config home is private.
            if work.exists():
                shutil.copytree(work, evidence / "workspace-artifacts", symlinks=True)
                workspace_mapping(evidence, work)
            response = collect_playwright_result(request, evidence, browser, agent.returncode if agent else None, failure)
            response.update(wall_seconds=round(time.monotonic() - started, 3),
                            completed_at=datetime.now(timezone.utc).isoformat(), harness_build=f"codex-cli@{common.CODEX_VERSION}",
                            native_process_exited=agent is None or agent.returncode is not None,
                            browser_closed=cleaned, browser_lifecycle="per-attempt official Playwright MCP")
            if not cleaned:
                response.update(status="unresolved", error="browser_cleanup_incomplete", retryable=False)
            trajectory = evidence / "trajectory.json"
            common.write_json(trajectory, {"schema_version": "native-agent-trajectory-v1", "answer": response["final_answer"],
                                          "tools": response["trace"], "native_events_path": response["native_events_path"],
                                          "provider_evidence_dir": response["provider_evidence_dir"],
                                          "access_audit_path": response["access_audit_path"],
                                          "browser_transport_trace_path": response["browser_transport_trace_path"],
                                          "page_verification_status": response["page_verification_status"]})
            response["trajectory_path"] = str(trajectory)
        finally:
            temporary.cleanup()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        common.write_json(response_path, response)
    return response


def adapter_config(*, tool_config, timeout_seconds, max_model_requests):
    sdk = common.NODE_MODULES / "@modelcontextprotocol/sdk"
    binding = {"harness_type": "codex", "build": f"{VERSION}/codex@{common.CODEX_VERSION}",
            "command": [sys.executable, str(ROOT / "cli/internal/codex_native_agent_adapter.py"),
                        "--request", "{request_path}", "--response", "{response_path}"],
            "supported_inputs": ["url"],
            "timeout_seconds": timeout_seconds + 90,
            "tool_config": {**tool_config,
                            "max_model_requests": max_model_requests, "codex_executable": common.native_executable("codex"),
                            "native_web_tools_policy": TOOLS_POLICY,
                            "system_prompt_policy": common.SYSTEM_PROMPT_POLICY, "input_policy": ACCESS_POLICY,
                            "mcp_sdk": str(sdk)}}
    return binding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args(argv)
    run_adapter(args.request, args.response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
