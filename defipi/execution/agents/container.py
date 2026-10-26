"""One disposable container per native trajectory, with its counted provider on the host."""
from __future__ import annotations

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import argparse
import hmac
import json
import os
from pathlib import Path
from defipi.execution.session_runtime import isolated_runtime
import secrets
import signal
import subprocess
import sys
import time
import uuid

from defipi.config import load_local_env
from defipi.execution.api.native_model_proxy import NativeModelProxy
from defipi.jsonio import write_json as write

VERSION = "native-isolated-container-v3-pi-all-channels/session-v1"
ROOT = _PACKAGE_ROOT
HARNESSES = {"pi-agent", "codex", "dsh", "claude"}


def validate_request(request):
    """Validate the host binding without requiring agent packages on the host."""
    from defipi.configuration.execution import validate_execution_request
    validate_execution_request(request, container=True)
    harness = request["experiment"]["harness_type"]
    settings = request["tool_config"]
    if harness not in HARNESSES:
        raise ValueError("Choose Pi, Codex, DSH or Claude Code")
    if not isinstance(settings.get("container_image"), str) or not settings["container_image"].strip():
        raise ValueError("Bind --container-image to the built image tag")
    if type(settings.get("max_model_requests")) is not int or settings["max_model_requests"] < 1:
        raise ValueError("Bind a positive --max-model-requests per trajectory")
    if harness == "pi-agent":
        from defipi.execution.agents.pi import model_binding, validate_request as validate_pi
        validate_pi(request, container=True)
        axes = request["experiment"]
        channel, attack = axes.get("web_injection_channel"), axes.get("attack_method")
        if (axes.get("mode") != "agent" or request["input"]["kind"] != channel or
                (channel, attack) not in {("html", "off"), ("html", "filter"),
                                         ("url", "off"), ("url", "url_prompt")}):
            raise ValueError("Pi containers require HTML OFF/Filter or URL OFF/system-prompt input")
    elif harness == "claude":
        from defipi.execution.agents.claude import validate_request as validate_claude
        validate_claude(request, container=True)
    else:
        from defipi.execution.agents.common import validate_request as validate_native
        validate_native(request, container=True)
        if harness == "codex":
            from defipi.execution.agents.codex import TOOLS_POLICY, ACCESS_POLICY
            if settings.get("native_web_tools_policy") != TOOLS_POLICY or settings.get("input_policy") != ACCESS_POLICY:
                raise ValueError("Bind the native Codex tools and post-run access audit policies")


def adapter_config(harness, image, *, max_model_requests, tool_config, timeout_seconds):
    """Keep each native adapter's settings and run it in the shared public image."""
    from defipi.configuration.execution import CONTAINER_RUNTIME_FIELDS, container_host_problems
    from defipi.configuration.schema import ValidationError
    problems = container_host_problems(tool_config, prefix="tool_config")
    if problems:
        raise ValidationError(problems)
    if not isinstance(image, str) or not image.strip():
        raise ValueError("Agent containers require --container-image with the built image tag")
    if type(max_model_requests) is not int or max_model_requests < 1:
        raise ValueError("Agent containers require --max-model-requests (positive per-trajectory budget)")
    options = dict(tool_config=tool_config, timeout_seconds=timeout_seconds)
    if harness == "pi-agent":
        from defipi.execution.agents.pi import adapter_config as native_config
        binding = native_config(**options)
    elif harness == "claude":
        from defipi.execution.agents.claude import adapter_config as native_config
        binding = native_config(max_model_requests=max_model_requests, **options)
    elif harness == "codex":
        from defipi.execution.agents.codex import adapter_config as native_config
        binding = native_config(max_model_requests=max_model_requests, **options)
    elif harness == "dsh":
        from defipi.execution.agents.dsh import adapter_config as native_config
        binding = native_config(harness, max_model_requests=max_model_requests, **options)
    else:
        raise ValueError("Choose Pi, Codex, DSH or Claude Code")
    # Native binding constructors locate host packages for direct execution.
    # The container entry resolves its own installed packages and executables.
    for name in CONTAINER_RUNTIME_FIELDS:
        binding["tool_config"].pop(name, None)
    binding.update(build=binding["build"] + "/" + VERSION,
                   command=[sys.executable, str(ROOT / "cli/internal/container_agent_adapter.py"),
                            "--request", "{request_path}", "--response", "{response_path}"],
                   supported_inputs=["html", "url"] if harness == "pi-agent" else ["url"],
                   timeout_seconds=timeout_seconds + 120)
    binding["tool_config"].update(container_image=image, max_model_requests=max_model_requests)
    return binding


class ContainerModelProxy(NativeModelProxy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, listen_host="0.0.0.0", **kwargs)
        self.capability = secrets.token_hex(24)

    def start(self):
        super().start()
        self.base_url = f"http://host.docker.internal:{self.server.server_port}/{self.capability}/v1"
        return self

    def forward(self, handler):
        prefix, separator, rest = handler.path.lstrip("/").partition("/")
        if not separator or not hmac.compare_digest(prefix, self.capability):
            handler.send_error(403, "This trajectory's proxy capability is required")
            return
        handler.path = "/" + rest
        super().forward(handler)


def docker_command(image, name, directory, control):
    if os.getuid() == 0:
        raise ValueError("Run the native container adapter as a non-root host user")
    command = ["docker", "run", "--rm", "--init", "--name", name,
            "--label", "session.isolated=true",
            "--add-host", "host.docker.internal:host-gateway",
            "--user", f"{os.getuid()}:{os.getgid()}",
            # Preserve the research container's native tool sandbox support.
            "--security-opt", "seccomp=unconfined", "--cpus", "2", "--memory", "1536m",
            "--pids-limit", "512", "--shm-size", "256m",
            "--mount", f"type=bind,source={directory},target={directory}"]
    return command + [image, str(control)]


