"""Read portable frozen run directories without a machine-local source registry."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path

from .file_task_evidence import fingerprint
from .file_task_metrics import deterministic_presence, matches
from .repeat_metrics import FIELDS

SOURCE_VERSION = "presence-source-v1"
ROOT = Path(__file__).resolve().parents[2]


def resolve(value, base=ROOT):
    p = Path(value)
    return p.resolve() if p.is_absolute() else (Path(base) / p).resolve()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def lines(path):
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def truth_of(task):
    truth = task.get("reporter_info") or task.get("truth") or {
        "name": task.get("target_identity_label"), **task.get("protected_fields", {})}
    if any(not isinstance(truth.get(f), str) or not truth[f].strip() for f in FIELDS):
        raise ValueError("Four complete ground-truth fields are required")
    return {f: truth[f] for f in FIELDS}


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") in ("text", "output_text")
                 and isinstance(b.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def answer_of(response):
    for key in ("final_answer", "answer", "output_text", "text"):
        if isinstance(response.get(key), str):
            return response[key]
    if response.get("choices"):
        return _text(response["choices"][0].get("message", {}).get("content"))
    if "content" in response:
        return _text(response["content"])
    if isinstance(response.get("output"), list):
        parts = [_text(item.get("content")) for item in response["output"] if item.get("type") == "message"]
        return "\n".join(x for x in parts if x is not None) if any(x is not None for x in parts) else None
    return None


def subject(source_id, identity, truth, text, model, *, channel="A", reason=None, provenance=None):
    if not model or not isinstance(model, str):
        raise ValueError("A declared or recorded target model is required")
    truth = truth_of({"truth": truth})
    if text is not None and not isinstance(text, str):
        raise ValueError("Evaluated content must be text or unknown")
    if identity.get("repeat_index") not in (0, 1, 2, 3):
        raise ValueError("Round index must be 0..3; retries are not rounds")
    content = {"version": SOURCE_VERSION, "channel": channel, "truth": truth, "text": text}
    # Content cache is independent of paths, archive verification timestamps, tools, and other channel.
    fp = fingerprint(content)
    item = {"source_id": source_id, "identity": identity, "channel": channel, "target_model": model,
            "truth": truth, "text": text, "unknown_reason": reason if text is None else None,
            "source_fingerprint": fp, "source_version": SOURCE_VERSION,
            "det_status": deterministic_presence(text, truth), "matched_fields": matches(text, truth),
            "provenance": provenance or {}}
    item["subject_id"] = fingerprint({"source_id": source_id, "identity": identity, "content": fp,
                                       "target_model": model})
    return item


def run_subjects(source):
    path = resolve(source["path"])
    # Explicit paths or direct children only: never pick up archived copies/recoveries by recursion.
    directories = [resolve(p, path) for p in source["runs"]] if source.get("runs") else (
        [path] if (path / "lock.json").exists() else sorted(p for p in path.iterdir() if p.is_dir()))
    for directory in directories:
        if not (directory / "results.json").exists():
            continue
        results = read(directory / "results.json")
        if results.get("run_status") not in ("completed", "completed_with_errors", "incomplete"):
            continue
        lock = read(directory / "lock.json")
        tasks = {t["case_id"]: t for t in lock.get("tasks", [])}
        if not tasks:
            raise ValueError(f"No frozen task truth in {directory / 'lock.json'}")
        runtime = lock.get("runtime", {})
        target = runtime.get("target", {}).get("model", "")
        if any(marker in target.lower() for marker in source.get("exclude_target_markers", [])):
            continue
        slots = results.get("slots", {})
        if not isinstance(slots, dict):
            raise ValueError("Expected keyed frozen result slots")
        selected = {}
        for original_key, slot in slots.items():
            key = (slot["method_id"], slot["case_id"], slot.get("channel"), slot["repeat_index"])
            if key in selected:
                raise ValueError("Duplicate frozen method/case/channel/round")
            selected[key] = (original_key, slot)
        # A terminal run can still lack an entire result. Keep its frozen denominator.
        for method in lock.get("methods", []):
            page_ids = {p["page_id"] for p in method.get("pages", [])}
            for case_id, task in tasks.items():
                if page_ids and task["page_id"] not in page_ids:
                    continue
                for channel in runtime.get("channels", []):
                    for repeat in range(runtime.get("repeat_start", 0),
                                        runtime.get("repeat_start", 0) + runtime.get("repeats", 0)):
                        key = (method["method_id"], case_id, channel, repeat)
                        selected.setdefault(key, (None, {
                            "method_id": method["method_id"], "case_id": case_id,
                            "page_id": task["page_id"], "channel": channel, "repeat_index": repeat,
                            "status": "missing_result"}))
        for key, (original_key, slot) in selected.items():
            task = tasks[slot["case_id"]]
            if slot["page_id"] != task["page_id"]:
                raise ValueError("Result page disagrees with its frozen case")
            model = slot.get("target_model") or runtime.get("target", {}).get("model") or slot.get("target")
            axes = runtime.get("dispatch", {}).get("axes", {})
            identity = {"batch_id": source["id"], "run_id": lock.get("run_id") or directory.name, "slot_key": fingerprint(key),
                "page_id": slot["page_id"], "case_id": slot["case_id"],
                "question_id": slot.get("question_id") or fingerprint(task.get("question", "")),
                "repeat_index": slot["repeat_index"], "method": slot.get("defense_method") or slot["method_id"],
                "scenario": slot.get("scenario") or lock.get("benchmark", {}).get("scenario"),
                "condition": slot.get("attack_method") or axes.get("attack_method") or slot.get("channel"),
                "mode": slot.get("mode") or axes.get("mode"),
                "surface": slot.get("surface") or runtime.get("observation_format"),
                "harness": slot.get("harness_type") or axes.get("harness_type"), "task": task.get("metadata", {}).get("task_kind", "answer"),
                "target_signature": fingerprint(runtime.get("target", {"model": model})),
                "harness_signature": fingerprint(runtime.get("dispatch", {}).get("adapter")),
                "selected_attempt": slot.get("selected_attempt", (slot.get("attempts") or [{}])[-1].get("attempt_index"))}
            valid = slot.get("status") == "valid" and slot.get("valid", True)
            provenance = {"lock": str(directory / "lock.json"), "results": str(directory / "results.json"),
                          "original_slot_key": original_key, "run_directory_name": directory.name,
                          "target_binding": runtime.get("target"),
                          "adapter_binding": runtime.get("dispatch", {}).get("adapter"),
                          "run_metadata": {"code_revision": lock.get("code_revision"),
                              "observation_format": runtime.get("observation_format"),
                              "dispatch": runtime.get("dispatch"), "target": runtime.get("target")}}
            yield subject(source["id"], identity, truth_of(task), slot.get("answer") if valid else None, model,
                          reason="missing_answer" if valid else slot.get("status", "missing"), provenance=provenance)
            if identity["task"] == "file":
                from .file_task_evidence import frozen_run_file
                text, detail = frozen_run_file(directory, slot, task) if valid else (None, {})
                yield subject(source["id"], identity, truth_of(task), text, model, channel="F",
                              reason=detail.get("unknown_reason", slot.get("status", "missing")),
                              provenance={**provenance, **detail})


def iter_subjects(spec):
    """Read only explicitly named runs or their direct children."""
    seen = set()
    for source in spec["sources"]:
        for item in run_subjects(source):
            item["comparison_source"] = source.get("comparison_source", source["id"])
            identity = item["identity"]
            key = (source["id"], identity["run_id"], identity["slot_key"], item["channel"])
            if key in seen:
                raise ValueError("Duplicate selected page/round/channel")
            seen.add(key)
            yield item
