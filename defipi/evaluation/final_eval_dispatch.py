"""Five-axis final evaluation, composed with the unchanged frozen-page engine.

The command adapter is a transport for explicitly bound URL/Agent runners. It
does not turn an API answer into an Agent trajectory or supply browsing tools.
"""
from __future__ import annotations

from defipi.artifacts.contract import load_artifact
from defipi.benchmark.files import load_tasks
from defipi.inputs import get_input
from defipi.configuration.schema import Field, Problem, ValidationError, validate_config as validate_fields

from defipi.concurrency import FileSlots
from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT, git_revision

import copy
from contextlib import nullcontext
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from . import unified_final_eval as engine
from defipi.execution.adapters import resolve_adapter
from defipi.execution.bindings import make_executor
from .field_matching import MATCHER_VERSION, field_in_text
from . import shared_filter


AXES = {
    "attack_method": ("off", "filter", "url_prompt"),
    "web_injection_channel": ("html", "url"),
    "mode": ("api", "agent"),
    "harness_type": ("pi-agent", "codex", "dsh", "claude"),
}
VERSION = "final-eval-dispatch-v2-html-url"


def validate_config(config):
    """Check the complete active input contract before resolving any resources."""
    fields = {
        "task_id": Field(str), "tasks": Field(str), "artifact": Field(str), "output_dir": Field(str),
        "experiment": Field(dict, schema={
            "attack_method": Field(str, choices=AXES["attack_method"]),
            "web_injection_channel": Field(str, choices=AXES["web_injection_channel"]),
            "mode": Field(str, choices=AXES["mode"]),
            "harness_type": Field(str, required=False, nullable=True, choices=AXES["harness_type"]),
        }),
        "observation_representation": Field(str, choices=("html", "backend_native")),
        "repeats": Field(int, minimum=1), "api_concurrency_per_task": Field(int, minimum=1, maximum=40),
        "max_attempts": Field(int, minimum=1, maximum=3), "target": Field(dict),
        "filter": Field(dict, required=False), "adapter": Field(dict, required=False),
        "agent_concurrency": Field(int, required=False, minimum=1, maximum=40),
        "agent_pool_dir": Field(str, required=False),
        "url_prompt_defense": Field(dict, required=False, schema={"prompt": Field(str), "placement": Field(str, choices=("system",))}),
        "shared_filter_results": Field((str, list), required=False),
    }
    def rules(data, problems):
        axes = data.get("experiment")
        axes = axes if isinstance(axes, dict) else {}
        agent = axes.get("mode") == "agent"
        url = axes.get("web_injection_channel") == "url"
        filtered = axes.get("attack_method") == "filter"
        for key in ("task_id", "tasks", "artifact", "output_dir", "agent_pool_dir"):
            if isinstance(data.get(key), str) and not data[key].strip():
                problems.append(Problem(key, "must be non-empty"))
        if agent:
            for key in ("agent_concurrency", "agent_pool_dir", "adapter"):
                if key not in data:
                    problems.append(Problem(key, "required for mode=agent"))
            if axes.get("harness_type") not in AXES["harness_type"]:
                problems.append(Problem("experiment.harness_type", "required for mode=agent"))
        elif axes.get("mode") == "api" and axes.get("harness_type") is not None:
            problems.append(Problem("experiment.harness_type", "must be null or omitted for mode=api"))
        if url and "adapter" not in data:
            problems.append(Problem("adapter", "required for URL input"))
        if not url and not agent and "adapter" in data:
            problems.append(Problem("adapter", "static HTML API does not use an adapter"))
        for key in ("agent_concurrency", "agent_pool_dir"):
            if not agent and key in data:
                problems.append(Problem(key, "only valid for mode=agent"))
        if axes.get("web_injection_channel") in ("html", "url"):
            channel = get_input(axes["web_injection_channel"])
            try:
                channel.validate_attack(axes.get("attack_method"))
            except ValueError as exc:
                problems.append(Problem("experiment.attack_method", str(exc)))
            if data.get("observation_representation") != channel.REPRESENTATION:
                problems.append(Problem("observation_representation", f"must be {channel.REPRESENTATION} for this channel"))
        for name, filter_role in (("target", False), ("filter", True)):
            if name == "filter" and not filtered:
                if name in data:
                    problems.append(Problem(name, "only valid for attack_method=filter"))
                continue
            if name not in data:
                if filter_role:
                    problems.append(Problem(name, "required for attack_method=filter"))
                continue
            if name == "target" and (url or agent) and isinstance(data.get("adapter"), dict):
                continue
            if isinstance(data[name], dict):
                try:
                    engine.validate_model_settings(data[name], filter_role=filter_role,
                                                   transport=(url or agent) and not filter_role, prefix=name)
                except ValidationError as exc:
                    problems.extend(exc.problems)
        if axes.get("attack_method") == "url_prompt":
            if "url_prompt_defense" not in data:
                problems.append(Problem("url_prompt_defense", "required for attack_method=url_prompt"))
            prompt = data.get("url_prompt_defense")
            if isinstance(prompt, dict) and isinstance(prompt.get("prompt"), str) and not prompt["prompt"].strip():
                problems.append(Problem("url_prompt_defense.prompt", "must be non-empty"))
        elif "url_prompt_defense" in data:
            problems.append(Problem("url_prompt_defense", "only valid for attack_method=url_prompt"))
        if "shared_filter_results" in data:
            sources = data["shared_filter_results"]
            if not filtered:
                problems.append(Problem("shared_filter_results", "requires attack_method=filter"))
            if isinstance(sources, list) and (not sources or any(not isinstance(v, str) or not v.strip() for v in sources)):
                problems.append(Problem("shared_filter_results", "must be a non-empty list of paths"))
        adapter = data.get("adapter")
        if isinstance(adapter, dict):
            try:
                validate_fields(adapter, {
                    "harness_type": Field(str, nullable=True, choices=AXES["harness_type"]),
                    "build": Field(str), "command": Field(list, items=Field(str)),
                    "supported_inputs": Field(list, items=Field(str, choices=("html", "url"))),
                    "timeout_seconds": Field((int, float), minimum=1), "tool_config": Field(dict),
                }, prefix="adapter")
            except ValidationError as exc:
                problems.extend(exc.problems)
            if isinstance(adapter.get("build"), str) and not adapter["build"].strip():
                problems.append(Problem("adapter.build", "must be non-empty"))
            command = adapter.get("command")
            if isinstance(command, list):
                if not command or any(isinstance(arg, str) and not arg.strip() for arg in command):
                    problems.append(Problem("adapter.command", "must contain non-empty arguments"))
                for token in ("{request_path}", "{response_path}"):
                    if not any(isinstance(arg, str) and token in arg for arg in command):
                        problems.append(Problem("adapter.command", f"must contain {token}"))
            if isinstance(adapter.get("tool_config"), dict) and not adapter["tool_config"]:
                problems.append(Problem("adapter.tool_config", "must bind active tools"))
            supported = adapter.get("supported_inputs")
            required_input = "url" if url else data.get("observation_representation")
            if isinstance(supported, list) and required_input not in supported:
                problems.append(Problem("adapter.supported_inputs", f"must declare support for {required_input}"))
            if adapter.get("harness_type") != axes.get("harness_type"):
                problems.append(Problem("adapter.harness_type", "must match experiment.harness_type"))
    from defipi.configuration.execution import execution_config_rules
    return validate_fields(config, fields, rules=(rules, execution_config_rules))


