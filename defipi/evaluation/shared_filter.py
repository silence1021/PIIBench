"""Explicit reuse of valid Filter outputs across API and Agent locks."""
from __future__ import annotations

import copy
from pathlib import Path
from . import unified_final_eval as engine


def bind_results(path):
    # A list explicitly selects saved outputs from partial and repair batches.
    partial_sources = isinstance(path, list)
    paths = path if partial_sources else [path]
    statuses = {"completed", "incomplete", "interrupted"} if partial_sources else {"completed"}
    cache, request_refs, source_by_key = {}, {}, {}
    for source in paths:
        source = str(Path(source).resolve())
        result = engine._read_json(source, "shared Filter results")
        if result.get("schema_version") != engine.RESULT_SCHEMA_VERSION or result.get("run_status") not in statuses:
            raise engine.FinalEvalError("shared_filter_results requires completed results, or an explicit list of completed/incomplete/interrupted results")
        if result.get("filter_cache_version") != engine.FILTER_CACHE_VERSION:
            raise engine.FinalEvalError("Cannot share legacy Filter results without repeat-index identity; "
                                        "produce independent Filter results in a new output directory")
        records = result.get("filter_attempts") or {}
        for key, entry in (result.get("filter_cache") or {}).items():
            if not entry.get("valid") or not entry.get("output_ref"):
                if partial_sources:
                    continue
                raise engine.FinalEvalError("Shared Filter cache contains an invalid output")
            request_ref = records.get(key, {}).get("request_ref")
            for reference in (entry["output_ref"], entry.get("raw_response_ref"), request_ref):
                if reference:
                    engine._FinalRunner._read_evidence(reference)
            if key in cache:
                if engine._FinalRunner._read_evidence(cache[key]["output_ref"]) != engine._FinalRunner._read_evidence(entry["output_ref"]):
                    raise engine.FinalEvalError("Conflicting shared Filter outputs for the same input; select one source")
                continue
            cache[key] = copy.deepcopy(entry)
            request_refs[key] = request_ref
            source_by_key[key] = source
    if not cache:
        raise engine.FinalEvalError("Shared result contains no valid Filter outputs")
    return {"filter_cache_version": engine.FILTER_CACHE_VERSION,
            "results_path": None if partial_sources else str(Path(path).resolve()),
            "results_paths": [str(Path(source).resolve()) for source in paths],
            "source_by_key": source_by_key,
            "cache": cache, "request_refs": request_refs}


def validate_coverage(lock):
    binding = lock["runtime"]["dispatch"].get("shared_filter")
    if not binding:
        return
    if binding.get("filter_cache_version") != engine.FILTER_CACHE_VERSION:
        raise engine.FinalEvalError("Shared Filter binding lacks repeat-index identity; prepare a new lock")
    method = lock["methods"][0]
    pages = {p["page_id"]: p for p in method["pages"]}
    keys = set()
    for task in lock["tasks"]:
        page = pages[task["page_id"]]
        text = Path(page["html_path"]).read_text(encoding="utf-8")
        for repeat in range(lock["runtime"]["repeats"]):
            key = engine._filter_id(lock, method, task, page, text, repeat)
            if key not in binding["cache"]:
                raise engine.FinalEvalError(f"No identical shared Filter output for {task['case_id']} "
                                            f"repeat_index={repeat}; check page, question and Filter settings")
            keys.add(key)
    binding["selected_keys"] = sorted(keys)


def reuse(runner, method, task, page, filter_input, repeat):
    binding = runner.dispatch["shared_filter"]
    key = engine._filter_id(runner.lock, method, task, page, filter_input, repeat)
    if key not in binding["cache"]:
        raise engine.FinalEvalError(f"Shared Filter input changed for {task['case_id']}")
    cached = copy.deepcopy(binding["cache"][key])
    source = binding.get("source_by_key", {}).get(key, binding.get("results_path"))
    cached.update(source_usage=cached.get("usage") or {}, usage={}, source_attempts=cached.get("attempts"),
                  attempts=0, reused_from=source)
    with runner.guard:
        runner.state["filter_cache"][key] = copy.deepcopy(cached)
        runner.state["filter_attempts"][key] = {
            "status": "valid", "filter_id": key, "repeat_index": repeat, "attempts": 0, "usage": {},
            "reused_from": source, "output_ref": cached["output_ref"],
            "raw_response_ref": cached.get("raw_response_ref"),
            "request_ref": binding["request_refs"].get(key),
        }
    runner._save()
    cached["output"] = runner._read_evidence(cached.pop("output_ref"))
    if cached.get("raw_response_ref"):
        cached["raw_response"] = runner._read_evidence(cached.pop("raw_response_ref"))
    return cached, True
