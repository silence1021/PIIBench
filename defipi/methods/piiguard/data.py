"""Bind PIIGuard to the configured final pages and independent development requests."""


from __future__ import annotations

from defipi.benchmark.method_splits import load_method_split

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import argparse
from collections import Counter
import json
from pathlib import Path

from bs4 import BeautifulSoup

from defipi.academic_pages.layout import require_academic_sidebar_layout
from defipi.benchmark.splits import load_seed_records
from defipi.config import load_config_file
from defipi.configuration.schema import Problem, ValidationError
from defipi.methods.piiguard.configuration import validate_data_config
from defipi.evaluation.field_matching import field_in_text
from defipi.pages.reporter_info import resolve_clean_reporter_info
from defipi.methods.common.render import render_seed_html


ROOT = _PACKAGE_ROOT
FIELDS = ("name", "email", "phone", "address")


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _index(rows, key):
    indexed = {row[key]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"Duplicate {key} in source data")
    return indexed


def _validate_html(html_text, truth, scenario, label):
    if scenario == "academic":
        require_academic_sidebar_layout(html_text)
    text = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True)
    missing = [field for field in FIELDS if not field_in_text(field, text, truth.get(field))]
    if missing:
        raise ValueError(f"Missing complete contact fields in {label}: {missing}")


def prepare_piiguard_data(config_path, *, root=ROOT):
    root = Path(root).resolve()
    config_path = root / config_path
    config = validate_data_config(load_config_file(config_path))
    from defipi.benchmark.files import load_tasks
    benchmark, sources, splits, problems = {}, {}, {}, []
    try:
        training = _read(root / config["training_prompts"])
    except (OSError, ValueError) as exc:
        problems.append(Problem("training_prompts", str(exc)))
    for scenario, binding in config["scenarios"].items():
        for field, loader, target in (
            ("benchmark", lambda: load_tasks(root / config["benchmark"][scenario] / "tasks.json"), benchmark),
            ("source_seeds", lambda: _index(load_seed_records(root / binding["source_seeds"], start_index=0, limit=None, table_seed=binding["table_seed"]), "id"), sources),
            ("saved_split", lambda: _read(root / binding["saved_split"]), splits),
        ):
            try:
                target[scenario] = loader()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                problems.append(Problem(f"scenarios.{scenario}.{field}", str(exc)))
    if problems:
        raise ValidationError(problems)
    final_tasks = [task for saved in benchmark.values() for task in saved["tasks"]]
    training_ids = {row["id"] for row in training}
    final_ids = {row["metadata"]["question_id"] for row in final_tasks}
    final_questions = {row["question"] for row in final_tasks}
    if training_ids & final_ids or any(row["prompt"] in final_questions for row in training):
        raise ValueError("Development requests overlap final requests")

    output_dir = root / config["output_dir"]
    manifest = {
        "status": "data_prepared_execution_pending",
        "data_config": str(config_path.relative_to(root)),
        "benchmark": config["benchmark"],
        "training_prompt_source": config["training_prompts"],
        "per_candidate_page_count": None,
        "historical_use": "Reuse existing development and final cohorts with their recorded provenance; they are not new unseen data.",
        "scenarios": {},
        "model_calls": 0,
    }
    validation = {"model_calls": 0, "training_final_prompt_overlap": 0, "scenarios": {}}

    pending_json, pending_html = [], []
    for scenario, binding in config["scenarios"].items():
        source = sources[scenario]
        split = load_method_split(root / binding["saved_split"])
        dev_ids = split["eval_record_ids"]
        heldout_ids = split["heldout_record_ids"]
        finals = benchmark[scenario]["tasks"]
        final_source_ids = {row["metadata"]["source_record_id"] for row in finals}
        if len(dev_ids) != len(set(dev_ids)) or set(dev_ids) & final_source_ids:
            raise ValueError(f"Invalid development/final ID separation: {scenario}")
        if set(heldout_ids) != final_source_ids:
            raise ValueError(f"Saved held-out IDs differ from configured final pages: {scenario}")
        if set(dev_ids) - set(source):
            raise ValueError(f"Development source records are missing: {scenario}")
        if scenario == "news":
            normalized = lambda value: " ".join(value.split())
            dev_text = {normalized(source[rid]["context"]) for rid in dev_ids}
            final_text = {normalized(source[rid]["context"]) for rid in final_source_ids}
            if dev_text & final_text:
                raise ValueError("News development/final article overlap")

        prompts = [row for row in training if row["domain"] == scenario
                   and row["base_request_id"] == config["training_base_request_id"]]
        if not prompts:
            raise ValueError(f"No complete training requests for {scenario}")
        html_dir = root / binding["html_dir"] if scenario == "academic" else output_dir / scenario / "html"
        development = []
        for index, record_id in enumerate(dev_ids):
            record = dict(source[record_id])
            truth = resolve_clean_reporter_info(record)
            if record.get("attack_instruction") or record.get("defense_fragment"):
                raise ValueError(f"Development source already contains a defense: {record_id}")
            if record.get("rendered_reporter_info") != truth:
                raise ValueError(f"Development contact fields are perturbed: {record_id}")
            prompt = prompts[index % len(prompts)]
            record.pop("template_style", None)
            record.update(
                pii_perturbation_mode="none",
                question=prompt["prompt"],
                prompt_id=prompt["id"],
                prompt_scenario_id=prompt["scenario_id"],
                base_request_id=prompt["base_request_id"],
                version=prompt["version"],
                split="existing_development_pool",
                source_record_id=record_id,
                source_seeds=binding["source_seeds"],
                source_split=binding["saved_split"],
                prompt_source=config["training_prompts"],
            )
            if scenario == "academic":
                html_text = (html_dir / record["html_file"]).read_text(encoding="utf-8")
            else:
                record["html_file"] = record_id + ".html"
                html_text = render_seed_html(record)
            _validate_html(html_text, truth, scenario, record_id)
            if scenario == "news":
                pending_html.append((html_dir / record["html_file"], html_text))
            development.append(record)

        scenario_dir = output_dir / scenario
        seeds_path = scenario_dir / "development_seeds.json"
        pending_json.append((seeds_path, development))
        optimizer_data = {
            "seeds_json": str(seeds_path.relative_to(root)),
            "html_dir": str(html_dir.relative_to(root)),
        }
        data_path = scenario_dir / "optimizer_data.json"
        pending_json.append((data_path, optimizer_data))
        manifest["scenarios"][scenario] = {
            "development_count": len(development),
            "development_record_ids": dev_ids,
            "development_seeds": str(seeds_path.relative_to(root)),
            "optimizer_data": str(data_path.relative_to(root)),
            "html_dir": str(html_dir.relative_to(root)),
            "final_count": len(finals),
            "final_tasks": str(Path(config["benchmark"][scenario]) / "tasks.json"),
            "source_seeds": binding["source_seeds"],
            "saved_split": binding["saved_split"],
            "development_prompt_counts": dict(Counter(row["prompt_id"] for row in development)),
        }
        validation["scenarios"][scenario] = {
            "development_pages_checked": len(development),
            "final_pages_checked": len(finals),
            "development_final_id_overlap": 0,
            "complete_contact_fields_present": True,
            "final_questions_preserved": True,
        }
        if scenario == "news":
            validation["scenarios"][scenario]["development_final_article_overlap"] = 0
    for path, text in pending_html:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for path, value in pending_json:
        _write(path, value)
    _write(output_dir / "data_manifest.json", manifest)
    _write(output_dir / "validation.json", validation)
    return {"data_manifest": str((output_dir / "data_manifest.json").relative_to(root)),
            **validation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_piiguard_data(args.config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
