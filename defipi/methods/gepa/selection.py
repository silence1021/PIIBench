"""Resolve tied GEPA position winners on additional development pages.

Each position winner already uses the full search ordering, including length
and ID. Across those winners, objective/D/J screening comes next. Only ties
enter the additional-page comparison, followed by length/ID if still tied.
prepare is offline; run makes the explicitly planned model requests.
"""
import argparse
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path

from defipi.artifacts import write_artifact
from defipi.benchmark.files import load_pages, load_tasks
from defipi.concurrency import FileSlots, RequestLimiter
from defipi.config import load_config_file, load_local_env
from defipi.configuration.schema import Field, validate_config
from defipi.evaluation import final_eval_dispatch as dispatch
from defipi.evaluation import unified_final_eval as engine
from defipi.methods.gepa.export import export
from defipi.methods.gepa.recoverability_pareto import POSITIONS, ranking_key


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    engine._atomic_json(Path(path), value)


def configuration(path):
    data = validate_config(load_config_file(path), {
        **{key: Field(str) for key in ("source", "pages", "tasks", "split_manifest", "reserved_records", "output_dir")},
        "page_count": Field(int, minimum=1), "repeats": Field(int, minimum=1),
        "concurrency": Field(int, minimum=1, maximum=40), "max_attempts": Field(int, minimum=1, maximum=3),
        "joint_weight": Field((int, float), minimum=0), "target": Field(dict), "filter": Field(dict),
    })
    for key in ("source", "pages", "tasks", "split_manifest", "reserved_records", "output_dir"):
        data[key] = str(Path(data[key]).resolve())
    engine.validate_model_settings(data["target"], prefix="target")
    engine.validate_model_settings(data["filter"], filter_role=True, prefix="filter")
    return data


def position_winners(saved):
    """Accept current per-position output and saved legacy position diagnostics."""
    if saved.get("run_status") != "completed":
        raise ValueError("Candidate selection requires a completed GEPA search")
    best = saved["result"]["best"]
    if "candidate_id" in best:
        diagnostics = saved.get("top_candidates_by_position") or {}
        best = {position: (diagnostics.get(position) or {}).get("candidate", {}) for position in POSITIONS}
    pool = {str(c["candidate_id"]): c for c in saved["result"]["pool"]}
    winners = []
    for position in POSITIONS:
        candidate = best.get(position) or {}
        if not candidate:
            continue
        original = pool.get(str(candidate["candidate_id"]))
        if original != candidate or candidate["position"] != position:
            raise ValueError("Position winner does not match its search-pool candidate")
        ranking_key(candidate)  # Validate the existing exact comparison inputs.
        winners.append(deepcopy(candidate))
    if not winners:
        raise ValueError("Search output has no position winners")
    return winners


def screened_candidates(winners):
    """Use objective, D, J; postpone length and ID until after extra pages."""
    key = min(ranking_key(candidate)[:3] for candidate in winners)
    return [candidate for candidate in winners if ranking_key(candidate)[:3] == key]