def _axes(config):
    axes = copy.deepcopy(config["experiment"])
    return {key: axes.get(key) for key in AXES}


def build_lock(config_path):
    """Offline plan: validate bindings and calculate calls without creating clients."""
    config_path, original = engine._load_config(config_path)
    try:
        validate_config(original)
    except ValidationError as exc:
        raise engine.FinalEvalError(str(exc)) from exc
    config = copy.deepcopy(original)
    axes = _axes(config)
    is_url = axes["web_injection_channel"] == "url"
    is_agent = axes["mode"] == "agent"
    task_id = config["task_id"]
    api_limit = config["api_concurrency_per_task"]
    agent_limit = config["agent_concurrency"] if is_agent else None
    attempts = config["max_attempts"]
    input_channel = get_input(axes["web_injection_channel"])
    try:
        representation = input_channel.normalize(config)
    except ValueError as exc:
        raise engine.FinalEvalError(str(exc)) from exc
    filtered = axes["attack_method"] == "filter"
    filter_config = copy.deepcopy(config["filter"]) if filtered else {"enabled": False}
    if filtered:
        filter_config.update(enabled=True, stage=input_channel.FILTER_STAGE)
    config["filter"] = filter_config
    config["channels"] = ["filtered"] if filtered else ["raw"]
    target = config["target"]
    api_ids = {target["api_id"] or (target["backend"], target["model"])}
    if filtered:
        api_ids.add(filter_config["api_id"] or (filter_config["backend"], filter_config["model"]))
    config["concurrency"] = agent_limit if is_agent else api_limit * len(api_ids)
    runtime = engine._resolve_runtime(
        config, target_resolver=copy.deepcopy if is_url or is_agent else engine._resolve_target,
        runtime_formats=(input_channel.RUNTIME_FORMAT,))
    runtime["observation_format"] = input_channel.RUNTIME_FORMAT
    runtime["renderer"] = None
    prompt = config["url_prompt_defense"] if axes["attack_method"] == "url_prompt" else None
    adapter = resolve_adapter(config["adapter"], config_path.parent, axes, representation) if is_url or is_agent else None
    artifact_path = engine._resolve_from(config_path.parent, config["artifact"], "artifact")
    tasks_path = engine._resolve_from(config_path.parent, config["tasks"], "tasks")
    benchmark = load_tasks(tasks_path)
    artifact = load_artifact(artifact_path, url_channel=is_url)
    engine._check_benchmark(benchmark, artifact)
    methods, tasks = [artifact], benchmark["tasks"]
    axes["defense_method"] = artifact["method_id"]
    if adapter and (adapter["tool_config"].get("container_image") or
                    adapter["build"].startswith(("native-url-agent-", "codex-native-url-", "claude-native-url-"))):
        if adapter["tool_config"].get("container_image"):
            from defipi.execution.agents.container import validate_request
        elif adapter["build"].startswith("codex-native-url-"):
            from defipi.execution.agents.codex import validate_request
        elif adapter["build"].startswith("claude-native-url-"):
            from defipi.execution.agents.claude import validate_request
        else:
            from defipi.execution.agents.dsh import validate_request
        page = methods[0]["pages"][0]
        try:
            validate_request({"schema_version": "final-eval-adapter-v1", "experiment": axes,
                              "target": runtime["target"], "tool_config": adapter["tool_config"],
                              "input": {"kind": "url" if is_url else representation, "content": page.get("url")},
                               "url_prompt_defense": prompt,
                              "limits": {"api_concurrency": 1}})
        except (ValueError, KeyError, TypeError) as exc:
            raise engine.FinalEvalError(f"Native Agent binding is incomplete: {exc}") from exc
    output = engine._resolve_from(config_path.parent, config["output_dir"], "output_dir")
    for page in methods[0]["pages"]:
        if not is_url and output.is_relative_to(Path(page["resource_root"])):
            raise engine.FinalEvalError("output_dir must be outside frozen resource roots")
    dispatch = {"version": VERSION, "axes": axes, "task_id": task_id,
                "observation_representation": representation, "api_concurrency_per_task": api_limit,
                "max_attempts": attempts, "adapter": adapter, "url_prompt_defense": prompt}
    if config.get("shared_filter_results") is not None:
        if not filtered or representation != "html":
            raise engine.FinalEvalError("shared_filter_results currently requires HTML with Filter ON")
        sources = config["shared_filter_results"]
        source = ([engine._resolve_from(config_path.parent, value, "shared_filter_results") for value in sources]
                  if isinstance(sources, list) else engine._resolve_from(config_path.parent, sources, "shared_filter_results"))
        dispatch["shared_filter"] = shared_filter.bind_results(source)
    if is_agent:
        pool = config["agent_pool_dir"]
        dispatch["agent_pool_dir"] = str(engine._resolve_from(config_path.parent, pool, "agent_pool_dir"))
        dispatch["agent_concurrency"] = agent_limit
    runtime["dispatch"] = dispatch
    lock = {"schema_version": engine.LOCK_SCHEMA_VERSION, "protocol_version": engine.PROTOCOL_VERSION,
            "code_revision": git_revision(),
            "config": {"path": str(config_path)},
            "benchmark": {"benchmark_id": benchmark["benchmark_id"], "scenario": benchmark["scenario"], "tasks": str(tasks_path), "artifact": str(artifact_path)},
            "methods": methods, "tasks": tasks, "runtime": runtime,
            "artifacts": {"output_dir": str(output), "results_path": str(output / "results.json"),
                          "lock_path": str(output / "lock.json")}}
    shared_filter.validate_coverage(lock)
    lock["run_id"] = engine.uuid.uuid4().hex
    return lock