@isolated_runtime(container=True)
def run_adapter(request_path, response_path):
    request_path, response_path = Path(request_path).resolve(), Path(response_path).resolve()
    request = json.loads(request_path.read_text())
    target, settings = request["target"], request["tool_config"]
    harness = request["experiment"]["harness_type"]
    validate_request(request)
    image = settings["container_image"]
    if response_path.exists() or (response_path.parent / "container-control.json").exists():
        raise ValueError("Refusing to repeat a container trajectory")
    load_local_env()
    key_env = target["client_settings"]["api_key_env"]
    key = os.environ.get(key_env)
    if not key:
        raise ValueError(f"Missing {key_env}")
    directory = response_path.parent
    provider = directory / "host-provider"
    proxy_class = ContainerModelProxy
    if harness == "claude":
        from defipi.execution.api.claude_model_proxy import ClaudeModelProxy
        proxy_class = ClaudeModelProxy
    search = None
    if harness == "dsh":
        from defipi.execution.agents.common import dsh_search_target
        search_target = dsh_search_target(request)
        search_key = os.environ.get(search_target["client_settings"]["api_key_env"])
        if not search_key:
            raise ValueError(f"Missing {search_target['client_settings']['api_key_env']}")
        search = {"target": search_target, "api_key": search_key}
    proxy = proxy_class(target, key, provider, settings["max_model_requests"],
                        connect_timeout_seconds=settings["proxy_connect_timeout_seconds"], preserve_native_parameters=harness == "claude",
                        native_search=search, native_files=harness in {"dsh", "claude"}).start()
    native_response = directory / "container-response.json"
    control = directory / "container-control.json"
    write(control, {"request_path": str(request_path), "response_path": str(native_response),
                    "provider_evidence_dir": str(provider), "proxy_url": proxy.base_url})
    name = "session-" + uuid.uuid4().hex
    command = docker_command(image, name, directory, control)
    write(directory / "container-invocation.json", {
        "version": VERSION, "argv": command, "image": image, "container_name": name,
        "environment": "linux-container", "host_directory_mounted": str(directory),
        "temporary_files": "private container filesystem, including hardcoded /tmp paths",
        "authentication_mode": target.get("auth_mode", "api_key"),
        "credentials": "provider credentials retained in the host process; container receives only a budgeted proxy capability"})
    started, process, failure = time.monotonic(), None, None
    previous = {}
    def interrupted(signum, _frame):
        raise InterruptedError(f"Container adapter interrupted by signal {signum}")
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, interrupted)
    try:
        with (directory / "container.stdout.log").open("wb") as stdout, (directory / "container.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            process.wait(timeout=settings["trajectory_timeout_seconds"] + 90)
            if process.returncode:
                failure = f"container_exit_{process.returncode}"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        # Docker clients can die while their container continues. Always remove
        # this exact container before returning a slot to the batch queue.
        cleanup_errors, cleaned = [], False
        try:
            try:
                removed = subprocess.run(["docker", "rm", "--force", name],
                                         capture_output=True, timeout=20)
                if removed.returncode:
                    cleanup_errors.append("docker_remove_failed")
            except subprocess.TimeoutExpired:
                cleanup_errors.append("docker_remove_timeout")
            except OSError:
                cleanup_errors.append("docker_remove_unavailable")
            if process and process.poll() is None:
                try:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_errors.append("docker_client_did_not_drain")
            try:
                check = subprocess.run(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"],
                                       capture_output=True, text=True, timeout=10)
                cleaned = check.returncode == 0 and not check.stdout.strip()
                if not cleaned:
                    cleanup_errors.append("container_cleanup_unconfirmed")
            except subprocess.TimeoutExpired:
                cleanup_errors.append("docker_cleanup_check_timeout")
            except OSError:
                cleanup_errors.append("docker_cleanup_check_unavailable")
        finally:
            try:
                proxy.close()
            except Exception:
                cleanup_errors.append("proxy_close_failed")
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                write(directory / "container-cleanup.json", {
                    "container_name": name, "container_removed": cleaned,
                    "errors": cleanup_errors})
    response = json.loads(native_response.read_text()) if native_response.exists() else {
        "schema_version": "final-eval-adapter-v1", "harness_type": harness,
        "model": target["model"], "final_answer": "", "finish_reason": None,
        "status": "unresolved" if proxy.count else "failed", "trace": [], "usage": {},
        "model_requests": proxy.count, "tool_calls": 0, "retryable": False,
        "provider_evidence_dir": str(provider)}
    response.update(model_requests=proxy.count, host_provider_evidence_dir=str(provider),
                    container_adapter_version=VERSION, execution_environment="linux-container",
                    container_image=image, container_name=name, container_removed=cleaned,
                    container_cleanup_errors=cleanup_errors,
                    environment_failure=(not cleaned or "proxy_close_failed" in cleanup_errors
                                         or bool(failure and not native_response.exists())),
                    container_wall_seconds=round(time.monotonic() - started, 3))
    if failure or not cleaned:
        response.update(status="unresolved" if proxy.count else "failed",
                        error=failure or "container_cleanup_incomplete", retryable=False)
    write(response_path, response)
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    run_adapter(args.request, args.response)


if __name__ == "__main__":
    main()
