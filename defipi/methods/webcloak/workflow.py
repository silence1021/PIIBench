#!/usr/bin/env python3
"""Validate, search, prepare, or evaluate the bounded WebCloak-PII pipeline."""
from __future__ import annotations

from defipi.benchmark.method_splits import load_method_split

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import argparse
import json
import sys
from pathlib import Path

from defipi.config import load_config_file

PROJECT_ROOT = _PACKAGE_ROOT
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from defipi.methods.webcloak.core import (
    FIELDS,
    GROUPS,
    extract_page,
    prepare_groups,
    SEMANTIC_LAYOUT_VERSION,
    STRUCTURE_VERSION,
)
from defipi.methods.webcloak.search import (
    run_search,
    search_evaluator_roles,
    SEARCH_VERSION,
)
from defipi.methods.webcloak.evaluation import (
    FILTER_VARIANTS,
    ATTACKER_CONDITIONS,
    FINAL_PROMPT_PROTOCOL_VERSION,
    build_attacker_prompt_specs,
    build_answer_request,
    run_record,
    summarize_records,
    _frozen_link_metadata,
)
from defipi.evaluation.weighted_recoverability import SCORING_VERSION
from defipi.evaluation.llm_filter import FILTER_PROTOCOL_VERSION


from defipi.configuration.schema import ValidationError
from defipi.methods.webcloak.configuration import validate_method_config


def _resolve(path, root=PROJECT_ROOT):
    p = Path(path).expanduser()
    return p if p.is_absolute() else root / p


def load_config(path):
    return load_config_file(path)


def validate_config(config, *, check_files=True, mode="validate"):
    errors, warnings = [], []
    try:
        validate_method_config(config, mode=mode)
    except ValidationError as exc:
        return {"data_valid": False, "execution_ready": False,
                "errors": [str(p) for p in exc.problems],
                "missing_execution_configuration": [], "warnings": []}
    page_profile = config["page_profile"]
    data = config["data"]
    html_root = _resolve(data["html_dir"])
    if not html_root.is_dir(): errors.append(f"missing HTML directory: {html_root}")
    seeds = split = None
    selected = []
    heldout, eval_ids = set(), set()
    paths = {"html_dir": str(html_root)}
    if mode != "evaluate":
        for key in ("seeds", "saved_split"):
            path = _resolve(data[key]); paths[key] = str(path)
            if not path.is_file():
                errors.append(f"missing {key} file: {path}")
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if key == "seeds": seeds = value
                else: split = value
            except Exception as exc:
                errors.append(f"invalid {key} JSON: {exc}")
        if seeds is not None and (type(seeds) is not list or any(type(r) is not dict for r in seeds)):
            errors.append("seeds must be a list of records"); seeds = None
        if split is not None and type(split) is not dict:
            errors.append("saved_split must be an object"); split = None
        ids = {r.get("id") for r in seeds or []}
        if split is not None:
            metadata = load_method_split(_resolve(data["saved_split"]), "webcloak")
            if type(metadata) is not dict or any(
                type(metadata.get(key)) is not list or any(type(value) is not str for value in metadata[key])
                for key in ("heldout_record_ids", "eval_record_ids")
            ):
                errors.append("saved_split requires canonical heldout IDs and WebCloak development IDs")
            else:
                heldout = set(metadata["heldout_record_ids"])
                eval_ids = set(metadata["eval_record_ids"])
        selected = data["prepare_ids"] if mode == "prepare" else data["developer_ids"]
        unknown = sorted(set(selected) - ids)
        if unknown: errors.append("unknown record IDs: " + ", ".join(unknown))
        if mode != "prepare" and set(selected) & heldout:
            errors.append("heldout IDs cannot be optimized: " + ", ".join(sorted(set(selected) & heldout)))
        eligible = eval_ids | heldout if mode == "prepare" else eval_ids
        if set(selected) - eligible:
            errors.append("requested IDs are outside saved split: " + ", ".join(sorted(set(selected) - eligible)))
        if check_files and html_root.is_dir() and seeds:
            by_id = {r["id"]: r for r in seeds}
            for record_id in selected:
                record = by_id.get(record_id)
                if record:
                    page = _resolve(record["html_file"], html_root)
                    if not page.is_file(): errors.append(f"missing page for {record_id}: {page}")
                    else:
                        try: extract_page(page.read_text(encoding="utf-8"), record, page_profile=page_profile)
                        except Exception as exc: errors.append(f"invalid page {record_id}: {type(exc).__name__}: {exc}")
        if page_profile == "academic" and seeds is not None and len(seeds) != 200:
            warnings.append(f"seed count is {len(seeds)}, expected 200")
    return {"data_valid": not errors, "execution_ready": not errors, "page_profile": page_profile,
        "errors": errors, "missing_execution_configuration": [], "warnings": warnings,
        "seed_count": len(seeds) if seeds is not None else None,
        "heldout_count": len(heldout), "saved_eval_count": len(eval_ids),
        "developer_ids": data.get("developer_ids", []), "paths": paths}


