"""Materialize a synthetic NoGuard/GEPA example and real-model configurations."""
import argparse
import json
from pathlib import Path

from defipi.configuration.evaluation import make_config
from defipi.configuration.generation import load_settings
from defipi.configuration.schema import Field
from defipi.benchmark.prepare import prepare_records
from defipi.benchmark import load_pages
from defipi.artifacts import write_artifact
from importlib import import_module
from defipi.methods.gepa.recoverability_pareto import POSITIONS, candidate_payload
from defipi.methods.common.render import render_seed_html
from defipi.jsonio import write_json

ROOT = Path(__file__).resolve().parents[1]


def prepare_example(output_dir, config_file):
    settings = load_settings(config_file, {
        "seed_file": Field(str), "initial_attack_file": Field(str),
        "evaluation_preset": Field(str), "condition": Field(str, choices=("off", "on")),
        "concurrency": Field(int, minimum=1, maximum=20), "repeats": Field(int, minimum=1),
        "max_attempts": Field(int, choices=(1,)),
    }, path_fields=("seed_file", "initial_attack_file"))
    from defipi.configuration.evaluation import TEMPLATES
    if not (TEMPLATES / (settings["evaluation_preset"] + ".json")).is_file():
        raise ValueError("evaluation_preset must name an existing evaluation template")
    output = Path(output_dir).resolve()
    if output.exists():
        raise ValueError(f"Example directory already exists: {output}; use a new directory")
    seed = json.loads(Path(settings["seed_file"]).read_text(encoding="utf-8"))
    from defipi.generation.attacks import load_attack_specs
    initial = load_attack_specs(settings["initial_attack_file"])[-1]
    candidate = candidate_payload(initial, "contact_block", budget=None)
    write_json(output / "candidate.json", {"candidate_id": "quickstart-initial-unoptimized", **candidate})
    page = output / "clean-site" / "index.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_seed_html(seed), encoding="utf-8")
    records_path = output / "records.json"
    write_json(records_path, [{"scenario": "news", "page_id": seed["id"], "case_id": "synthetic-news-1-pii",
        "task_type": "pii", "html_file": "clean-site/index.html", "resource_root": "clean-site",
        "source_record": seed, "prepared_record_id": seed["id"], "source_record_id": seed["id"],
        "question": seed["question"],
        "reporter_info": seed["reporter_info"], "metadata": {"scenario": "news", "source_record_id": seed["id"],
        "question_id": "synthetic-contact-question", "split": "example", "synthetic": True, "used_for_candidate_selection": False}}])
    prepare_records(records_path, output / "benchmark", benchmark_id="quickstart-synthetic")
    pages = load_pages(output / "benchmark/news/pages.json")
    source = output / "gepa-output.json"
    write_json(source, {"seed_meta": {"fragment_profile": "news"}, "run_id": "quickstart-initial-unoptimized",
                       "run_status": "unoptimized", "result": {"best": {position: ({"candidate_id": "quickstart-initial-unoptimized", **candidate}
                           if position == candidate["position"] else {}) for position in POSITIONS},
                                  "pool": [{"candidate_id": "quickstart-initial-unoptimized", **candidate}]}})
    from argparse import Namespace
    for method in ("noguard", "gepa"):
        exported = import_module(f"defipi.methods.{method}.export").export(pages, Namespace(**({"source": str(source), "candidate_id": "quickstart-initial-unoptimized"} if method == "gepa" else {})))
        artifact = write_artifact(output / method, pages, exported, method_id=method, artifact_id=f"quickstart-{method}")
        preset = settings["evaluation_preset"]
        config = make_config(preset, output / "benchmark/news/tasks.json", artifact, output / "live" / method,
                             condition=settings["condition"], concurrency=settings["concurrency"])
        config.update(task_id=f"quickstart-live-{method}", repeats=settings["repeats"], max_attempts=settings["max_attempts"],
                      tasks="benchmark/news/tasks.json", artifact=f"{method}/artifact.json", output_dir=f"live/{method}")
        write_json(output / f"{method}-live.json", config)
    return {"example_dir": str(output), "methods": ["noguard", "gepa"],
            "synthetic": True, "candidate": "initial, unoptimized", "model_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_example(args.output_dir, args.config)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