def call_plan(lock):
    plan = engine._call_plan(lock)
    dispatch = lock["runtime"]["dispatch"]
    plan.update(dispatch["axes"], task_id=dispatch["task_id"],
                observation_representation=dispatch["observation_representation"],
                api_concurrency_per_task=dispatch["api_concurrency_per_task"],
                agent_concurrency=dispatch.get("agent_concurrency"),
                max_attempts=dispatch["max_attempts"],
                target_call_unit="trajectory" if dispatch["axes"]["mode"] == "agent" else "answer",
                planned_backend_api_calls=None if dispatch.get("adapter") else plan["planned_target_calls"])
    if dispatch.get("shared_filter"):
        plan["planned_filter_calls"] = 0
        plan["reused_filter_outputs"] = len(dispatch["shared_filter"]["selected_keys"])
    if dispatch.get("adapter") and dispatch["axes"]["harness_type"] in {"pi-agent", "codex", "dsh", "claude"}:
        tool_limit = dispatch["adapter"]["tool_config"].get("max_tool_calls")
        if tool_limit:
            plan.update(tool_count_threshold=tool_limit, tool_count_policy="observe_only")
        request_limit = dispatch["adapter"]["tool_config"].get("max_model_requests")
        if request_limit:
            plan.update(max_model_requests_per_attempt=request_limit,
                        max_model_requests_first_attempts=plan["planned_target_calls"] * request_limit,
                        max_model_requests_including_retries=plan["planned_target_calls"] * request_limit * dispatch["max_attempts"])
    return plan


