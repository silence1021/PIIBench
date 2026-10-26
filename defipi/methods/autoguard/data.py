"""Strict data loading and split/provenance validation for reproduction cases."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

PATTERNS = ("CR", "AR", "RE", "TC", "LOGIC", "PROG", "TRANS", "SUPER", "SUDO", "SIMU")
SPLITS = ("discovery", "bandit", "heldout")
CONTACT_FIELDS = ("email", "phone", "address")


def read_rows(path):
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"expected a list of case records in {path}")
    return rows


def _normalized_question(row):
    question = " ".join(str(row.get("prompt_template") or row.get("question") or "").casefold().split())
    return question


def validate_cases(cases, *, require_paper_cardinality=False, require_heldout=False):
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a non-empty list")
    split_ids = defaultdict(set)
    split_questions = defaultdict(set)
    by_split_pattern = defaultdict(list)
    for row in cases:
        if not isinstance(row, dict):
            raise ValueError("every case must be a JSON object")
        if "seed" in row:
            raise ValueError("case.seed is not supported; browser.episode_seed sets the episode seed")
        case_id = str(row.get("id") or "").strip()
        split = str(row.get("split") or "").strip()
        pattern = str(row.get("pattern") or "").strip()
        if not case_id or split not in SPLITS:
            raise ValueError(f"case requires non-empty id and split in {SPLITS}: {case_id!r}, {split!r}")
        if pattern not in PATTERNS:
            raise ValueError(f"case {case_id!r} has unsupported pattern {pattern!r}")
        if not str(row.get("question") or "").strip():
            raise ValueError(f"case {case_id!r} has no complete frozen question")
        if not str(row.get("starting_url") or "").strip():
            raise ValueError(f"case {case_id!r} has no starting_url")
        if not isinstance(row.get("reporter_info"), dict):
            raise ValueError(f"case {case_id!r} has no reporter_info object")
        missing = [field for field in ("name", *CONTACT_FIELDS) if not str(row["reporter_info"].get(field) or "").strip()]
        if missing:
            raise ValueError(f"case {case_id!r} is missing contact-dj ground truth fields: {missing}")
        provenance = row.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("kind"):
            raise ValueError(f"case {case_id!r} requires explicit provenance.kind")
        if split in {"discovery", "bandit"} and provenance.get("kind") not in {"paraphrase", "pattern_expansion"}:
            raise ValueError(f"training case {case_id!r} must have explicit paraphrase provenance")
        if split in {"discovery", "bandit"} and provenance.get("kind") == "paraphrase":
            if not str(provenance.get("parent_question") or "").strip():
                raise ValueError(f"training paraphrase {case_id!r} requires provenance.parent_question")
            if " ".join(str(provenance["parent_question"]).casefold().split()) == " ".join(str(row["question"]).casefold().split()):
                raise ValueError(f"training case {case_id!r} is the unchanged original question, not a paraphrase")
        if split in {"discovery", "bandit"} and provenance.get("kind") == "pattern_expansion":
            template = row.get("prompt_template")
            if not isinstance(template, str) or "[URL]" not in template:
                raise ValueError(f"pattern expansion {case_id!r} must preserve its original prompt_template")
            if row["question"] != template.replace("[URL]", row["starting_url"]):
                raise ValueError(f"case {case_id!r} changed prompt wording beyond URL binding")
        if split == "heldout" and provenance.get("kind") not in {"original", "reserved_original"}:
            raise ValueError(f"heldout case {case_id!r} must come from an original question")
        if case_id in split_ids[split]:
            raise ValueError(f"duplicate case id in {split}: {case_id}")
        split_ids[split].add(case_id)
        question = _normalized_question(row)
        if question in split_questions[split]:
            raise ValueError(f"duplicate question/task/url in {split}: {case_id}")
        split_questions[split].add(question)
        by_split_pattern[(split, pattern)].append(row)
    for i, left in enumerate(SPLITS):
        for right in SPLITS[i + 1:]:
            ids_overlap = split_ids[left] & split_ids[right]
            questions_overlap = split_questions[left] & split_questions[right]
            if ids_overlap or questions_overlap:
                raise ValueError(f"case or prompt overlap between {left} and {right}")
    if require_paper_cardinality:
        expected_splits = {"discovery": 100, "bandit": 100}
        for split, expected in expected_splits.items():
            rows = [r for r in cases if r["split"] == split]
            if len(rows) != expected:
                raise ValueError(f"formal {split} requires {expected} cases, got {len(rows)}")
            counts = {p: len(by_split_pattern[(split, p)]) for p in PATTERNS}
            if counts != {p: 10 for p in PATTERNS}:
                raise ValueError(f"formal {split} requires 10 paraphrases per pattern: {counts}")
    if require_heldout and not any(row["split"] == "heldout" for row in cases):
        raise ValueError("evaluation requires a separately reserved heldout split")
    return {split: [row for row in cases if row["split"] == split] for split in SPLITS}


def import_seed_rows(seeds, mapping):
    """Keep original questions intact; require explicit per-seed execution metadata."""
    if not isinstance(mapping, dict):
        raise ValueError("mapping must be an object keyed by seed id")
    by_id = {str(seed.get("id") or ""): seed for seed in seeds}
    unknown = set(mapping) - set(by_id)
    if unknown:
        raise ValueError(f"mapping refers to unknown seed ids: {sorted(unknown)}")
    rows = []
    for seed_id, spec in mapping.items():
        if not isinstance(spec, dict):
            raise ValueError(f"mapping entry for {seed_id!r} must be an object")
        required = ("split", "pattern", "starting_url", "provenance")
        missing = [key for key in required if not spec.get(key)]
        if missing:
            raise ValueError(f"mapping entry for {seed_id!r} must explicitly supply {missing}")
        seed = by_id[seed_id]
        row = {
            "id": seed_id,
            "task_id": str(spec.get("task_id") or seed_id),
            "split": spec["split"],
            "pattern": spec["pattern"],
            "question": seed.get("question"),
            "reporter_info": seed.get("reporter_info"),
            "starting_url": spec["starting_url"],
            "provenance": spec["provenance"],
            "source_artifact": spec["provenance"].get("source_artifact"),
            "source_seed_id": seed_id,
            "ideal": seed.get("ideal"),
            "context": seed.get("context"),
            "page_id": seed.get("slug") or seed_id,
            "html_file": seed.get("html_file"),
        }
        if row["question"] is None:
            raise ValueError(f"seed {seed_id!r} has no saved original question")
        rows.append(row)
    # This is an explicit source import, not a claim that the imported originals
    # form the paper's paraphrase training set.
    for row in rows:
        if row["split"] in {"discovery", "bandit"} and row["provenance"].get("kind") != "paraphrase":
            raise ValueError("discovery/bandit imports must explicitly identify paraphrases and their parent question")
    validate_cases(rows)
    return rows
