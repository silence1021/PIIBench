"""MUZZLE browser execution and captured-request replay through Pi."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import quote

from defipi.concurrency import FileSlots
from defipi.evaluation.field_matching import field_in_text
from defipi.execution.agents.pi import ROOT, model_binding, read_jsonl
from defipi.execution.agents.container import adapter_config
from defipi.configuration.execution import pi_target_from_role
from defipi.methods.muzzle.browser import (
    BudgetExceeded,
    ExecutionStopped,
    FIELDS,
    ResponseGateway,
    contains_text,
    score_answer,
    write_json,
)

SCAFFOLD_VERSION = "muzzle-pii-pi-container-v3"


@contextmanager
def published_page(html_root, record, html, directory):
    """Serve a separate immutable profile bundle for each candidate trajectory."""
    root = Path(html_root).resolve()
    source = (root / record["html_file"]).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("Pi source page must be a file inside html_root")
    relative = source.relative_to(root)
    site = Path(directory) / "site"
    shutil.copytree(source.parent, site / relative.parent)
    (site / relative).write_text(html, encoding="utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), partial(Handler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://host.docker.internal:{server.server_port}/{quote(relative.as_posix(), safe='/')}"
    state = {"url": url, "running": True, "page": str(site / relative)}
    write_json(Path(directory) / "page_server.json", state)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        write_json(Path(directory) / "page_server.json", {**state, "running": False})


def muzzle_trace(response, record, html, payload, directory, joint_weight):
    """Adapt recorded Pi evidence; scoring ground truth stays outside Pi."""
    native_path = response.get("trajectory_path")
    native = json.loads(Path(native_path).read_text()) if native_path else {}
    steps = []
    for index, step in enumerate(native.get("steps", []), 1):
        content = [{"type": "output_text", "text": block["text"]}
                   for block in step.get("assistant", {}).get("content", []) if block.get("type") == "text"]
        steps.append({"turn": index, "response": {"output": [{"type": "message", "content": content}]},
                      "tools": [{"name": tool["name"], "arguments": tool.get("arguments", {}),
                                 "result": tool.get("result", {}), "is_error": tool.get("is_error", False)}
                                for tool in step.get("tools", [])]})
    observations = [tool["result"] for step in steps for tool in step["tools"] if not tool["is_error"]]
    valid = response.get("status") == "completed" and response.get("browser_closed") is True
    error = response.get("error")
    answer = response.get("final_answer", "")
    if valid and not answer.strip():
        valid, error = False, "empty_final_answer"
    from defipi.methods.muzzle.browser import visible_strings
    text = "\n".join(visible_strings(observations))
    trace = {"scaffold_version": SCAFFOLD_VERSION, "status": "valid" if valid else "invalid",
             "answer": answer, "error": error, "steps": steps,
             "score": score_answer(answer, record["reporter_info"], joint_weight) if valid else None,
             "exposure": {"contact_in_tool_text": {f: field_in_text(f, text, record["reporter_info"][f]) for f in FIELDS},
                          "defense_in_tool_text": bool(payload and contains_text(observations, payload)),
                          "screenshot_calls": sum(tool["name"].endswith("__browser_take_screenshot")
                                                  for step in steps for tool in step["tools"])},
             "pi_trajectory_path": native_path, "pi_response_path": str(Path(directory) / "adapter/response.json"),
             "model_request_paths": native.get("model_request_paths", []),
             "model_requests": response.get("model_requests", 0), "usage": response.get("usage", {}),
             "tool_calls": response.get("tool_calls", 0), "tool_budget": response.get("tool_budget"),
             "tool_policy": response.get("tool_policy"), "browser_closed": response.get("browser_closed"),
             "execution_environment": response.get("execution_environment"),
             "container_image": response.get("container_image"), "container_removed": response.get("container_removed"),
             "observation_representations": response.get("observation_representations", []),
             "trace_path": str(Path(directory) / "trace.json")}
    write_json(trace["trace_path"], trace)
    return trace


class PiBrowserRunner:
    def __init__(self, output_dir, config):
        self.output_dir = Path(output_dir).resolve()
        self.config = config
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.claims = self.output_dir / "pi_request_budget"
        self.claims.mkdir()
        pool = config["pi_agent"]["agent_pool_dir"]
        self.pool = Path(pool) if Path(pool).is_absolute() else ROOT / pool
        self.pool_slots = config["pi_agent"]["agent_pool_slots"]
        self.replay_gateway = None
        self.usage = {"reserved_tasks": 0, "valid_tasks": 0, "invalid_tasks": 0,
                      "model_requests": 0, "replay_requests": 0, "max_pi_tasks": config["max_pi_tasks"],
                      "max_pi_requests": config["max_pi_requests"],
                      "agent_pool_dir": str(self.pool), "agent_pool_slots": self.pool_slots,
                      "target_requests_in_direct_api_budget": False}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stopped.set()
        with self.lock:
            self._save_usage()

    def _save_usage(self):
        self.usage["model_requests"] = sum(p.name.isdigit() for p in self.claims.iterdir())
        if self.replay_gateway is not None:
            self.usage["replay_usage"] = copy.deepcopy(self.replay_gateway.usage)
        write_json(self.output_dir / "pi_usage.json", self.usage)

    def replay(self, request, *, purpose):
        """Submit one target request under Pi's shared budget; never execute tools."""
        role = self.config["roles"]["target"]
        binding = model_binding(pi_target_from_role(role), container=True)
        if binding["api"] != "openai-responses" or request.get("model") != binding["model"]:
            raise ValueError("Pi single-step evaluation requires a request for the configured Luna model")
        with FileSlots(self.pool, self.pool_slots):
            with self.lock:
                if self.stopped.is_set():
                    raise ExecutionStopped("MUZZLE Pi controller stopped")
                # The Pi observer uses the same exclusive-create claims. Replay
                # and browser requests therefore share one hard attempt limit.
                for number in range(self.config["max_pi_requests"]):
                    try:
                        with (self.claims / str(number)).open("x") as claim:
                            json.dump({"case_id": purpose, "runtime": "pi_single_step", "at": time.time()}, claim)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise BudgetExceeded("Pi model request budget exhausted")
                self.usage["replay_requests"] += 1
                if self.replay_gateway is None:
                    self.replay_gateway = ResponseGateway(self.output_dir / "pi_replay", self.config["max_pi_requests"],
                                                          roles=self.config["roles"], parallelism=self.config["parallelism"])
                    self.replay_gateway.stopped = self.stopped
                self._save_usage()
            # Collect the author mode text-template request without SSE.
            replay = copy.deepcopy(request)
            replay["stream"] = False
            try:
                return self.replay_gateway.call(replay, purpose=purpose)
            finally:
                with self.lock:
                    self._save_usage()

    def _invoke(self, command, request_path, response_path, settings):
        argv = [part.replace("{request_path}", str(request_path)).replace("{response_path}", str(response_path))
                for part in command]
        with (response_path.parent / "adapter.log").open("wb") as log:
            process = subprocess.Popen(argv, stdout=log, stderr=log)
            deadline = time.monotonic() + settings["trajectory_timeout_seconds"] + 150
            try:
                while process.poll() is None:
                    if self.stopped.wait(.1):
                        raise ExecutionStopped("MUZZLE Pi controller stopped")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Pi adapter timed out")
            finally:
                if process.poll() is None:
                    process.terminate()  # The adapter handles SIGTERM and closes its browser.
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        if not response_path.is_file():
            raise RuntimeError(f"Pi adapter produced no response (exit {process.returncode}); see {response_path.parent / 'adapter.log'}")
        return json.loads(response_path.read_text())

    def run_browser(self, _gateway, role, *, html_root, record, html, question, output_dir,
                    max_steps, joint_weight, payload=None):
        attempts = self.config["pi_agent"]["trajectory_max_attempts"]
        prior = []
        for attempt in range(1, attempts + 1):
            directory = Path(output_dir) if attempt == 1 else Path(str(output_dir) + f"-api-retry-{attempt:02d}")
            trace = self._run_browser_once(_gateway, role, html_root=html_root, record=record, html=html,
                                          question=question, output_dir=directory, max_steps=max_steps,
                                          joint_weight=joint_weight, payload=payload)
            transient = False
            if attempts > 1 and trace["status"] != "valid":
                response = json.loads((directory / "adapter/response.json").read_text())
                error = response.get("error") or ""
                provider_errors = [event.get("message", {}).get("errorMessage", "")
                                   for event in read_jsonl(directory / "adapter/pi-evidence/events.jsonl")
                                   if event.get("message", {}).get("stopReason") == "error"]
                error_text = " ".join([error, *provider_errors]).lower()
                transient = response.get("status") != "completed" and (
                    bool(re.fullmatch(r"provider_http_(408|429|5\d\d)", error)) or
                    (error in {"pi_did_not_complete", ""} and any(word in error_text for word in
                     ("timeout", "timed out", "connection", "fetch failed", "econnreset"))))
            if attempts > 1:
                trace.update(api_retry_trace_paths=list(prior), retryable_transport_error=transient)
                write_json(trace["trace_path"], trace)
            if not transient or attempt == attempts:
                return trace
            prior.append(trace["trace_path"])
            print(f"Pi transient API failure; retry {attempt + 1}/{attempts}: {directory}", flush=True)
            if self.stopped.wait(2):
                raise ExecutionStopped("MUZZLE Pi controller stopped")

    def _run_browser_once(self, _gateway, role, *, html_root, record, html, question, output_dir,
                          max_steps, joint_weight, payload=None):
        with self.lock:
            if self.stopped.is_set():
                raise ExecutionStopped("MUZZLE Pi controller stopped")
            self._save_usage()
            if self.usage["reserved_tasks"] >= self.config["max_pi_tasks"]:
                raise BudgetExceeded("Pi task budget exhausted")
            if self.usage["model_requests"] >= self.config["max_pi_requests"]:
                raise BudgetExceeded("Pi model request budget exhausted")
            self.usage["reserved_tasks"] += 1
            number = self.usage["reserved_tasks"]
            self._save_usage()
        directory = Path(output_dir).resolve()
        directory.mkdir(parents=True)
        adapter_dir = directory / "adapter"
        adapter_dir.mkdir()
        options = self.config["pi_agent"]
        binding = adapter_config("pi-agent", options["container_image"],
                                 max_model_requests=options["max_model_requests"],
                                 tool_config={"max_tool_calls": max_steps,
                                     **{key: options[key] for key in ("trajectory_timeout_seconds", "proxy_connect_timeout_seconds",
                                        "pi_http_idle_timeout_seconds", "tool_profile", "mcp_startup_timeout_seconds", "mcp_tool_timeout_seconds")}},
                                 timeout_seconds=options["trajectory_timeout_seconds"])
        settings = binding["tool_config"]
        settings["shared_request_budget"] = {"directory": str(self.claims), "limit": self.config["max_pi_requests"]}
        target = pi_target_from_role(role)
        try:
            with FileSlots(self.pool, self.pool_slots), published_page(html_root, record, html, directory) as url:
                request = {"schema_version": "final-eval-adapter-v1", "case_id": f"muzzle-pi-{number:06d}",
                           "experiment": {"harness_type": "pi-agent", "mode": "agent", "attack_method": "off",
                                          "web_injection_channel": "url"}, "limits": {"api_concurrency": 1},
                           "target": target, "question": question, "input": {"kind": "url", "content": url},
                           "tool_config": settings}
                request_path, response_path = adapter_dir / "request.json", adapter_dir / "response.json"
                write_json(request_path, request)
                response = self._invoke(binding["command"], request_path, response_path, settings)
            trace = muzzle_trace(response, record, html, payload, directory, joint_weight)
            with self.lock:
                self.usage["valid_tasks" if trace["status"] == "valid" else "invalid_tasks"] += 1
            if response.get("error") == "shared_model_request_budget_exhausted":
                raise BudgetExceeded("Pi model request budget exhausted; interrupted trace retained")
            return trace
        finally:
            with self.lock:
                self._save_usage()
