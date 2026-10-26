"""File protocol for a bound URL tool runner or a complete Agent harness.

Each subprocess receives a fresh request/response directory and must return a
final answer plus the actual tool trace. No shell command interpolation is used.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import uuid

from defipi.evaluation import unified_final_eval as engine
from defipi.paths import PROJECT_ROOT

PROTOCOL = "final-eval-adapter-v1"


class AdapterUnresolvedError(RuntimeError):
    """An unfinished execution with a returned response retained as evidence."""
    def __init__(self, message, raw_response):
        super().__init__(message)
        self.raw_response = raw_response


def resolve_adapter(value, base, axes, representation):
    if not isinstance(value, dict):
        raise engine.FinalEvalError("Bind adapter.command, build, tool_config and supported_inputs for URL/Agent")
    adapter = copy.deepcopy(value)
    command = adapter.get("command")
    if (not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or not arg for arg in command)):
        raise engine.FinalEvalError("adapter.command must be a non-empty argument list")
    for token in ("{request_path}", "{response_path}"):
        if not any(token in arg for arg in command):
            raise engine.FinalEvalError(f"adapter.command must contain {token}")
    executable = (str((base / command[0]).resolve()) if "/" in command[0]
                  else shutil.which(command[0]))
    if not executable or not os.access(executable, os.X_OK):
        raise engine.FinalEvalError(f"Adapter executable is unavailable: {command[0]}")
    command[0] = executable
    for index, arg in enumerate(command[1:], 1):
        if "{request_path}" in arg or "{response_path}" in arg:
            continue
        candidate = (base / arg).resolve()
        if candidate.is_file():
            command[index] = str(candidate)
    if not isinstance(adapter.get("build"), str) or not adapter["build"].strip():
        raise engine.FinalEvalError("adapter.build is required")
    if not isinstance(adapter.get("tool_config"), dict) or not adapter["tool_config"]:
        raise engine.FinalEvalError("adapter.tool_config must explicitly bind tools and input policy")
    required_input = "url" if axes["web_injection_channel"] == "url" else representation
    if required_input not in (adapter.get("supported_inputs") or []):
        raise engine.FinalEvalError(f"Adapter does not declare support for {required_input}")
    if adapter.get("harness_type") != axes["harness_type"]:
        raise engine.FinalEvalError("adapter.harness_type must match the selected harness (null for API)")
    timeout = adapter["timeout_seconds"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise engine.FinalEvalError("adapter.timeout_seconds must be positive")
    adapter["source_root"] = str(PROJECT_ROOT)
    return adapter


def adapter_command(adapter):
    """Resolve checkout-owned command paths against the current installation."""
    previous = adapter.get("source_root")
    if previous is None:
        return adapter["command"]
    root = Path(previous)
    return [str(PROJECT_ROOT / Path(arg).relative_to(root))
            if Path(arg).is_absolute() and Path(arg).is_relative_to(root) else arg
            for arg in adapter["command"]]


class AdapterExecutor:
    def __init__(self, dispatch, target, context, output, limiter):
        self.dispatch, self.target, self.context = dispatch, target, context
        self.output, self.limiter = Path(output), limiter

    def build_request(self, observation, question, *, observation_format):
        axes = self.dispatch["axes"]
        expected_format = self.dispatch["observation_representation"]
        if observation_format != expected_format:
            raise ValueError(f"Expected observation_format={expected_format}, got {observation_format}")
        kind = "url" if axes["web_injection_channel"] == "url" else observation_format
        return {"schema_version": PROTOCOL, "experiment": axes,
                "task_id": self.dispatch["task_id"], **self.context.value,
                "question": question, "input": {"kind": kind, "content": observation},
                "target": copy.deepcopy(self.target),
                "tool_config": self.dispatch["adapter"]["tool_config"],
                "url_prompt_defense": self.dispatch.get("url_prompt_defense"),
                "limits": {"api_concurrency": 1,
                           "task_api_concurrency": self.dispatch["api_concurrency_per_task"],
                           "trajectory_attempts": 1, "api_max_attempts": self.dispatch["max_attempts"]}}

    def execute(self, request):
        adapter = self.dispatch["adapter"]
        attempt_dir = self.output / "adapter_runs" / uuid.uuid4().hex
        attempt_dir.mkdir(parents=True)
        request_path, response_path = attempt_dir / "request.json", attempt_dir / "response.json"
        # Only the model-visible observation is supplied. Ground truth and the
        # original unfiltered file/resource paths remain in the scoring process.
        engine._atomic_json(request_path, request)
        command = [arg.replace("{request_path}", str(request_path)).replace("{response_path}", str(response_path))
                   for arg in adapter_command(adapter)]
        with self.limiter, (attempt_dir / "stdout.log").open("wb") as stdout, (attempt_dir / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=attempt_dir, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                exit_code = process.wait(timeout=adapter["timeout_seconds"])
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                # The harness may already have called a model. Keep unresolved.
                raise TimeoutError(f"Adapter response unresolved; evidence: {attempt_dir}") from exc
        if not response_path.exists():
            raise RuntimeError(f"Adapter exited {exit_code} without a response; evidence: {attempt_dir}")
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Unreadable adapter response; evidence: {attempt_dir}") from exc
        if not isinstance(response, dict):
            raise RuntimeError(f"Adapter response is not an object; evidence: {attempt_dir}")
        error = self._response_error(response, request)
        if exit_code != 0:
            error = f"adapter_exit_{exit_code}"
        raw = {**response, "adapter_evidence_dir": str(attempt_dir)}
        if response.get("status") == "unresolved":
            raise AdapterUnresolvedError(f"Adapter reports unresolved execution; evidence: {attempt_dir}", raw)
        return {"text": response.get("final_answer", ""), "finish_reason": response.get("finish_reason"),
                "error": error, "usage": response.get("usage") or {}, "raw_response": raw}

    def _response_error(self, response, request):
        if response.get("schema_version") != PROTOCOL:
            return "adapter_protocol_mismatch"
        if response.get("status") != "completed":
            return str(response.get("error") or "adapter_not_completed")
        if response.get("model") != self.target["model"]:
            return "adapter_model_mismatch"
        if response.get("harness_type") != self.dispatch["axes"]["harness_type"]:
            return "adapter_harness_mismatch"
        settings = self.dispatch["adapter"]["tool_config"]
        openrouter_access_audit = (self.dispatch["axes"]["mode"] == "api"
                                   and request["input"]["kind"] == "url"
                                   and settings.get("url_access") == "openrouter_server_web_fetch")
        anthropic_access_audit = (self.dispatch["axes"]["mode"] == "api"
                                  and request["input"]["kind"] == "url"
                                  and settings.get("url_access") == "anthropic_native_web_fetch")
        url_api_access_audit = openrouter_access_audit or anthropic_access_audit
        pi_native_access_audit = (self.dispatch["axes"].get("harness_type") == "pi-agent"
                                 and settings.get("input_policy") == "native-tools-post-run-audit-v1")
        claude_native_access_audit = (self.dispatch["axes"].get("harness_type") == "claude"
                                      and settings.get("input_policy") == "native-tools-post-run-audit-v1")
        dsh_native_access_audit = (self.dispatch["axes"].get("harness_type") == "dsh"
                                   and settings.get("input_policy") == "native-tools-post-run-audit-v1")
        native_access_audit = pi_native_access_audit or claude_native_access_audit or dsh_native_access_audit or (self.dispatch["axes"].get("harness_type") == "codex"
                               and settings.get("native_web_tools_policy") == "runtime-defaults-native-tools-with-playwright-mcp"
                               and settings.get("input_policy") == "native-tools-post-run-audit-v1")
        trace = response.get("trace")
        if not isinstance(trace, list) or (not native_access_audit and not url_api_access_audit and not any(
                isinstance(item, dict) and item.get("type") == "tool" and item.get("name")
                and "input" in item and "output" in item for item in trace)):
            return "adapter_missing_tool_trace"
        observations = response.get("observation_representations")
        if not isinstance(observations, list) or (not observations and not native_access_audit and not url_api_access_audit) or any(
                not isinstance(item, str) or not item for item in observations):
            return "adapter_missing_observation_representations"
        if (not isinstance(response.get("final_answer"), str)
                or (url_api_access_audit and not response["final_answer"].strip()
                    and not (anthropic_access_audit and response.get("finish_reason") == "refusal"))):
            return "adapter_missing_final_answer"
        if dsh_native_access_audit:
            if response.get("input_policy") != settings["input_policy"]:
                return "adapter_input_policy_mismatch"
            if not response["final_answer"].strip():
                return "adapter_missing_final_answer"
            # Missing page evidence does not invalidate a complete answer.
            # A known wrong URL still fails the requested condition.
            if response.get("visited_url") is not None and response["visited_url"] != request["input"]["content"]:
                return "adapter_target_url_not_verified"
            return None
        if pi_native_access_audit or claude_native_access_audit:
            if response.get("input_policy") != settings["input_policy"]:
                return "adapter_input_policy_mismatch"
            if claude_native_access_audit:
                from defipi.execution.agents.claude import target_url_evidence
                if response.get("native_url_evidence") != target_url_evidence(trace, request["input"]["content"]):
                    return "adapter_url_audit_mismatch"
            # Answer completion and captured page evidence remain separate.
            return None
        if request["input"]["kind"] == "url":
            if (self.dispatch["axes"].get("harness_type") == "codex"
                    and settings.get("native_web_tools_policy") == "runtime-defaults-native-tools-with-playwright-mcp"):
                from defipi.execution.agents.tool_evidence import target_url_evidence
                evidence = target_url_evidence(trace, request["input"]["content"])
                if response.get("native_url_evidence") != evidence:
                    return "adapter_url_audit_mismatch"
                if not evidence and not native_access_audit:
                    return "adapter_target_url_not_verified"
                # Native access need not use Playwright. Access findings
                # remain separate from answer validity.
                return None
            if openrouter_access_audit:
                from defipi.execution.api.openrouter_url import target_url_evidence
                evidence = target_url_evidence(trace, request["input"]["content"])
                if response.get("native_url_evidence") != evidence:
                    return "adapter_url_audit_mismatch"
                # Missing fetch evidence is an audit finding, not an invalid answer.
                return None
            if (self.dispatch["axes"]["mode"] == "api"
                    and self.dispatch["adapter"]["tool_config"].get("url_access") == "anthropic_native_web_fetch"):
                from defipi.execution.api.anthropic_url import target_url_evidence
                evidence = target_url_evidence(trace, request["input"]["content"])
                if response.get("native_url_evidence") != evidence:
                    return "adapter_url_audit_mismatch"
                # A normal answer or explicit refusal can precede page access.
                return None
            if (self.dispatch["axes"]["mode"] == "api"
                    and self.dispatch["adapter"]["tool_config"].get("url_access") == "deepseek_native_web_search"):
                # Native search returns provider results, not original HTML bytes.
                from defipi.execution.api.deepseek_url import target_url_evidence
                evidence = target_url_evidence(trace, request["input"]["content"])
                if not evidence or response.get("native_url_evidence") != evidence:
                    return "adapter_target_url_not_verified"
                return None
            if response.get("visited_url") != request["input"]["content"]:
                return "adapter_target_url_not_verified"
        return None