def _records(config):
    seeds = json.loads(_resolve(config["data"]["seeds"]).read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in seeds}
    return [by_id[i] for i in config["data"]["developer_ids"]]


def _requested_records(config):
    seeds = json.loads(_resolve(config["data"]["seeds"]).read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in seeds}
    ids = config["data"]["prepare_ids"]
    split = load_method_split(_resolve(config["data"]["saved_split"]), "webcloak")
    eligible = set(split["eval_record_ids"]) | set(split["heldout_record_ids"])
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ValueError("requested record IDs must be a non-empty unique list")
    unknown = sorted(set(ids) - set(by_id))
    if unknown: raise ValueError("unknown record IDs: " + ", ".join(unknown))
    outside = sorted(set(ids) - eligible)
    if outside: raise ValueError("requested IDs are outside saved eval/heldout split: " + ", ".join(outside))
    html_root = _resolve(config["data"]["html_dir"])
    for record in (by_id[i] for i in ids):
        page = _resolve(record["html_file"], html_root)
        if not page.is_file(): raise FileNotFoundError(f"missing requested page {record['id']}: {page}")
        extract_page(page.read_text(encoding="utf-8"), record, page_profile=config["page_profile"])
    return [by_id[i] for i in ids], {"heldout": set(split["heldout_record_ids"]), "eval": set(split["eval_record_ids"])}


def cmd_validate(config):
    report = validate_config(config)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["data_valid"] else 2


def cmd_optimize(config):
    report = validate_config(config, mode="optimize")
    if not report["data_valid"]:
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 2
    records = _records(config)
    output = _resolve(config["artifacts"]["search"])
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    def checkpoint(snapshot, page):
        snapshot["model_versions"] = snapshot["roles"]
        snapshot["rng_seed"] = config["search"]["rng_seed"]
        temporary = output_path.with_name(output_path.name + ".tmp")
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output_path)
        print(json.dumps({"record_id": page["record_id"], "status": page["status"],
            "channel": snapshot["search_channel"], "candidates": page["candidate_count"], "best_D": page["best_D"],
            "best_J": page["best_J"], "best_L": page["best_L"],
            "last_candidate": page["candidates"][-1]["candidate_id"] if page["candidates"] else None},
            ensure_ascii=False), flush=True)
    result = run_search(config, records, _resolve(config["data"]["html_dir"]),
                        progress_callback=checkpoint, candidate_callback=checkpoint)
    result["model_versions"] = result["roles"]
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    all_succeeded = bool(result["page_results"]) and all(p["status"] == "optimized" for p in result["page_results"].values())
    print(json.dumps({"output": str(output), "pages": len(result["page_results"]),
        "status": "optimized" if all_succeeded else "partial_failure"}, indent=2))
    return 0 if all_succeeded else 2


