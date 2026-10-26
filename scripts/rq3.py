"""RQ3: analyze attempt budgets from existing four-round results, entirely offline."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.report_paper_tables import repeat_curves, exact_repeat_curves, write_csv
from defipi.evaluation.file_task_judges import jsonl
from defipi.evaluation.presence_metrics import _group
from defipi.evaluation.presence_sources import iter_subjects
from defipi.evaluation.repeat_metrics import joint_contact_units
from defipi.evaluation.unified_final_eval import _attempt_budget_summary
from defipi.jsonio import write_json


def analyze(records, output):
    if not records:
        raise ValueError("No selected rounds to analyze")
    groups = defaultdict(lambda: defaultdict(dict))
    for row in records:
        identity = row["identity"]
        repeat = identity["repeat_index"]
        page = (identity["page_id"], identity["question_id"])
        rounds = groups[_group(row)][page]
        if type(repeat) is not int or repeat not in range(4) or repeat in rounds:
            raise ValueError("Expected distinct rounds 0..3; retries are not additional rounds")
        rounds[repeat] = row
    budgets = []
    for key, pages in sorted(groups.items(), key=lambda item: str(item[0])):
        axes = dict(zip(("source_id", "run_id", "target_model", "scenario", "method", "condition", "channel"), key))
        cases = []
        for rounds in pages.values():
            counts = {"valid": 0, "invalid": 0, "unresolved": 0, "missing": 0}
            scores = []
            for repeat in range(4):
                row = rounds.get(repeat)
                if row is not None and row["matched_fields"] is not None:
                    fields = row["matched_fields"]
                    units = joint_contact_units(fields)
                    scores.append({"J_binary": int(units > 0), "J_PER_numerator": units, "matched_fields": fields})
                    counts["valid"] += 1
                else:
                    reason = (row or {}).get("unknown_reason") or "missing"
                    status = "unresolved" if reason in ("started", "unresolved") else "invalid" if reason == "invalid" else "missing"
                    counts[status] += 1
            cases.append((scores, counts))
        budgets.append({**axes, **_attempt_budget_summary(cases, 4)})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "attempt_budget.json", {"rq": 3, "name": "attempt budget", "rounds": 4,
               "model_requests": 0, "groups": budgets})
    write_csv(output / "attempt_budget.csv", (
        {**{k: v for k, v in group.items() if k not in ("J_binary", "J_PER")},
         "metric": metric, "statistic": statistic, **value}
        for group in budgets for metric in ("J_binary", "J_PER")
        for statistic, value in group[metric].items()))
    write_csv(output / "exact_repeat_curves.csv", exact_repeat_curves(records))
    if all("variants" in row for row in records):
        write_csv(output / "repeat_curves.csv", repeat_curves(records))
    return {"rq": 3, "output": str(output), "groups": len(budgets), "model_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--labels", help="Existing presence labels.jsonl")
    source.add_argument("--raw-results", help="Existing report_paper_tables raw_results.jsonl")
    source.add_argument("--runs", nargs="+", help="Explicit frozen run directories (lock.json + results.json)")
    source.add_argument("--manifests", nargs="+", help="RQ1/RQ2/RQ4 reproduction manifests")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.labels:
            records = list(jsonl(args.labels))
        elif args.raw_results:
            records = []
            for raw in jsonl(args.raw_results):
                if raw.get("schema") != "piibench-raw-result-v1":
                    raise ValueError("Unsupported raw-result schema")
                records.extend(raw["channels"].values())
        else:
            runs = args.runs or []
            for path in args.manifests or []:
                manifest = json.loads(Path(path).read_text(encoding="utf-8"))
                if manifest.get("schema") != "piibench-rq-reproduction-v1":
                    raise ValueError("Expected a reproduction manifest")
                runs.extend(run["directory"] for run in manifest["runs"])
            for directory in runs:
                path = Path(directory) / "results.json"
                if not path.is_file() or json.loads(path.read_text()).get("run_status") not in (
                        "completed", "completed_with_errors", "incomplete"):
                    raise ValueError(f"Run has no reportable results: {directory}")
            records = list(iter_subjects({"sources": [{"id": "reproduction", "kind": "runs",
                "path": str(Path.cwd()), "runs": [str(Path(p).resolve()) for p in runs]}]}))
        print(json.dumps(analyze(records, args.output), indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