def development_inputs(config, saved):
    pages, tasks = load_pages(config["pages"]), load_tasks(config["tasks"])
    scenario = saved["seed_meta"]["fragment_profile"]
    if pages["scenario"] != scenario or tasks["scenario"] != scenario:
        raise ValueError("Search, development pages and tasks must have the same scenario")
    expected = config["page_count"]
    if len(pages["pages"]) != expected or len(tasks["tasks"]) != expected:
        raise ValueError(f"Expected exactly {expected} development pages and tasks")
    ids = [page.get("source_record_id") for page in pages["pages"]]
    page_ids = {page["page_id"] for page in pages["pages"]}
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != expected:
        raise ValueError("Development pages require distinct source_record_id values")
    if {task["page_id"] for task in tasks["tasks"]} != page_ids:
        raise ValueError("Each development page requires exactly one task")
    split = read(config["split_manifest"])
    excluded = set(split["heldout_ids"]) | set(split["development_ids"])
    for key in ("feedback_record_ids", "ranking_record_ids", "eval_record_ids", "heldout_record_ids"):
        excluded.update(saved["seed_meta"].get(key, []))
    reserved = read(config["reserved_records"])
    excluded.update(record["id"] for record in reserved)
    if excluded.intersection(ids):
        raise ValueError("Additional pages overlap evaluation or existing optimization records")
    normalized = lambda text: " ".join(text.split())
    reserved_contexts = {normalized(record["context"]) for record in reserved}
    contexts = [normalized(page["source_record"]["context"]) for page in pages["pages"]]
    if len(set(contexts)) != expected or reserved_contexts.intersection(contexts):
        raise ValueError("Additional pages duplicate reserved or other development article contexts")
    page_map = {page["page_id"]: page for page in pages["pages"]}
    for task in tasks["tasks"]:
        if task["task_type"] != "pii" or task["metadata"].get("base_request_id") != "B1":
            raise ValueError("Candidate comparison requires the bound full B1 PII tasks")
        page = page_map[task["page_id"]]
        if task["metadata"].get("source_record_id") != page["source_record_id"]:
            raise ValueError("Task source_record_id differs from its development page")
        if task["reporter_info"] != page["source_record"]["reporter_info"]:
            raise ValueError("Task contact truth differs from its development page")
    return pages, tasks


def prepare(config):
    output = Path(config["output_dir"])
    if output.exists() and any(output.iterdir()):
        raise ValueError("Selection output is not empty; use run to resume or a fresh output directory")
    saved = read(config["source"])
    winners = position_winners(saved)
    if any(Fraction(str(candidate["ranking"]["joint_weight"])) != Fraction(str(config["joint_weight"])) for candidate in winners):
        raise ValueError("Additional-page joint_weight must match the original search objective")
    candidates = screened_candidates(winners)
    plan = {"source": config["source"], "config": config, "position_winner_ids": [c["candidate_id"] for c in winners],
            "comparison_candidate_ids": [c["candidate_id"] for c in candidates], "candidates": candidates,
            "rule": "full ordering within each position; across position winners: objective, D, J; additional-page mean(D + joint_weight * J_binary); length; candidate ID",
            "groups": [], "planned_target_calls": 0, "planned_filter_calls": 0}
    if len(candidates) > 1:
        pages, tasks = development_inputs(config, saved)
        for candidate in candidates:
            cid = candidate["candidate_id"]
            directory = output / f"candidate_{cid}"
            materialized = export(pages, argparse.Namespace(source=config["source"], candidate_id=str(cid)))
            artifact = write_artifact(directory / "artifact", pages, materialized, method_id="gepa",
                                      artifact_id=f"gepa-development-{cid}", code_revision=None)
            task_path = directory / "tasks.json"
            write(task_path, tasks)
            for condition in ("off", "filter"):
                settings = {"task_id": f"gepa-development-{cid}-{condition}", "tasks": str(task_path),
                    "artifact": str(artifact), "output_dir": str(directory / condition),
                    "experiment": {"attack_method": condition, "web_injection_channel": "html", "mode": "api", "harness_type": None},
                    "observation_representation": "html", "repeats": config["repeats"],
                    "api_concurrency_per_task": config["concurrency"], "max_attempts": config["max_attempts"],
                    "target": deepcopy(config["target"])}
                if condition == "filter":
                    settings["filter"] = deepcopy(config["filter"])
                config_path = directory / f"{condition}.json"
                write(config_path, settings)
                lock = dispatch.prepare(config_path)
                calls = dispatch.validate(lock["artifacts"]["lock_path"])["call_plan"]
                plan["planned_target_calls"] += calls["planned_target_calls"]
                plan["planned_filter_calls"] += calls["planned_filter_calls"]
                plan["groups"].append({"candidate_id": cid, "condition": condition,
                    "lock": lock["artifacts"]["lock_path"], "results": lock["artifacts"]["results_path"]})
    write(output / "plan.json", plan)
    report(output)
    return plan


