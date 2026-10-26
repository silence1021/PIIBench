"""Compose input, execution and method settings from a named preset (offline)."""

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT
import argparse
import json
from pathlib import Path

from defipi.evaluation import final_eval_dispatch as dispatch
from defipi.benchmark.files import load_tasks
from defipi.artifacts.contract import load_artifact
from defipi.config import load_config_file
from .schema import Field, validate_config

ROOT = _PACKAGE_ROOT
TEMPLATES = ROOT / "configs/evaluation"


def make_config(template, tasks, artifact, output_dir, *, condition,
                concurrency, max_model_requests=None, container_image=None, task_variant="answer"):
    config = load_config_file(TEMPLATES / f"{template}.json")
    axes = config["experiment"]
    allowed = config.pop("allowed_conditions")
    fields = {"condition": Field(str, choices=allowed), "concurrency": Field(int, minimum=1, maximum=40)}
    arguments = {"condition": condition, "concurrency": concurrency}
    if axes["mode"] == "agent":
        fields.update({"--container-image": Field(str), "--max-model-requests": Field(int, minimum=1)})
        arguments.update({"--container-image": container_image, "--max-model-requests": max_model_requests})
    validate_config(arguments, fields)

    if task_variant not in ("answer", "file"):
        raise ValueError("task_variant must be answer or file")
    if task_variant == "file":
        if axes["mode"] != "agent":
            raise ValueError("File delivery requires an Agent preset")
        from .agent_file_batch import file_tasks
        converted = file_tasks(load_tasks(tasks))
        destination = Path(output_dir).resolve()
        tasks = destination.parent / ("." + destination.name + "-file-tasks.json")
        serialized = json.dumps(converted, ensure_ascii=False, indent=2) + "\n"
        if tasks.exists() and tasks.read_text() != serialized:
            raise ValueError(f"File task manifest already exists with different content: {tasks}")
        tasks.parent.mkdir(parents=True, exist_ok=True)
        tasks.write_text(serialized)
    scenario = load_tasks(tasks)["scenario"]
    method = load_artifact(artifact, url_channel=axes["web_injection_channel"] == "url")["method_id"]
    axes.pop("defense_method", None)
    axes["attack_method"] = ("filter" if axes["web_injection_channel"] == "html" else "url_prompt") if condition == "on" else "off"
    config.update(task_id=f"{template}-{scenario}-{method}-{condition}",
                  tasks=str(Path(tasks).resolve()), artifact=str(Path(artifact).resolve()), output_dir=str(Path(output_dir).resolve()),
                  api_concurrency_per_task=concurrency)
    if "agent_concurrency" in config:
        config["agent_concurrency"] = concurrency
        config["agent_pool_dir"] = str((ROOT / config["agent_pool_dir"]).resolve())
    if axes["attack_method"] == "filter":
        config["filter"]["prompt_profile"] = scenario
    else:
        config.pop("filter", None)
    adapter = config.pop("adapter_name", None)
    from defipi.execution.bindings import bind_adapter
    bind_adapter(config, adapter, container_image, max_model_requests)
    if "adapter" in config:
        config["adapter"]["tool_config"].update(config.pop("tool_config"))
        config["adapter"]["timeout_seconds"] = config.pop("adapter_timeout_seconds")
    if condition == "on" and axes["web_injection_channel"] == "url":
        prompt = config["url_prompt_defense"]
        prompt["prompt"] = (ROOT / prompt.pop("prompt_file")).read_text()
    else:
        config.pop("url_prompt_defense", None)
    dispatch.validate_config(config)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, choices=sorted(p.stem for p in TEMPLATES.glob("*.json")))
    parser.add_argument("--task-variant", choices=("answer", "file"), default="answer")
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config-out", required=True)
    parser.add_argument("--condition", choices=("off", "on"), required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--max-model-requests", type=int)
    parser.add_argument("--container-image", help="Built Docker image tag for agent presets")
    args = parser.parse_args(argv)
    try:
        config = make_config(args.template, args.tasks, args.artifact, args.output_dir, condition=args.condition, concurrency=args.concurrency,
                             max_model_requests=args.max_model_requests, container_image=args.container_image, task_variant=args.task_variant)
        path = Path(args.config_out).resolve()
        if path.exists():
            raise ValueError(f"Config already exists: {path}")
        # Validate the concrete binding before writing it; this performs no inference.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "config.json"
            probe.write_text(json.dumps(config))
            plan = dispatch.call_plan(dispatch.build_lock(probe))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
        print(json.dumps({"config": str(path), "call_plan": plan}, indent=2))
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
