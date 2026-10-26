"""Export offline metric tables and optional text-bearing raw results; no model calls."""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import json
from itertools import combinations
from pathlib import Path
from defipi.evaluation.file_task_judges import jsonl, write_json
from defipi.evaluation.file_task_evidence import fingerprint
from defipi.evaluation.presence_metrics import summarize, VARIANTS, _group


def write_csv(path, rows):
    rows = list(rows)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def repeat_curves(records):
    groups = defaultdict(list)
    for row in records:
        groups[_group(row)].append(row)
    for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
        axes = dict(zip(("source_id", "run_id", "target_model", "scenario", "method", "condition", "channel"), key))
        for variant in VARIANTS:
            pages = defaultdict(dict)
            for row in rows:
                i = row["identity"]
                page = (i["page_id"], i["question_id"])
                if i["repeat_index"] in pages[page]:
                    raise ValueError("Duplicate page/round")
                pages[page][i["repeat_index"]] = row["variants"][variant]
            complete = [v for v in pages.values() if all(v.get(n) in ("PRESENT", "ABSENT") for n in range(4))]
            for k in range(1, 5):
                subsets = list(combinations(range(4), k))
                denominator = len(complete) * len(subsets)
                yield {**axes, "variant": variant, "k": k, "planned_pages": len(pages),
                       "evaluable_pages": len(complete), "unknown_or_missing_pages": len(pages)-len(complete),
                       "PRESENT_at_k": sum(any(v[n] == "PRESENT" for n in indices) for v in complete for indices in subsets)/denominator if denominator else None,
                       "PRESENT_all_k": sum(all(v[n] == "PRESENT" for n in indices) for v in complete for indices in subsets)/denominator if denominator else None}



def exact_repeat_curves(records):
    from defipi.evaluation.repeat_metrics import joint_contact_units
    groups = defaultdict(list)
    for row in records:
        groups[_group(row)].append(row)
    for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
        axes = dict(zip(("source_id", "run_id", "target_model", "scenario", "method", "condition", "channel"), key))
        pages = defaultdict(dict)
        for row in rows:
            i = row["identity"]
            pages[(i["page_id"], i["question_id"])][i["repeat_index"]] = row["matched_fields"]
        complete = [[joint_contact_units(rounds[n]) for n in range(4)] for rounds in pages.values()
                    if set(rounds) == {0,1,2,3} and all(v is not None for v in rounds.values())]
        for k in range(1, 5):
            subsets = list(combinations(range(4), k))
            denominator = len(complete) * len(subsets)
            yield {**axes, "k":k, "planned_pages":len(pages), "evaluable_pages":len(complete),
                "JDC_at_k":sum(max(v[n] for n in indices) for v in complete for indices in subsets)/(3*denominator) if denominator else None,
                "JDC_all_k":sum(min(v[n] for n in indices) for v in complete for indices in subsets)/(3*denominator) if denominator else None}