def prepare(config_path):
    lock = build_lock(config_path)
    path = Path(lock["artifacts"]["lock_path"])
    if path.exists():
        raise engine.FinalEvalError(f"Final-eval lock already exists: {path}")
    engine._atomic_json(path, lock)
    return lock


def validate(lock_path):
    lock = engine._read_json(lock_path, "final-eval lock")
    if not (lock.get("runtime") or {}).get("dispatch"):
        raise engine.FinalEvalError("Use a public HTML/URL config from cli.make_eval_config and prepare a new lock")
    engine._validate_lock(lock)
    shared_filter.validate_coverage(lock)
    return {"valid": True, "run_id": lock["run_id"], "call_plan": call_plan(lock)}


def _pi_tool_diagnostics(trajectory, reporter_info):
    """Benchmark-only diagnostics; ground truth is never sent to the adapter."""
    tools, exposed = [], set()
    fields = ("email", "phone", "address")
    for step in trajectory["steps"]:
        for tool in step["tools"]:
            # File links are only text. Do not open linked snapshots or OCR images.
            model_visible = tool.get("model_visible", True)
            blocks = (tool.get("result") or {}).get("content", [])
            texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
            matched = ([field for field in fields if any(
                field_in_text(field, text, reporter_info.get(field)) for text in texts)]
                if reporter_info and tool["status"] == "completed" and model_visible else None)
            exposed.update(matched or [])
            tools.append({"turn": step["turn"], "call_id": tool["call_id"], "tool": tool["name"],
                          "parent_call_id": tool.get("parent_call_id"), "model_visible": model_visible,
                          "status": tool["status"], "is_error": tool.get("is_error"),
                          "output_line": tool.get("end_event_line"), "target_fields_in_tool_text": matched,
                          "image_blocks_returned": tool.get("image_blocks_returned", 0) if model_visible else 0})
    return {"schema_version": "pi-tool-observation-diagnostics-v1", "matcher_version": MATCHER_VERSION,
            "trace_path": trajectory["trace_path"], "ground_truth_available": bool(reporter_info),
            "target_fields_in_tool_text": sorted(exposed) if reporter_info else None,
            "tool_trajectory": tools, "returned_images_index_path": trajectory["returned_images_index_path"],
            "image_blocks_returned": trajectory["image_blocks_returned"],
            "provider_image_delivery": trajectory["provider_image_delivery"]}


