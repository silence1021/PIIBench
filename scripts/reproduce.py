"""Compose existing public CLIs into the RQ1, RQ2 and RQ4 workflows."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from cli import presence_judge
from cli.report_paper_tables import export
from defipi.configuration.evaluation import make_config, TEMPLATES
from defipi.evaluation import final_eval_dispatch as dispatch
from defipi.evaluation.file_task_judges import jsonl
from defipi.evaluation.presence_judges import decisions, validate_store
from defipi.jsonio import write_json
from defipi.paths import PROJECT_ROOT

METHODS = ("noguard", "gepa", "piiguard", "webcloak", "autoguard", "muzzle")
SCENARIOS = ("academic", "news")
DEFAULT_IMAGE = "piiguard-agents:chromium156"
RQ_NAMES = {1: "defense effectiveness", 2: "attack processing", 3: "attempt budget", 4: "native agents"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def templates_for(rq):
    result = []
    for path in sorted(TEMPLATES.glob("*.json")):
        config = read(path)
        axes = config["experiment"]
        if rq == 4:
            included = axes.get("harness_type") in ("codex", "claude", "dsh")
        else:
            included = (axes["mode"] == "api" or axes.get("harness_type") == "pi-agent")
        condition = "on" if rq == 2 else "off"
        if included and condition in config["allowed_conditions"]:
            result.append(path.stem)
    return result


def image_id(image):
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        return image
    result = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                            check=True, capture_output=True, text=True, timeout=30)
    resolved = result.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", resolved):
        raise ValueError(f"Docker did not resolve an immutable image ID for {image}")
    return resolved


def prepare(args, rq):
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError(f"Choose a new output directory: {output}")
    templates = args.templates or templates_for(rq)
    for name, values in (("templates", templates), ("methods", args.methods), ("scenarios", args.scenarios)):
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate {name} would schedule the same condition twice")
    uses_agent = any(read(TEMPLATES / f"{name}.json")["experiment"]["mode"] == "agent"
                     for name in templates)
    image = image_id(args.container_image) if uses_agent else None
    output.mkdir(parents=True)
    dataset = Path(args.dataset).resolve()
    runs = []
    condition = "on" if rq == 2 else "off"
    for template in templates:
        axes = read(TEMPLATES / f"{template}.json")["experiment"]
        variant = "file" if axes["mode"] == "agent" and axes["web_injection_channel"] == "url" else "answer"
        for method in args.methods:
            for scenario in args.scenarios:
                source = dataset / method / scenario
                run_name = f"rq{rq}-{template}-{method}-{scenario}"
                config = make_config(template, source / "tasks.json", source / "artifact.json",
                                     output / "runs" / run_name, condition=condition,
                                     concurrency=args.concurrency, container_image=image,
                                     max_model_requests=args.max_model_requests, task_variant=variant)
                config.update(task_id=run_name, repeats=4)
                if axes["mode"] == "agent":
                    config["agent_pool_dir"] = str(output / "agent_slots")
                config_path = output / "configs" / f"{run_name}.json"
                write_json(config_path, config)
                lock = dispatch.prepare(config_path)
                runs.append({"template": template, "method": method, "scenario": scenario,
                             "task_variant": variant, "config": str(config_path),
                             "directory": lock["artifacts"]["output_dir"],
                             "lock": lock["artifacts"]["lock_path"],
                             "call_plan": dispatch.call_plan(lock)})
    manifest = {"schema": "piibench-rq-reproduction-v1", "rq": rq, "name": RQ_NAMES[rq],
                "condition": condition, "repeats": 4, "dataset": str(dataset),
                "container_image": image, "model_requests_during_preparation": 0, "runs": runs}
    write_json(output / "manifest.json", manifest)
    return {"manifest": str(output / "manifest.json"), **plan(manifest)}


def load_manifest(path, rq):
    manifest = read(path)
    if manifest.get("schema") != "piibench-rq-reproduction-v1" or manifest.get("rq") != rq:
        raise ValueError(f"Expected an RQ{rq} reproduction manifest")
    if not manifest.get("runs"):
        raise ValueError("The manifest contains no runs")
    return manifest


def plan(manifest):
    plans = [dispatch.validate(run["lock"])["call_plan"] for run in manifest["runs"]]
    return {"rq": manifest["rq"], "conditions": len(plans),
            "planned_target_calls": sum(p["planned_target_calls"] for p in plans),
            "planned_filter_calls": sum(p["planned_filter_calls"] for p in plans),
            "call_plans": plans, "model_requests": 0}


def run_batch(manifest, *, resume):
    from defipi.config import load_local_env
    # Validate every frozen condition before starting the first one.
    plan(manifest)
    load_local_env()
    for run in manifest["runs"]:
        code = dispatch.run(run["lock"], resume=resume)
        if code:
            return {"rq": manifest["rq"], "stopped_at": run["directory"], "exit_code": code}
    return {"rq": manifest["rq"], "completed_conditions": len(manifest["runs"]), "exit_code": 0}


def score_prepare(args, manifest):
    runs = [run["directory"] for run in manifest["runs"]]
    for directory in runs:
        result = Path(directory) / "results.json"
        if not result.is_file() or read(result).get("run_status") not in (
                "completed", "completed_with_errors", "incomplete"):
            raise ValueError(f"Run has no reportable results: {directory}")
    command = ["prepare", "--runs", *runs, "--source-id", args.source_id,
               "--registry", str(Path(args.registry).resolve()), "--output", str(Path(args.output).resolve())]
    if args.append:
        command.append("--append")
    presence_judge.main(command)


def report(args, rq):
    if args.jobs:
        validate_store(Path(args.jobs).resolve())
        records = list(decisions(Path(args.jobs).resolve()))
    else:
        records = list(jsonl(args.labels))
    if not records:
        raise ValueError("No labels to report")
    result = export(records, args.output, bootstrap_samples=args.bootstrap_samples)
    output = Path(args.output)
    with (output / "labels.jsonl").open("w", encoding="utf-8") as stream:
        for row in records:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_json(output / "rq_metadata.json", {"rq": rq, "name": RQ_NAMES[rq], "model_requests": 0})
    return result


def main(rq, argv=None):
    parser = argparse.ArgumentParser(description=f"RQ{rq}: {RQ_NAMES[rq]}")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Offline: freeze evaluation locks from prepared inputs")
    p.add_argument("--output", required=True)
    p.add_argument("--dataset", required=True, help="Prepared evaluation directory from scripts/experiments.sh prepare (see README)")
    p.add_argument("--templates", nargs="+", choices=templates_for(rq), help="Default: all available presets for this RQ")
    p.add_argument("--methods", nargs="+", choices=METHODS, default=["gepa", "webcloak"] if rq == 4 else list(METHODS))
    p.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    p.add_argument("--concurrency", type=int, default=1, choices=range(1, 41), metavar="1..40")
    p.add_argument("--container-image", default=DEFAULT_IMAGE, help="Agent image; tags are resolved to immutable IDs")
    p.add_argument("--max-model-requests", type=int, default=20, help="Positive model-request limit per agent trajectory")
    for name in ("plan", "run", "score-prepare"):
        p = commands.add_parser(name)
        p.add_argument("--manifest", required=True)
        if name == "run":
            p.add_argument("--resume", action="store_true")
        elif name == "score-prepare":
            p.add_argument("--output", required=True, help="Presence jobs directory")
            p.add_argument("--source-id", default="reproduction", help="Use the same ID for matched RQ1/RQ2 runs")
            p.add_argument("--registry", default=str(PROJECT_ROOT / "configs/evaluation_models.json"))
            p.add_argument("--append", action="store_true")
    p = commands.add_parser("judge", help="Run or inspect presence jobs; forwards to cli.presence_judge")
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    p = commands.add_parser("report", help="Offline: presence, exact fields, repeat curves and A/F tables")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--jobs")
    source.add_argument("--labels")
    p.add_argument("--output", required=True)
    p.add_argument("--bootstrap-samples", type=int, default=1000)
    args = parser.parse_args(argv)
    if getattr(args, "bootstrap_samples", 0) < 0 or getattr(args, "max_model_requests", 1) < 1:
        parser.error("Budgets must be positive; bootstrap samples must be nonnegative")
    try:
        if args.command == "prepare":
            result = prepare(args, rq)
        elif args.command == "judge":
            return presence_judge.main(args.arguments)
        elif args.command == "report":
            result = report(args, rq)
        else:
            manifest = load_manifest(args.manifest, rq)
            if args.command == "plan":
                result = plan(manifest)
            elif args.command == "score-prepare":
                return score_prepare(args, manifest)
            else:
                result = run_batch(manifest, resume=args.resume)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get("exit_code", 0)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{exc}\n")