class SelectionRunner(dispatch.DispatchRunner):
    def _initialise_clients(self):
        super()._initialise_clients()
        for executor in self.executors.values():
            executor.request_limiter = self.selection_limiter
            if executor.client is not None:
                executor.client.request_limiter = self.selection_limiter
        if self.filter_client is not None:
            self.filter_client.client.request_limiter = self.selection_limiter


def run(config):
    output = Path(config["output_dir"])
    plan = read(output / "plan.json")
    if plan["config"] != config:
        raise ValueError("Selection configuration changed after preparation")
    load_local_env()
    with FileSlots(output / ".run_lease", 1, busy=RuntimeError("This GEPA selection is already running")):
        limiter = RequestLimiter(config["concurrency"])
        for group in plan["groups"]:
            dispatch.validate(group["lock"])
            lock = read(group["lock"])
            for attempt in range(config["max_attempts"] * 2 - 1):
                runner = SelectionRunner(lock, output_path=group["results"], retry_invalid=config["max_attempts"] - 1)
                runner.selection_limiter = limiter
                code = runner.run(resume=Path(group["results"]).exists())
                if code == 0 or not list(runner._jobs(resume=True)):
                    break
            report(output)
    result = report(output)
    return 0 if result["complete"] else 1


def report(output):
    output = Path(output)
    plan = read(output / "plan.json")
    conditions, complete = [], True
    risks = {str(c["candidate_id"]): [] for c in plan["candidates"]}
    for group in plan["groups"]:
        path = Path(group["results"])
        if not path.exists():
            conditions.append({"candidate_id": group["candidate_id"], "condition": group["condition"], "status": "pending"})
            complete = False
            continue
        state, lock = read(path), read(group["lock"])
        summary = engine._summary(lock, state["slots"])["conditions"][0]
        conditions.append({"candidate_id": group["candidate_id"], "condition": group["condition"], **summary})
        if summary["valid"] != summary["planned"]:
            complete = False
            continue
        scores = [slot["score"] for slot in state["slots"].values() if slot["status"] == "valid"]
        d = Fraction(sum(s["D_numerator"] for s in scores), 3 * len(scores))
        joint = Fraction(sum(s["J_binary"] for s in scores), len(scores))
        risks[str(group["candidate_id"])].append((d, joint))
    result = {"complete": complete, "conditions": conditions, "candidate_id": None}
    if complete:
        def key(candidate):
            values = risks[str(candidate["candidate_id"])]
            if values:
                d = sum(v[0] for v in values) / len(values)
                joint = sum(v[1] for v in values) / len(values)
                objective = d + Fraction(str(plan["config"]["joint_weight"])) * joint
                return objective, d, joint, *ranking_key(candidate)[3:]
            return ranking_key(candidate)
        chosen = min(plan["candidates"], key=key)
        cid = chosen["candidate_id"]
        basis = (f"C{cid} was selected after comparing the tied GEPA position winners on "
                 f"{plan['config']['page_count']} additional development pages."
                 if plan["groups"] else f"C{cid} was selected by the existing development objective/D/J comparison.")
        write(output / "selection.json", {"candidate_id": cid, "selection_basis": basis})
        result["candidate_id"] = cid
    write(output / "summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
        epilog="prepare makes no model calls. run executes/resumes the prepared OFF/ON comparison; incomplete results do not select a winner.")
    parser.add_argument("command", choices=("prepare", "run", "report"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = configuration(args.config)
        if args.command == "prepare":
            result = prepare(config)
            print(json.dumps({k: result[k] for k in ("position_winner_ids", "comparison_candidate_ids", "planned_target_calls", "planned_filter_calls")}, indent=2))
            return 0
        if args.command == "run":
            return run(config)
        result = report(config["output_dir"])
        print(json.dumps(result, indent=2))
        return 0 if result["complete"] else 1
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