def export_raw(records, subjects, output):
    """One row per selected slot; answer/file labels retain their own vote and attempt provenance."""
    contents = {s["subject_id"]: s for s in subjects}
    pairs = defaultdict(dict)
    for row in records:
        i = row["identity"]
        key = (row["source_id"], i["run_id"], i["slot_key"], i["selected_attempt"])
        if row["channel"] in pairs[key]:
            raise ValueError("Duplicate selected channel")
        if row["subject_id"] not in contents:
            raise ValueError("Missing subject text for raw export")
        subject = contents[row["subject_id"]]
        if subject["source_fingerprint"] != row["source_fingerprint"]:
            raise ValueError("Raw subject differs from selected labels")
        pairs[key][row["channel"]] = {**row, "text": subject["text"], "truth": subject["truth"]}
    condition_dir = output.parent / "raw_conditions"
    condition_dir.mkdir()
    manifest = {}
    with output.open("w", encoding="utf-8") as stream:
        for key, channels in pairs.items():
            first = next(iter(channels.values()))
            identity = first["identity"]
            row = {"schema": "piibench-raw-result-v1", "source_id": key[0],
                   "target_model": first["target_model"], "identity": identity, "channels": channels}
            line = json.dumps(row, ensure_ascii=False)+"\n"
            stream.write(line)
            axes = {"source_id":key[0], "run_id":key[1], "target_model":first["target_model"],
                    **{k:identity.get(k) for k in ("method","scenario","condition","mode","surface","harness_signature","target_signature")}}
            condition_id = fingerprint(axes)[:24]
            path = condition_dir / (condition_id + ".jsonl")
            with path.open("a", encoding="utf-8") as condition:
                condition.write(line)
            metadata = manifest.setdefault(condition_id, {"axes":axes, "path":str(path.relative_to(output.parent)),
                "selected_slots":0, "run_metadata":first.get("provenance", {}).get("run_metadata")})
            metadata["selected_slots"] += 1
    write_json(condition_dir / "manifest.json", {"schema":"piibench-raw-conditions-v1", "model_requests":0,
        "conditions":list(manifest.values()), "coverage":"Only supplied selected slots; no completeness claim."})


def export(records, output, *, subjects=None, bootstrap_samples=1000):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    report = summarize(records, bootstrap_samples=bootstrap_samples)
    write_json(output / "summary.json", report)
    write_csv(output / "presence.csv", ({**{k:g[k] for k in ("source_id","run_id","target_model","scenario","method","condition","channel")},
        "variant":v, **{k:m[k] for k in ("planned_texts","evaluable_texts","unknown_texts","present_texts","PRESENT_rate","complete_pages","excluded_pages","PRESENT_at4","PRESENT_all4")}}
        for g in report["groups"] for v,m in g["name_gated_presence"].items()))
    write_csv(output / "exact_fields.csv", ({**{k:g[k] for k in ("source_id","run_id","target_model","scenario","method","condition","channel")}, **g["exact_name_gated"]} for g in report["groups"]))
    write_csv(output / "repeat_curves.csv", repeat_curves(records))
    write_csv(output / "exact_repeat_curves.csv", exact_repeat_curves(records))
    write_csv(output / "file_matrices.csv", ({**{k:v for k,v in row.items() if k not in ("cells","ci95")}, **row["cells"]} for row in report["file_matrices"]))
    write_json(output / "paired_noguard.json", report["paired_noguard"])
    if subjects is not None:
        export_raw(records, subjects, output / "raw_results.jsonl")
    write_json(output / "export_metadata.json", {"schema":"piibench-table-export-v1", "model_requests":0,
        "human_validation":report["human_validation"], "texts":len(records),
        "coverage":"Only supplied labels; this export does not certify complete paper evidence.",
        "raw_text_included":subjects is not None})
    return {"output":str(output), "texts":len(records), "model_requests":0}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--labels", help="labels.jsonl from presence_judge report")
    source.add_argument("--raw-results", help="Text-bearing raw_results.jsonl from this exporter")
    p.add_argument("--subjects", help="Optional prepared subjects.jsonl for text-bearing raw export")
    p.add_argument("--output", required=True)
    p.add_argument("--bootstrap-samples", type=int, default=1000)
    a = p.parse_args(argv)
    if a.bootstrap_samples < 0:
        p.error("bootstrap-samples must be nonnegative")
    if a.raw_results:
        records = []
        for raw in jsonl(a.raw_results):
            if raw.get("schema") != "piibench-raw-result-v1":
                raise ValueError("Unsupported raw-result schema")
            records.extend({k:v for k,v in row.items() if k not in ("text","truth")} for row in raw["channels"].values())
    else:
        records = list(jsonl(a.labels))
    print(json.dumps(export(records, a.output,
        subjects=jsonl(a.subjects) if a.subjects else None, bootstrap_samples=a.bootstrap_samples), indent=2))


if __name__ == "__main__":
    main()