def _native_tool_diagnostics(response, reporter_info):
    tools, exposed = [], set()
    for item in response["trace"]:
        blocks = (item.get("output") or {}).get("content", [])
        texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
        matched = ([field for field in ("email", "phone", "address") if any(
            field_in_text(field, text, reporter_info.get(field)) for text in texts)]
            if reporter_info and not item.get("is_error") else None)
        exposed.update(matched or [])
        tools.append({"call_id": item.get("call_id"), "tool": item["name"], "is_error": item.get("is_error"),
                      "target_fields_in_tool_text": matched,
                      "image_blocks_returned": sum(block.get("type") == "image" for block in blocks)})
    return {"schema_version": "native-tool-observation-diagnostics-v1", "matcher_version": MATCHER_VERSION,
            "trace_path": response["trajectory_path"], "ground_truth_available": bool(reporter_info),
            "observation_surface": "native_harness_tool_return", "provider_evidence_dir": response["provider_evidence_dir"],
            "target_fields_in_tool_text": sorted(exposed) if reporter_info else None, "tool_trajectory": tools}


class DispatchRunner(engine._FinalRunner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dispatch = self.lock["runtime"]["dispatch"]
        self.context = threading.local()
        self.tasks_by_id = {task["case_id"]: task for task in self.lock["tasks"]}

    def _initialise_clients(self):
        # Construct each API counter independently inside this task. A Filter on
        # the same API shares its counter; a different task gets different ones.
        runtime = self.lock["runtime"]
        limiters = {}

        def limiter(settings):
            key = settings.get("api_id") or (settings["backend"], settings["model"])
            return limiters.setdefault(key, engine.RequestLimiter(self.dispatch["api_concurrency_per_task"]))

        self.executors = {}
        for key, target in engine._targets(runtime).items():
            executor = make_executor(self.dispatch, target, self.context, self.output_path.parent,
                                     limiter(target), self.evaluator_class)
            self.executors[key] = executor
        self.filter_client = None
        settings = runtime["filter"]
        if settings.get("enabled") and not self.dispatch.get("shared_filter"):
            self.filter_client = engine.LLMHTMLFilter(
                model=settings["model"], backend=settings["backend"], temperature=settings["temperature"],
                client_settings=settings["client_settings"], retry_backoff_seconds=0, input_format="html",
                prompt_variant=settings.get("prompt_variant"), prompt_profile=settings.get("prompt_profile"),
                prompt_override=settings.get("prompt_override"), max_retries=1,
                max_response_tokens=settings["max_response_tokens"],
                required_finish_reasons=settings["required_finish_reasons"],
                reasoning_effort=settings.get("reasoning_effort"), request_limiter=limiter(settings))
        self.renderer = None

    def _page_observation(self, method, task, page, html):
        return get_input(self.dispatch["axes"]["web_injection_channel"]).observation(page, html)

    def _filter_once(self, method, task, page, filter_input, repeat):
        if self.dispatch.get("shared_filter"):
            return shared_filter.reuse(self, method, task, page, filter_input, repeat)
        return super()._filter_once(method, task, page, filter_input, repeat)

    def _evaluate(self, job):
        method, task, _, repeat, _ = job
        page = self._methods_pages()[method["method_id"]][task["page_id"]]
        self.context.value = {"case_id": task["case_id"], "page_id": task["page_id"], "repeat_index": repeat}
        lease = (FileSlots(self.dispatch["agent_pool_dir"], self.dispatch["agent_concurrency"])
                 if self.dispatch["axes"]["mode"] == "agent" else nullcontext())
        with lease:
            slot_id = super()._evaluate(job)
        if self.dispatch.get("adapter"):
            with self.guard:
                slot = self.state["slots"][slot_id]
                attempt = slot["attempts"][-1]
                if attempt.get("response_ref"):
                    response = json.loads(self._read_evidence(attempt["response_ref"]))
                    if response.get("tool_calls") is not None:
                        for row in (attempt, slot):
                            row["tool_calls"] = response["tool_calls"]
                            row["tool_budget"] = copy.deepcopy(response.get("tool_budget"))
                    representations = response.get("observation_representations")
                    attempt["backend_observation_representations"] = representations
                    slot["backend_observation_representations"] = representations
                    if self.dispatch["axes"]["harness_type"] == "pi-agent" and response.get("trajectory_path"):
                        trajectory = json.loads(Path(response["trajectory_path"]).read_text(encoding="utf-8"))
                        diagnostics = _pi_tool_diagnostics(trajectory, task.get("reporter_info"))
                        reference = self._evidence_ref("pi-tool-diagnostics", diagnostics)
                        attempt["observation_diagnostics_ref"] = reference
                        slot["observation_diagnostics_ref"] = reference
                    elif (response.get("adapter_version", "").startswith("native-url-agent-")
                          and response.get("trajectory_path")):
                        diagnostics = _native_tool_diagnostics(response, task.get("reporter_info"))
                        reference = self._evidence_ref("native-tool-diagnostics", diagnostics)
                        attempt["observation_diagnostics_ref"] = reference
                        slot["observation_diagnostics_ref"] = reference
                    if response.get("retryable") is False:
                        slot["retryable"] = False
            self._save()
        return slot_id

    def _jobs(self, resume):
        # Filter failures must not consume the independent target-call budget.
        for job in super()._jobs(resume=False):
            yield job
        if not resume:
            return
        runtime = self.lock["runtime"]
        method = self.lock["methods"][0]
        for task in self.lock["tasks"]:
            for repeat in range(runtime["repeats"]):
                channel = runtime["channels"][0]
                slot = self.state["slots"].get(engine._slot_id(self.lock, method, task, channel, repeat))
                if not slot or slot.get("status") != "invalid":
                    continue
                if slot.get("retryable") is False:
                    continue
                history = slot.get("attempts", [])
                phase = history[-1].get("phase") if history else "prepare"
                if phase == "filter":
                    record = self.state["filter_attempts"].get(slot.get("filter_id"), {})
                    spent = int(record.get("attempts", 0))
                else:
                    spent = sum(item.get("phase") == phase for item in history)
                if spent < self.dispatch["max_attempts"]:
                    yield method, task, channel, repeat, None

    def _save(self):
        with self.guard:
            axes = self.dispatch["axes"]
            common = {**axes, "task_id": self.dispatch["task_id"],
                      "defense_variant": self.lock["methods"][0].get("defense_variant"),
                      "observation_representation": self.dispatch["observation_representation"],
                      "target_model": self.lock["runtime"]["target"]["model"]}
            self.state["experiment"] = common
            self.state["call_plan"] = call_plan(self.lock)
            summary = self.state.get("summary") or {}
            rows = list(self.state["slots"].values())
            if isinstance(summary, dict):
                rows += summary.get("cases", []) + summary.get("conditions", [])
            for row in rows:
                row.update(common)
                task = self.tasks_by_id.get(row.get("case_id"))
                if task:
                    row.update(page_id=task["page_id"], question_id=task["metadata"]["question_id"],
                               scenario=self.lock["benchmark"]["scenario"])
            attempts = [attempt for slot in self.state["slots"].values() for attempt in slot.get("attempts", [])
                        if "tool_calls" in attempt]
            if self.dispatch["axes"]["harness_type"] in {"pi-agent", "codex", "dsh", "claude"}:
                self.state["tool_call_statistics"] = {
                    "policy": "observe_only", "attempts": len(attempts),
                    "threshold": self.dispatch["adapter"]["tool_config"].get("max_tool_calls"),
                    "total_excess_tool_calls": sum(max(0, row["tool_calls"] - (row.get("tool_budget") or {}).get("limit", row["tool_calls"])) for row in attempts),
                    "total_tool_calls": sum(row["tool_calls"] for row in attempts),
                    "attempts_over_threshold": sum(bool((row.get("tool_budget") or {}).get("exceeded")) for row in attempts),
                    "slots_over_threshold": sum(any(bool((a.get("tool_budget") or {}).get("exceeded"))
                                                      for a in slot.get("attempts", [])) for slot in self.state["slots"].values())}
            super()._save()


def run(lock_path, *, resume=False, evaluator_class=engine.LLMEvaluator):
    validate(lock_path)
    lock = engine._read_json(lock_path, "final-eval lock")
    dispatch = lock["runtime"]["dispatch"]
    output = Path(lock["artifacts"]["output_dir"])
    with FileSlots(output / ".run_lease", 1, busy=engine.FinalEvalError("This final-eval task is already running")):
        # Each pass retries known failures once; persisted per-call budgets also
        # apply after a process restart. Unresolved requests are never resent.
        for index in range(dispatch["max_attempts"] * 2 - 1):
            runner = DispatchRunner(lock, output_path=lock["artifacts"]["results_path"],
                                    retry_invalid=dispatch["max_attempts"] - 1,
                                    evaluator_class=evaluator_class)
            code = runner.run(resume=resume or index > 0)
            if code == 0 or not list(runner._jobs(resume=True)):
                break
        engine._atomic_json(output / "exit.json", {"exit_code": code,
            "run_status": runner.state["run_status"], "run_id": lock["run_id"]})
    return code