def cmd_prepare(config):
    report = validate_config(config, mode="prepare")
    if not report["data_valid"]: print(json.dumps(report, ensure_ascii=False, indent=2)); return 2
    search_output_path = _resolve(config["artifacts"]["search"])
    output = _resolve(config["artifacts"]["prepared"])
    search_doc = json.loads(Path(search_output_path).read_text(encoding="utf-8"))
    if search_doc.get("version") != SEARCH_VERSION or search_doc.get("search_unit") != "per-record-three-contact-fields-v1":
        raise ValueError("prepare requires a per-page WebCloak-PII search-v6 artifact")
    page_profile = config["page_profile"]
    if search_doc["page_profile"] != page_profile:
        raise ValueError("search artifact page_profile differs from current configuration")
    question = config["question"]
    base_question = search_doc["base_question"]
    if base_question != question:
        raise ValueError("search artifact question differs from current configuration")
    search_evaluator_roles(search_doc)
    records, split_sets = _requested_records(config)
    developer_ids = search_doc.get("developer_record_ids")
    page_results = search_doc.get("page_results")
    split_meta = load_method_split(_resolve(config["data"]["saved_split"]), "webcloak")
    heldout_ids, eval_ids = set(split_meta["heldout_record_ids"]), set(split_meta["eval_record_ids"])
    if not isinstance(developer_ids, list) or not developer_ids or set(developer_ids) & heldout_ids or not set(developer_ids) <= eval_ids:
        raise ValueError("search artifact developer IDs must be saved eval IDs and exclude heldout")
    if (not isinstance(page_results, dict) or not set(page_results) or
            not set(page_results) <= set(developer_ids)):
        raise ValueError("search artifact page results must be a non-empty subset of developer IDs")
    requested_ids = [r["id"] for r in records]
    if not set(requested_ids) <= set(page_results):
        raise ValueError("cannot prepare a page without a saved independent search result")
    semantics, chosen_candidate_ids, semantic_rng_seeds = {}, {}, {}
    for record in records:
        page = page_results[record["id"]]
        if page.get("status") != "optimized" or not page.get("best_candidate_id"):
            raise ValueError(f"page has no valid search winner: {record['id']}")
        selected = [candidate for candidate in page.get("candidates", [])
                    if candidate.get("candidate_id") == page["best_candidate_id"]]
        if len(selected) != 1 or selected[0].get("record_id") != record["id"] or not selected[0].get("valid"):
            raise ValueError(f"page winner does not identify one valid local candidate: {record['id']}")
        page_semantics = page.get("best_semantics")
        if not isinstance(page_semantics, dict) or set(page_semantics) != set(FIELDS):
            raise ValueError(f"page winner has invalid frozen semantics: {record['id']}")
        for field in FIELDS:
            slots = page_semantics[field]
            if not isinstance(slots, dict) or set(slots) != {"p_before_text", "alt_text", "p_after_text"} or any(not isinstance(v, str) for v in slots.values()):
                raise ValueError(f"{record['id']}/{field}: winner must contain exactly three string slots")
        if selected[0].get("semantics") != page_semantics:
            raise ValueError(f"page winner semantics differ from selected candidate: {record['id']}")
        layout_seed = selected[0].get("semantic_rng_seed")
        if (type(layout_seed) is not int or layout_seed < 0 or
                selected[0].get("semantic_layout_protocol") != SEMANTIC_LAYOUT_VERSION or
                page.get("best_semantic_rng_seed") != layout_seed):
            raise ValueError(f"page winner has no matching frozen semantic layout: {record['id']}")
        semantic_rng_seeds[record["id"]] = layout_seed
        semantics[record["id"]] = page_semantics
        chosen_candidate_ids[record["id"]] = page["best_candidate_id"]
    root = _resolve(config["data"]["html_dir"])
    pages, records_meta, structure_assets = {}, {}, {}
    for record in records:
        html_path = _resolve(record["html_file"], root)
        source_html = html_path.read_text(encoding="utf-8")
        assets = {}
        groups = prepare_groups(source_html, semantics[record["id"]], record,
                                rng_seed=config["search"]["rng_seed"],
                                semantic_rng_seed=semantic_rng_seeds[record["id"]],
                                assets=assets, page_profile=page_profile)
        pages[record["id"]] = {}
        record_dir = f"pages/{record['id'].replace(':','_')}"
        for arm in GROUPS:
            rel = f"{record_dir}/{arm.lower().replace(' ','_')}.html"
            target = Path(output).parent / rel; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(groups[arm], encoding="utf-8")
            pages[record["id"]][arm] = rel
        structure_assets[record["id"]] = {}
        for asset_url, content in assets.items():
            rel = f"{record_dir}/{asset_url}"
            target = Path(output).parent / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            structure_assets[record["id"]][asset_url] = {"path": rel}
        canonical_links, canonical_display = _frozen_link_metadata(source_html, record["reporter_info"],
                                                                  page_profile=page_profile)
        records_meta[record["id"]] = {"html_file": record["html_file"], "reporter_info": record["reporter_info"],
        "record_role": "heldout" if record["id"] in split_sets["heldout"] else "saved_eval",
            "canonical_links": canonical_links, "canonical_display": canonical_display}
    attacker_conditions = list(config["evaluation"]["attacker_conditions"])
    attacker_prompt_specs = build_attacker_prompt_specs(question)
    manifest = {"version": "webcloak-pii-prepared-v5",
        "page_profile": page_profile,
        "search_unit": "per-record-three-contact-fields-v1",
         "question": question, "developer_record_ids": developer_ids,
        "prepared_record_ids": [r["id"] for r in records],
        "search_artifact": str(Path(search_output_path).resolve()),
        "source_search_version": search_doc["version"],
        "source_search_channel": search_doc["search_channel"],
        "source_search_pipeline": search_doc.get("search_pipeline"),
        "search_filter_protocol_version": search_doc.get("search_filter_protocol_version"),
        "source_search_roles": search_doc.get("roles"),
        "search_scoring_protocol_version": search_doc["search_scoring_protocol_version"],
        "search_feedback_protocol_version": search_doc["search_feedback_protocol_version"],
        "search_objective": search_doc.get("search_objective"),
        "semantic_layout_protocol": SEMANTIC_LAYOUT_VERSION,
        "semantic_rng_seeds": semantic_rng_seeds,
        "structure_protocol": STRUCTURE_VERSION,
        "structure_assets": structure_assets,
        "search_extractor_protocol_version": search_doc["search_extractor_protocol_version"],
        "chosen_candidate_ids": chosen_candidate_ids,
        "attacker_conditions": attacker_conditions,
        "attacker_prompt_protocol_version": FINAL_PROMPT_PROTOCOL_VERSION,
        "attacker_prompt_specs": attacker_prompt_specs,
        "record_semantics": semantics,
        "rng_seed": config["search"]["rng_seed"],
        "records": records_meta, "pages": pages,
        "roles": config.get("roles"), "channels": config["evaluation"]["channels"],
        "config": config, "frozen": True}
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "records": len(records), "frozen": True,
        "candidate_ids": chosen_candidate_ids}, indent=2))
    return 0


