"""Bind saved attack questions to Academic or News development pages offline."""
from __future__ import annotations

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

from collections import Counter
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

from defipi.config import load_config_file
from defipi.benchmark.method_splits import load_method_split
from defipi.evaluation.field_matching import field_in_text
from defipi.methods.autoguard.data import PATTERNS, read_rows, validate_cases

REPO_ROOT = _PACKAGE_ROOT


def _source_path(value):
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _prompt_origin(prompt, page_profile):
    """Read the generator and seed template recorded by each saved prompt kind."""
    kind = prompt["provenance_kind"]
    if kind == "historical_generated_prompt":
        return kind, prompt["generation_input"]["model"], f"{page_profile}__seed__{prompt['pattern_id']}", prompt["seed_prompt"]
    if kind == "seed_template_expansion":
        seed_prompt = "Target webpage: [URL]\n" + prompt["seed_template"].replace("{base_request}", prompt["base_request"])
        return kind, prompt["generator_model"], prompt["parent_id"], seed_prompt
    raise ValueError(f"prompt {prompt['prompt_id']} has unsupported provenance_kind: {kind!r}")


def bind_training_cases(*, prompt_dir, source_config, final_tasks, base_url, output_dir,
                        page_profile):
    if page_profile not in {"academic", "news"}:
        raise ValueError("page_profile must be academic or news")
    label = "Academic" if page_profile == "academic" else "News"
    if urlparse(base_url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError(f"{label} training uses a local page server")
    prompt_dir, source_config = Path(prompt_dir).resolve(), Path(source_config).resolve()
    output = Path(output_dir).resolve()
    config = load_config_file(source_config)
    seed_path = _source_path(config["seeds_json"])
    html_root = _source_path(config["html_dir"]) if page_profile == "academic" else output / "news_html"
    if config.get("fragment_profile") != page_profile:
        raise ValueError("Source configuration scenario differs from page profile")
    seeds = read_rows(seed_path)
    split = load_method_split(_source_path(config["split_manifest"]), "autoguard")
    by_id = {page["id"]: page for page in seeds}
    if len(by_id) != len(seeds):
        raise ValueError("Source seed record IDs must be unique")
    developer_ids = split["eval_record_ids"]
    missing = set(developer_ids) - set(by_id)
    if missing:
        raise ValueError(f"Missing source development records: {sorted(missing)}")
    pages = [by_id[record_id] for record_id in developer_ids]
    from defipi.benchmark.files import load_tasks
    tasks = load_tasks(final_tasks)
    if tasks["scenario"] != page_profile:
        raise ValueError("Final tasks scenario differs from page profile")
    final_ids = {row["metadata"]["source_record_id"] for row in tasks["tasks"]}
    if not final_ids:
        raise ValueError(f"final registry has no {label} pages to check")
    training_ids = {page["id"] for page in pages}
    if training_ids & final_ids:
        raise ValueError(f"training pages overlap the current {label} final set")
    if len(pages) != 25 or len(training_ids) != 25:
        raise ValueError(f"expected the existing 25-page {label} development pool")
    rendered_pages = {}
    if page_profile == "news":
        from defipi.methods.common.render import render_seed_html
        bound_pages = []
        for index, page in enumerate(pages, 1):
            if page.get("attack_instruction") or page.get("defense_fragment"):
                raise ValueError(f"page {page['id']} must be an undefended News source page")
            relative = f"news_pages/page-{index:03d}/index.html"
            rendered_pages[relative] = render_seed_html(page)
            bound_pages.append({**page, "html_file": relative})
        pages = bound_pages
    for page in pages:
        html = (rendered_pages[page["html_file"]] if page_profile == "news"
                else (html_root / page["html_file"]).read_text())
        if page_profile == "academic" and "academic-contact-single-sidebar-layout-v1" not in html:
            raise ValueError(f"page {page['id']} does not use the current Academic layout")
        truth = page.get("rendered_reporter_info") or page["reporter_info"]
        if not all(field_in_text(key, html, truth.get(key)) for key in ("email", "phone", "address")):
            raise ValueError(f"page {page['id']} does not contain all complete contact fields")
    rows = []
    for phase_index, phase in enumerate(("discovery", "bandit")):
        prompt_file = prompt_dir / f"{phase}_prompts.jsonl"
        prompts = read_rows(prompt_file)
        if len(prompts) != 100 or Counter(row["pattern_id"] for row in prompts) != Counter({p: 10 for p in PATTERNS}):
            raise ValueError(f"{phase} needs the saved 100 prompts, ten per pattern")
        for index, prompt in enumerate(prompts):
            if prompt.get("split") != phase or prompt.get("scenario") != page_profile:
                raise ValueError("prompt scenario/split does not match the saved training pool")
            page = pages[(index + phase_index * 12) % len(pages)]
            url = urljoin(base_url.rstrip("/") + "/", page["html_file"])
            template = prompt["prompt_text"]
            if "[URL]" not in template:
                raise ValueError(f"prompt {prompt['prompt_id']} has no URL placeholder")
            source_kind, generator, template_id, seed_prompt = _prompt_origin(prompt, page_profile)
            provenance = {
                "kind": "pattern_expansion", "source_kind": source_kind,
                "method": f"saved ten-pattern expansion; offline binding of existing {label} pages",
                "source_artifact": str(prompt_file), "source_line": index + 1,
                "source_prompt_id": prompt["prompt_id"], "source_pool": prompt["source_pool"],
                "generator_model": generator, "template_id": template_id,
                "template_prompt": seed_prompt,
                "original_source_artifact": prompt.get("source_artifact"),
                "original_source_prompt_id": prompt.get("source_prompt_id"),
                "original_source_line": prompt.get("source_line"),
                "generation_input_file": prompt.get("generation_input_file"),
                "raw_response_file": prompt.get("raw_response_file"), "output_index": prompt.get("output_index"),
                "page_source_config": str(source_config), "page_source_seeds": str(seed_path),
                "page_source_record_id": page["id"], "page_source_html": str(html_root / page["html_file"]),
                "binding_rule": "saved prompt order; page[(index + split_index*12) % 25]",
                "heldout_original_claim": False,
            }
            rows.append({"id": prompt["prompt_id"], "task_id": prompt["prompt_id"],
                         "split": phase, "pattern": prompt["pattern_id"], "page_id": page["id"],
                         "question": template.replace("[URL]", url), "prompt_template": template,
                         "starting_url": url, "html_file": page["html_file"],
                         "reporter_info": page.get("rendered_reporter_info") or page["reporter_info"],
                         "provenance": provenance})
    validate_cases(rows, require_paper_cardinality=True)
    cases_file = output / "training_cases.jsonl"
    manifest = {"source_config": str(source_config), "source_seeds": str(seed_path),
                "html_root": str(html_root), "base_url": base_url, "source_prompt_dir": str(prompt_dir),
                "page_ids": [page["id"] for page in pages], "unique_pages": len(pages),
                "final_tasks_registry": str(Path(final_tasks).resolve()), "final_overlap": [],
                "new_pages_created": 0, "model_calls": 0,
                "cases_file": str(cases_file), "cases": len(rows),
                "patterns_per_split": {phase: dict(Counter(r["pattern"] for r in rows if r["split"] == phase))
                                       for phase in ("discovery", "bandit")},
                "page_uses_per_split": {phase: dict(Counter(r["page_id"] for r in rows if r["split"] == phase))
                                       for phase in ("discovery", "bandit")},
                "source_counts_per_split": {phase: dict(Counter(r["provenance"]["source_pool"] for r in rows if r["split"] == phase))
                                           for phase in ("discovery", "bandit")},
                "questions_preserved_except_url": True, "heldout_required_for_training": False}
    if page_profile == "news":
        manifest.update(page_profile="news", rendered_existing_pages=len(rendered_pages),
                        renderer="defipi.methods.common.render.render_seed_html")
    encoded = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    if cases_file.exists() and cases_file.read_text() != encoded:
        raise FileExistsError("existing training bindings differ; use a new output directory")
    for relative, html in rendered_pages.items():
        path = html_root / relative
        if path.exists() and path.read_text() != html:
            raise FileExistsError("existing News HTML differs; use a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    for relative, html in rendered_pages.items():
        path = html_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html)
    cases_file.write_text(encoded)
    (output / "binding_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest
