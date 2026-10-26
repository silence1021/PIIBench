"""Unified presence preparation and offline consensus; no model calls here."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .file_task_evidence import fingerprint
from .file_task_judges import VERSION, consensus, jsonl, render_user, select_panel, system_prompt, write_json
from .presence_runner import REQUEST_SCHEMA, read_receipt, validate_job


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def make_jobs(subject, registry):
    """Content-addressed requests; subject identity is stored separately."""
    source = "final reply" if subject["channel"] == "A" else "saved file ./contacts.md"
    payload = {"reference": subject["truth"], "text": subject["text"], "source": source}
    messages = [{"role": "user", "content": render_user("JP", payload)}]
    jobs = []
    for binding in select_panel(subject["target_model"], registry):
        job = {"job_schema": REQUEST_SCHEMA, "protocol": VERSION, "role": "JP", "binding": binding,
               "system": system_prompt("JP"), "messages": messages, "ready": True}
        job["job_id"] = fingerprint(job)
        jobs.append(job)
    return jobs


def _prepare_contents(subjects, directory, registry, *, sources=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    ids, seen_subjects, counts = set(), set(), Counter()
    with (directory / "subjects.jsonl").open("w", encoding="utf-8") as subjects_file, \
            (directory / "jobs.jsonl").open("w", encoding="utf-8") as jobs_file:
        for subject in subjects:
            if subject["subject_id"] in seen_subjects:
                raise ValueError("Duplicate text subject")
            seen_subjects.add(subject["subject_id"])
            # Resolve the panel even for rule-only texts, so unknown target families fail early.
            select_panel(subject["target_model"], registry)
            item = {**subject, "job_ids": []}
            counts[subject["det_status"] or "unknown"] += 1
            if subject["det_status"] == "judge":
                for job in make_jobs(subject, registry):
                    item["job_ids"].append(job["job_id"])
                    if job["job_id"] not in ids:
                        jobs_file.write(json.dumps(job, ensure_ascii=False) + "\n")
                        ids.add(job["job_id"])
            subjects_file.write(json.dumps(item, ensure_ascii=False) + "\n")
    manifest = {"schema": "presence-store-v1", "protocol": VERSION, "sources": sources,
                "registry": registry, "subjects": len(seen_subjects), "unique_jobs": len(ids),
                "rule_states": dict(counts), "human_validation": "pending", "model_requests": 0,
                "files": {name: digest(directory / name) for name in ("subjects.jsonl", "jobs.jsonl")}}
    write_json(directory / "manifest.json", manifest)
    return {k: v for k, v in manifest.items() if k not in ("registry", "sources", "files")}


def prepare(subjects, directory, registry, *, sources=None):
    """Build beside the destination, publishing only a complete new store."""
    directory = Path(directory)
    if directory.exists() or directory.is_symlink():
        raise FileExistsError(f"Presence destination already exists: {directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".presence-prepare-", dir=directory.parent) as tmp:
        staged = Path(tmp) / "store"
        result = _prepare_contents(subjects, staged, registry, sources=sources)
        def identity(path):
            info = path.stat(follow_symlinks=False)
            return info.st_dev, info.st_ino

        stage_identity = identity(staged)
        # Reserve our own destination; a failed publish must not remove files
        # another writer added to it or a replacement directory at this path.
        directory.mkdir()
        reserved_identity = identity(directory)
        try:
            staged.replace(directory)
        except BaseException:
            try:
                current = identity(directory)
                if current == stage_identity:
                    shutil.rmtree(directory)  # Our stage was published before interruption.
                elif current == reserved_identity:
                    directory.rmdir()  # Remove only our reservation, and only while empty.
            except OSError:
                pass  # Missing/nonempty/replaced destinations are preserved.
            raise

    return result


def validate_store(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != "presence-store-v1" or manifest.get("protocol") != VERSION:
        raise ValueError("Unsupported presence store; prepare a new directory")
    for name, sha in manifest["files"].items():
        if name not in ("subjects.jsonl", "jobs.jsonl") or digest(directory / name) != sha:
            raise ValueError("Prepared presence store was modified")
    return manifest


def extend(subjects, directory, registry, *, sources=None):
    """Refresh source snapshots in one store; never reset receipts or resend successful jobs.

    Completed texts are immutable. Pending evidence can become available. Prior
    subjects/jobs/manifests are kept in history; active jobs refer only to this snapshot.
    """
    directory = Path(directory)
    def key(s):
        i = s["identity"]
        return (s["source_id"], i["run_id"], i["slot_key"], s["channel"])
    with (directory / ".runner.lock").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Cannot update a presence store while its runner is active") from exc
        try:
            validate_store(directory)
            current = {key(s): s for s in jsonl(directory / "subjects.jsonl")}
            for s in subjects:
                previous = current.get(key(s))
                if previous and previous["text"] is not None:
                    if previous["source_fingerprint"] != s["source_fingerprint"]:
                        raise ValueError("Completed source text/truth changed; preserve this store and investigate")
                current[key(s)] = s
            with tempfile.TemporaryDirectory(prefix=".presence-refresh-", dir=directory.parent) as tmp:
                fresh = Path(tmp) / "store"
                result = prepare(current.values(), fresh, registry, sources=sources)
                history = directory / "history" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                history.mkdir(parents=True)
                for name in ("subjects.jsonl", "jobs.jsonl", "manifest.json"):
                    shutil.copy2(directory / name, history / name)
                    (fresh / name).replace(directory / name)
                return {**result, "history": str(history), "receipts_preserved": True}
        finally:
            fcntl.flock(owner, fcntl.LOCK_UN)


def decisions(directory):
    """Rule results, majority, unanimity/any-vote sensitivity and the common Luna vote."""
    directory = Path(directory)
    manifest = validate_store(directory)
    jobs, votes = {}, {}
    for job in jsonl(directory / "jobs.jsonl"):
        validate_job(job)
        if job["job_id"] in jobs:
            raise ValueError("Duplicate request in presence store")
        jobs[job["job_id"]] = job
        votes[job["job_id"]] = read_receipt(directory, job)
    for s in jsonl(directory / "subjects.jsonl"):
        det = s["det_status"]
        label = "PRESENT" if det == "positive" else "ABSENT" if det == "empty" else None
        variants = {k: label for k in ("majority", "all_judges", "any_judge", "luna")}
        state = "rule" if label else s.get("unknown_reason") or "missing_evidence"
        panel = []
        if det == "judge":
            expected = [j["job_id"] for j in make_jobs(s, manifest["registry"])]
            if s["job_ids"] != expected:
                raise ValueError("Subject/request binding mismatch")
            panel = [{"model": jobs[j]["binding"]["model"], "alias": jobs[j]["binding"]["alias"],
                      "provider": jobs[j]["binding"]["provider"],
                      "job_id": j, "status": votes[j]["status"], "value": votes[j].get("value"),
                      "retryable": votes[j].get("retryable", False),
                      "attempts": len(votes[j]["attempts"])} for j in s["job_ids"]]
            result = consensus("JP", panel)
            label = result["value"]
            pending = any(v["status"] in ("missing", "started") or
                          v["status"] == "error" and v["retryable"] and v["attempts"] < 3 for v in panel)
            state = "consensus" if label else "pending" if pending else "no_consensus"
            ok = [p["value"] for p in panel if p["status"] == "ok"]
            variants.update(majority=label,
                all_judges="ABSENT" if "ABSENT" in ok else "PRESENT" if ok == ["PRESENT"] * 3 else None,
                any_judge="PRESENT" if "PRESENT" in ok else "ABSENT" if ok == ["ABSENT"] * 3 else None,
                luna=next((p["value"] for p in panel if p["alias"] == "luna" and p["status"] == "ok" and p["value"] in ("PRESENT", "ABSENT")), None))
        elif s["job_ids"]:
            raise ValueError("Rule-only subject unexpectedly has judge requests")
        # Reports never duplicate the original PII text. It stays in subjects and frozen evidence.
        yield {k: v for k, v in s.items() if k not in ("text", "truth")} | {
            "label": label, "state": state, "variants": variants, "votes": panel,
            "unknown_reason": None if label else s.get("unknown_reason") or state}
