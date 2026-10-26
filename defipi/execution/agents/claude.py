"""Claude Code CLI's native loop, full permissions in a disposable container."""

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT
from collections import Counter
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
from defipi.execution.session_runtime import isolated_runtime, session_environment, runtime_script, workspace_mapping
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from defipi.execution.agents import common
from defipi.execution.agents.playwright import start_browser
from defipi.execution.agents.tool_evidence import ACCESS_POLICY, access_audit, content_blocks
from defipi.execution.agents.tool_evidence import target_url_evidence as common_url_evidence
from defipi.execution.api.native_model_proxy import NativeModelProxy

ROOT = _PACKAGE_ROOT
VERSION = "claude-native-url-v3-tool-count-observe-only/session-v1"
CLAUDE_VERSION = "2.1.293"
TOOLS_POLICY = "runtime-defaults-native-tools-with-playwright-mcp"
PERMISSION_MODE = "bypassPermissions"
ANTHROPIC_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def validate_request(request, *, container=False, container_inner=False):
    if request["experiment"].get("harness_type") != "claude":
        raise ValueError("This adapter is for Claude Code CLI")
    common.validate_request(request, container=container, container_inner=container_inner)
    settings, target = request["tool_config"], request["target"]
    if target.get("api") != "anthropic-messages":
        raise ValueError("Claude Code requires the bound Anthropic Messages API")
    if target.get("reasoning_effort") not in ANTHROPIC_EFFORT_LEVELS:
        raise ValueError("Bind an explicit native Claude effort level")
    if settings.get("native_web_tools_policy") != TOOLS_POLICY or settings.get("input_policy") != ACCESS_POLICY:
        raise ValueError("Bind native tools with Playwright and post-run access auditing")


def prompt_for(request):
    return request["question"] + "\n\nPage URL: " + request["input"]["content"]


def cli_environment(request, proxy_url, home, config_home):
    model = request["target"]["model"]
    # The suffix is a CLI context hint; the provider still receives the exact ID.
    primary_model = model + "[1m]" if request["tool_config"]["use_1m_context"] else model
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(config_home),
               ANTHROPIC_BASE_URL=proxy_url.removesuffix("/v1"), ANTHROPIC_AUTH_TOKEN="local-proxy-only",
               ANTHROPIC_MODEL=primary_model, ANTHROPIC_DEFAULT_OPUS_MODEL=primary_model,
               ANTHROPIC_DEFAULT_SONNET_MODEL=primary_model, ANTHROPIC_DEFAULT_HAIKU_MODEL=model,
               CLAUDE_CODE_SUBAGENT_MODEL=model, CLAUDE_CODE_EFFORT_LEVEL=request["target"]["reasoning_effort"],
               CLAUDE_CODE_MAX_RETRIES="0",
               DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1",
               NO_PROXY="127.0.0.1,localhost,host.docker.internal")
    compact_window = request["tool_config"]["auto_compact_window"]
    if compact_window is not None:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(compact_window)
    return env


def cli_command(executable, mcp_config):
    return [executable, "-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--forward-subagent-text",
            "--permission-mode", PERMISSION_MODE, "--mcp-config", str(mcp_config)]


def response_blocks(path):
    """Reconstruct native Anthropic blocks, including provider-side search tools."""
    raw = path.read_text(errors="replace")
    if raw.lstrip().startswith("{"):
        try:
            return json.loads(raw).get("content", [])
        except ValueError:
            return []
    blocks, partial = {}, {}
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        index = event.get("index")
        if event.get("type") == "content_block_start":
            blocks[index] = dict(event["content_block"])
        elif event.get("type") == "content_block_delta" and index in blocks:
            delta = event.get("delta", {})
            if delta.get("type") == "input_json_delta":
                partial[index] = partial.get(index, "") + delta.get("partial_json", "")
            elif delta.get("type") == "text_delta":
                blocks[index]["text"] = blocks[index].get("text", "") + delta.get("text", "")
    for index, raw_input in partial.items():
        try:
            blocks[index]["input"] = json.loads(raw_input)
        except ValueError:
            blocks[index]["partial_input"] = raw_input
    return list(blocks.values())