def cmd_evaluate(config):
    report = validate_config(config, mode="evaluate")
    if not report["data_valid"]:
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 2
    prepared_path = _resolve(config["artifacts"]["prepared"])
    output = _resolve(config["artifacts"]["evaluation"])
    manifest = json.loads(Path(prepared_path).read_text(encoding="utf-8"))
    if (manifest.get("version") != "webcloak-pii-prepared-v5" or
            manifest.get("search_unit") != "per-record-three-contact-fields-v1" or
            manifest.get("frozen") is not True):
        raise ValueError("evaluate requires a frozen prepared-v5 artifact")
    page_profile = config["page_profile"]
    if manifest["page_profile"] != page_profile:
        raise ValueError("prepared artifact page_profile differs from current configuration")
    if set(manifest.get("records", {})) != set(manifest.get("pages", {})):
        raise ValueError("frozen record/page ID sets differ")
    if set(manifest.get("record_semantics", {})) != set(manifest.get("pages", {})):
        raise ValueError("frozen semantic/page ID sets differ")
    channels = manifest.get("channels")
    if not isinstance(channels, list) or not channels or any(ch not in FILTER_VARIANTS for ch in channels):
        raise ValueError("frozen evaluation channels must be a non-empty list of fixed channels")
    if channels != config["evaluation"]["channels"]:
        raise ValueError("configured channels differ from frozen manifest")
    if manifest["question"] != config["question"]:
        raise ValueError("configured question differs from frozen manifest")
    attacker_conditions = manifest.get("attacker_conditions")
    configured_conditions = config["evaluation"]["attacker_conditions"]
    if not isinstance(attacker_conditions, list) or not attacker_conditions or len(set(attacker_conditions)) != len(attacker_conditions):
        raise ValueError("frozen attacker conditions must be a non-empty unique list")
    if any(c not in ATTACKER_CONDITIONS for c in attacker_conditions):
        raise ValueError("frozen artifact contains unsupported attacker conditions")
    if configured_conditions != attacker_conditions:
        raise ValueError("configured attacker conditions differ from frozen manifest")
    root = _resolve(config["data"]["html_dir"])
    prepared_root = Path(prepared_path).resolve().parent
    loaded_assets = {}
    if (manifest.get("structure_protocol") != STRUCTURE_VERSION or
            set(manifest.get("structure_assets", {})) != set(manifest["records"])):
        raise ValueError(f"{manifest['version']} requires its frozen structure protocol and assets")
    # Validate all generated resources before any model request can be made.
    for rid, entries in manifest["structure_assets"].items():
        if not entries:
            raise ValueError(f"frozen decoder assets missing: {rid}")
        loaded_assets[rid] = {}
        for asset_url, meta in entries.items():
            path = prepared_root / meta["path"]
            if not path.is_file():
                raise ValueError(f"frozen decoder asset missing: {rid}/{asset_url}")
            content = path.read_text(encoding="utf-8")
            loaded_assets[rid][asset_url] = content
    results = []
    source_missing = set()
    for rid, meta in manifest["records"].items():
        source_path = _resolve(meta["html_file"], root)
        if not source_path.is_file():
            source_missing.add(rid)
            continue
    for rid, paths in manifest["pages"].items():
        record_meta = manifest["records"][rid]
        record = {"id": rid, "html_file": record_meta["html_file"], "reporter_info": record_meta["reporter_info"],
            "canonical_links": record_meta.get("canonical_links", {}), "canonical_display": record_meta.get("canonical_display", {})}
        missing_arms = set(GROUPS) - set(paths)
        arms = {}
        for arm, rel in paths.items():
            if not rel: continue
            page_path = prepared_root / rel
            if not page_path.is_file(): continue
            html = page_path.read_text(encoding="utf-8")
            arms[arm] = html
        for arm in missing_arms:
            paths[arm] = None
        planned_arms = dict(arms)
        if rid in source_missing:
            for arm in GROUPS:
                for channel in channels:
                    for condition in attacker_conditions:
                        request = build_answer_request(config["roles"]["evaluator"], manifest["question"], "", condition=condition, protocol="final")
                        request["submitted"] = False
                        results.append({"record_id": rid, "record_role": record_meta.get("record_role"), "arm": arm,
                            "channel": channel, "condition": condition, "protocol_version": request["protocol_version"],
                            "condition_metadata": request["condition_metadata"], "request": request,
                            "status": "invalid", "answer": None, "score": None, "finish_reason": None, "usage": None,
                            "content_intact": False, "content_loss": False, "semantic_exposure": None,
                            "diagnostics": {}, "error": "FileNotFoundError: source page required for local resources"})
            continue
        if arms:
            results.extend(run_record(record, record["html_file"], root, planned_arms, question=manifest["question"],
                channels=channels, filter_config=config["roles"]["filter"] if any(c != "raw" for c in channels) else None,
                filter_stage="native_ax",
                extractor_configs=[config["roles"]["evaluator"]], semantics=manifest["record_semantics"][rid],
                attacker_conditions=attacker_conditions, extractor_protocol="final", assets=loaded_assets.get(rid), page_profile=page_profile))
        # A missing arm remains in the planned denominator as invalid, with null score.
        for arm in GROUPS:
            if arm in arms: continue
            for channel in channels:
                for condition in attacker_conditions:
                    request = build_answer_request(config["roles"]["evaluator"], manifest["question"], "", condition=condition, protocol="final")
                    request["submitted"] = False
                    results.append({"record_id": rid, "record_role": record_meta.get("record_role"), "arm": arm,
                        "channel": channel, "condition": condition, "protocol_version": request["protocol_version"],
                        "condition_metadata": request["condition_metadata"], "request": request,
                        "status": "invalid", "answer": None, "score": None, "finish_reason": None, "usage": None,
                        "content_intact": False, "content_loss": False, "semantic_exposure": None,
                        "diagnostics": {}, "error": "FileNotFoundError: frozen prepared page missing"})
        for result in results:
            if result["record_id"] == rid:
                result["record_role"] = record_meta.get("record_role")
    result = {"version": "webcloak-pii-evaluation-v3", "scoring_version": SCORING_VERSION,
        "page_profile": page_profile,
        "prepared_artifact": str(Path(prepared_path).resolve()),
        "structure_protocol": manifest["structure_protocol"],
        "semantic_layout_protocol": manifest["semantic_layout_protocol"],
        "question": manifest["question"], "roles": config["roles"], "channels": channels,
        "attacker_conditions": attacker_conditions,
        "attacker_prompt_protocol_version": FINAL_PROMPT_PROTOCOL_VERSION,
        "filter_protocol_version": FILTER_PROTOCOL_VERSION,
        "filter_stage": "native_ax",
        "filter_task_binding": "The fixed manifest question is forwarded unchanged as user_task on each non-raw filter channel.",
        "renderer_version": "chromium-native-cdp-full-axtree-v1",
        "chromium_versions": sorted({r.get("diagnostics", {}).get("chromium_version") for r in results
            if r.get("diagnostics", {}).get("chromium_version")}),
        "records": results, "summary": summarize_records(results)}
    Path(output).parent.mkdir(parents=True, exist_ok=True); Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "planned": len(results), "summary": result["summary"]}, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__ + " Prepare freezes the selected per-page strings offline.")
    parser.add_argument("--config", help="YAML configuration (required for search, transfer and evaluation)")
    sub = parser.add_subparsers(dest="command", required=True)
    transfer_parser = sub.add_parser("transfer", help="Imitate one explicit search candidate on benchmark pages and render pre-JS arms")
    transfer_parser.add_argument("--retry-failed", action="store_true")
    sub.add_parser("validate", help="Offline data and execution configuration audit; creates no clients")
    sub.add_parser("optimize", help="Search fixed themes on explicitly configured developer IDs")
    sub.add_parser("prepare", help="Freeze selected per-page semantics and four HTML arms offline")
    sub.add_parser("evaluate", help="Evaluate only a frozen prepared artifact")
    from .materialize import add_arguments, run_cli
    add_arguments(sub.add_parser("materialize", help="Bind already generated final pages offline"))
    args = parser.parse_args(argv)
    if args.command == "materialize":
        return run_cli(args)
    if args.config is None:
        parser.error("--config is required for " + args.command)
    from defipi.config import load_local_env
    load_local_env()
    config = load_config(args.config)
    if args.command == "transfer":
        from .transfer import transfer
        result = transfer(config, retry_failed=args.retry_failed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2
    if args.command == "validate": return cmd_validate(config)
    if args.command == "optimize": return cmd_optimize(config)
    if args.command == "prepare": return cmd_prepare(config)
    return cmd_evaluate(config)


if __name__ == "__main__":
    raise SystemExit(main())