def native_trace(events, evidence):
    calls, outputs = {}, {}

    def ingest(blocks, source, path, *, returned=False, parent=None):
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type", "")
            if kind in {"tool_use", "server_tool_use"} and block.get("id"):
                key = block["id"]
                calls[key] = {**calls.get(key, {}), **block, "source": source, "evidence_path": str(path),
                              "parent_tool_use_id": parent or calls.get(key, {}).get("parent_tool_use_id")}
            elif kind.endswith("tool_result") and block.get("tool_use_id"):
                key = block["tool_use_id"]
                outputs[key] = {**block, "returned_to_model": returned or outputs.get(key, {}).get("returned_to_model", False),
                                "evidence_path": str(path)}

    for event in events:
        if event.get("type") in {"assistant", "user"}:
            ingest((event.get("message") or {}).get("content"), "native_cli_event", evidence / "events.jsonl",
                   parent=event.get("parent_tool_use_id"))
    provider = evidence / "provider"
    for path in sorted(provider.glob("*.response.body")):
        ingest(response_blocks(path), "native_messages_tool", path)
    for path in sorted(provider.glob("*.request.json")):
        body = json.loads(path.read_text())
        for message in body.get("messages", []):
            ingest(message.get("content"), "native_messages_tool", path, returned=True)
    trace = []
    for call_id, call in calls.items():
        result = outputs.get(call_id)
        trace.append({"type": "tool", "name": call["name"], "call_id": call_id,
                      "input": call.get("input", call.get("partial_input", {})),
                      "output": {"content": content_blocks(result.get("content", [])) if result else []},
                      "output_observed": result is not None, "returned_to_model": bool(result and result["returned_to_model"]),
                      "is_error": bool(result and result.get("is_error")), "native_item": call,
                      "source": call["source"], "evidence_path": call["evidence_path"],
                      "output_evidence_path": result["evidence_path"] if result else None,
                      "parent_tool_use_id": call.get("parent_tool_use_id")})
    return trace


def target_url_evidence(trace, target):
    # Reuse the common URL audit, including its HTTP-command evidence rule.
    normalized = []
    for tool in trace:
        if tool["name"] == "Bash" and isinstance(tool["input"], dict) and tool.get("output_observed"):
            tool = {**tool, "name": "command_execution", "native_item": {
                "command": tool["input"].get("command", ""), "exit_code": 1 if tool.get("is_error") else 0,
                "aggregated_output": "\n".join(b.get("text", "") for b in tool["output"]["content"])}}
        normalized.append(tool)
    matches = common_url_evidence(normalized, target)
    names = {tool["call_id"]: tool["name"] for tool in trace}
    return [{**row, "tool": names[row["call_id"]]} for row in matches]


def collect_result(request, evidence, browser, exit_code, error=None):
    events = common.read_jsonl(evidence / "events.jsonl")
    provider = common.read_jsonl(evidence / "provider/events.jsonl")
    trace = native_trace(events, evidence)
    observer = common.read_jsonl(evidence / "browser-events.jsonl")
    # Match occurrences, not a set of arguments: identical repeated calls count.
    def signature(name, args):
        return name, json.dumps(args, sort_keys=True, ensure_ascii=False)
    direct = Counter(signature(t["name"].removeprefix("mcp__playwright__"), t["input"])
                     for t in trace if t["name"].startswith("mcp__playwright__"))
    unmatched = set()
    for row in observer:
        if row.get("type") != "tool_start":
            continue
        key = signature(row["name"], row.get("input", {}))
        if direct[key]:
            direct[key] -= 1
        else:
            unmatched.add(row["call_id"])
    tool_count = len(trace) + len(unmatched)
    for row in observer:
        if row.get("call_id") in unmatched and row.get("type") in {"tool_end", "transport_error"}:
            trace.append({"type": "tool", "name": row["name"], "call_id": row["call_id"],
                          "input": row.get("input", {}), "output": row.get("output", {"content": []}),
                          "output_observed": row["type"] == "tool_end",
                          "is_error": row.get("is_error", row["type"] == "transport_error"),
                          "source": "playwright_mcp_observer", "evidence_path": str(evidence / "browser-events.jsonl")})
    results = [e for e in events if e.get("type") == "result"]
    result = results[-1] if results else {}
    answer = result.get("result", "")
    count = sum(e.get("type") == "request" for e in provider)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    response = {"schema_version": common.PROTOCOL, "harness_type": "claude", "adapter_version": VERSION,
                "model": request["target"]["model"], "status": "completed", "final_answer": answer,
                "finish_reason": "stop", "trace": trace, "usage": result.get("usage") or {},
                "model_requests": count, "tool_calls": tool_count, "retryable": False,
                "tool_budget": {"unit": "tool_call", "limit": request["tool_config"]["max_tool_calls"],
                                "threshold": request["tool_config"]["max_tool_calls"],
                                "used": tool_count, "exceeded": tool_count > request["tool_config"]["max_tool_calls"],
                                "excess": max(0, tool_count - request["tool_config"]["max_tool_calls"]),
                                "enforcement": "observe_only"},
                "observation_representations": sorted({"claude_native_tool_output" for t in trace if t.get("output_observed")}),
                "native_exit_code": exit_code, "native_result_subtype": result.get("subtype"),
                "native_web_tools_policy": TOOLS_POLICY, "system_prompt_policy": common.SYSTEM_PROMPT_POLICY,
                "input_policy": ACCESS_POLICY, "permission_mode": init.get("permissionMode"),
                "native_events_path": str(evidence / "events.jsonl"), "provider_evidence_dir": str(evidence / "provider"),
                "playwright_mcp_enabled": True, "browser_tool_calls": sum(e.get("type") == "tool_start" for e in observer),
                "browser_transport_trace_path": str(evidence / "browser-events.jsonl"),
                "browser": dict(browser.session or {}) if browser else None,
                "native_url_evidence": target_url_evidence(trace, request["input"]["content"]),
                "page_verification_status": "unknown",
                "page_verification_source": "claude_native_tool_trace",
                "reasoning_effort": request["target"]["reasoning_effort"],
                "native_model_usage": result.get("modelUsage", {}),
                "native_cost_estimate_usd": result.get("total_cost_usd"),
                "cost_basis": "Claude CLI estimate, not a provider invoice",
                "transport_errors": [e for e in provider if e.get("type") in {"transport_error", "stream_error"}]}
    known = [e for e in provider if e.get("type") == "response" and e["status"] >= 400]
    budget = next((e for e in provider if e.get("type") == "budget_stop"), None)
    settled = result.get("subtype") == "success" and not result.get("is_error")
    mcp_connected = any(s.get("name") == "playwright" and s.get("status") == "connected" for s in init.get("mcp_servers", []))
    if budget:
        response.update(status="failed", error=budget["reason"])
    elif known:
        response.update(status="failed", error=f"provider_http_{known[-1]['status']}")
    elif error or exit_code != 0 or response["transport_errors"] or any(e.get("type") == "malformed_event" for e in events):
        response.update(status="unresolved" if count else "failed", error=error or "native_agent_did_not_complete")
    elif not settled or not isinstance(answer, str) or not answer.strip():
        response.update(status="failed", error="missing_final_answer" if settled else "native_result_not_success")
    elif init.get("permissionMode") != PERMISSION_MODE or not mcp_connected:
        response.update(status="failed", error="native_configuration_mismatch")
    if response["native_url_evidence"]:
        response.update(visited_url=request["input"]["content"], page_verification_status="native_url_observed")
    documents = common.read_jsonl(browser.directory / "navigation.jsonl") if browser else []
    match = next((r for r in documents if r.get("type") == "document" and r.get("url") == request["input"]["content"]
                  and 200 <= r.get("status", 0) < 300), None)
    if match:
        response.update(visited_url=match["url"],
                        page_verification_source="playwright_mcp_document",
                        page_verification_status="browser_document_observed")
    invocation_path = evidence / "invocation.json"
    invocation = json.loads(invocation_path.read_text()) if invocation_path.exists() else {}
    audit = access_audit(trace, invocation.get("cwd"), request["input"]["content"])
    audit.update(target_url_evidence=response["native_url_evidence"], page_verification_status=response["page_verification_status"],
                 coverage="Claude native events and Messages inputs/outputs; lexical paths and tool outputs, not an OS-level file access trace")
    audit["trace_indices_for_manual_review"] = sorted(set(audit["trace_indices_for_manual_review"]) |
                                                    {i for i, t in enumerate(trace) if t["name"] == "Bash"})
    path = evidence / "access-audit.json"
    common.write_json(path, audit)
    response["access_audit_path"] = str(path)
    bindings = []
    for path in sorted((evidence / "provider").glob("*.request.json")):
        body = json.loads(path.read_text())
        bindings.append({"request": path.name, "model": body.get("model"), "thinking": body.get("thinking"),
                         "output_config": body.get("output_config"), "max_tokens": body.get("max_tokens"),
                         "tool_names": [t.get("name", t.get("type")) for t in body.get("tools", [])],
                         "native_system_present": bool(body.get("system"))})
    common.write_json(evidence / "model-input-audit.json", bindings)
    response["model_input_audit_path"] = str(evidence / "model-input-audit.json")
    return response


@isolated_runtime
def run_adapter(request_path, response_path, *, container=False):
    request = json.loads(Path(request_path).read_text())
    validate_request(request, container=container, container_inner=container)
    if not Path("/.dockerenv").is_file() or os.getuid() == 0:
        raise ValueError("Claude full permissions run only as a non-root user inside the trajectory container")
    response_path = Path(response_path).resolve()
    evidence = response_path.parent / "claude-evidence"
    if response_path.exists() or evidence.exists():
        raise ValueError("Refusing to overwrite a native Claude attempt")
    evidence.mkdir(parents=True)
    settings, target = request["tool_config"], request["target"]
    temporary = tempfile.TemporaryDirectory(prefix="session-", dir=response_path.parent)
    root = Path(temporary.name)
    work, home, config_home = root / "workspace", root / "home", root / "config"
    for directory in (work, home, config_home):
        directory.mkdir(mode=0o700)
    agent = browser = proxy = None
    failure = None
    started = time.monotonic()
    previous = {}
    def interrupted(signum, _frame):
        raise InterruptedError(f"Native adapter interrupted by signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, interrupted)
    try:
        executable = settings["claude_executable"]
        actual = subprocess.check_output([executable, "--version"], text=True).strip()
        if actual != CLAUDE_VERSION + " (Claude Code)":
            raise ValueError(f"Expected Claude Code {CLAUDE_VERSION}; got {actual}")
        proxy = NativeModelProxy(target, "local-proxy-only", evidence / "provider", settings["max_model_requests"],
                connect_timeout_seconds=settings["proxy_connect_timeout_seconds"], preserve_native_parameters=True).start()
        browser, observer_path = start_browser(request, evidence)
        mcp = {"mcpServers": {"playwright": {"type": "stdio", "command": shutil.which("node"),
               "args": [str(runtime_script(common.RUNTIME / "browser_observer.mjs", evidence)), str(observer_path)]}}}
        mcp_path = evidence / "mcp.json"
        common.write_json(mcp_path, mcp)
        prompt = prompt_for(request)
        (evidence / "prompt.txt").write_text(prompt)
        env = session_environment(cli_environment(request, proxy.base_url, home, config_home), root)
        command = cli_command(executable, mcp_path)
        common.write_json(evidence / "invocation.json", {"argv": command, "cwd": str(work), "harness": "claude",
                          "adapter_version": VERSION, "model_binding": target, "environment": env,
                          "authentication_mode": target.get("auth_mode", "api_key"),
                          "native_web_tools_policy": TOOLS_POLICY, "input_policy": ACCESS_POLICY,
                          "system_prompt_policy": common.SYSTEM_PROMPT_POLICY, "permission_mode": PERMISSION_MODE})
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
            shutil.copytree(work, evidence / "workspace-artifacts", symlinks=True)
            workspace_mapping(evidence, work)
            if (config_home / "projects").exists():
                shutil.copytree(config_home / "projects", evidence / "native-sessions", symlinks=True)
            response = collect_result(request, evidence, browser, agent.returncode if agent else None, failure)
            response.update(wall_seconds=round(time.monotonic() - started, 3), completed_at=datetime.now(timezone.utc).isoformat(),
                            authentication_mode=target.get("auth_mode", "api_key"),
                            harness_build=f"claude-code@{CLAUDE_VERSION}", native_process_exited=agent is None or agent.returncode is not None,
                            browser_closed=cleaned, browser_lifecycle="per-attempt official Playwright MCP")
            if not cleaned:
                response.update(status="unresolved", error="browser_cleanup_incomplete")
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
        response["temporary_profile_removed"] = not config_home.exists()
        common.write_json(response_path, response)
    return response


def adapter_config(*, tool_config, timeout_seconds, max_model_requests):
    return {"harness_type": "claude", "build": f"{VERSION}/claude-code@{CLAUDE_VERSION}/container",
            "command": [sys.executable, str(ROOT / "cli/internal/claude_native_agent_adapter.py"),
                        "--request", "{request_path}", "--response", "{response_path}"],
            "supported_inputs": ["url"],
            "timeout_seconds": timeout_seconds + 120,
            "tool_config": {**tool_config, "max_model_requests": max_model_requests,
                            "native_web_tools_policy": TOOLS_POLICY, "system_prompt_policy": common.SYSTEM_PROMPT_POLICY,
                            "input_policy": ACCESS_POLICY}}
