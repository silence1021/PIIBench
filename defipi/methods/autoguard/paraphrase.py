"""Explicit, resumable generation of the paper's two disjoint paraphrase splits."""
from __future__ import annotations

import json
import re
from pathlib import Path

from defipi.methods.autoguard.data import PATTERNS
from defipi.methods.autoguard.runtime import call_model


def _norm(text):
    return " ".join(str(text or "").casefold().split())


def _response_text(response):
    return response if isinstance(response, str) else str((response or {}).get("text") or "")


def _parse_pair(raw):
    text = str(raw or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)```", text, re.I | re.S)
    if fence:
        text = fence.group(1).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("paraphrase_response_not_object")
    left, right = value.get("discovery"), value.get("bandit")
    if not isinstance(left, str) or not left.strip() or not isinstance(right, str) or not right.strip():
        raise ValueError("paraphrase_response_missing_question")
    return left.strip(), right.strip()


def prepare_paraphrases(originals, *, output, request_budget, paid_authorized, client_factory, model):
    from defipi.methods.autoguard.configuration import validate_model_settings
    validate_model_settings(model)
    if not paid_authorized:
        raise PermissionError("paraphrase generation makes paid model calls; explicit authorization is required")
    if type(request_budget) is not int or request_budget < 100:
        raise ValueError("formal preparation needs an explicit request budget of at least 100")
    origins = list(originals)
    counts = {p: sum(str(row.get("pattern") or "") == p for row in origins) for p in PATTERNS}
    if len(origins) != 100 or counts != {p: 10 for p in PATTERNS}:
        raise ValueError(f"input must contain 100 explicitly patterned originals, ten per category: {counts}")
    for row in origins:
        for key in ("id", "question", "starting_url", "reporter_info"):
            if not row.get(key):
                raise ValueError(f"original {row.get('id')!r} is missing {key}")
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = out.with_suffix(out.suffix + ".checkpoint.json")
    request_log = out.with_suffix(out.suffix + ".requests_responses.jsonl")
    state = { "complete": False, "completed": {}, "requests": 0}
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
    system = (Path(__file__).parent / "prompts" / "paraphraser.txt").read_text(encoding="utf-8").strip()
    client = client_factory(model, "paraphraser")
    for origin in origins:
        parent_id = str(origin["id"])
        if parent_id in state["completed"]:
            continue
        user = json.dumps({"question": origin["question"]}, ensure_ascii=False)
        record = {"role": "paraphraser", "model": model["model"], "backend": model["backend"],
                  "reasoning_effort": model["reasoning_effort"], "temperature": model["temperature"], "max_tokens": model["max_tokens"],
                  "api_payload": {"model": model["model"], "messages": [
                      {"role": "system", "content": system}, {"role": "user", "content": user}],
                      "reasoning_effort": model["reasoning_effort"]}, "parent_id": parent_id,
                  "system": system, "messages": [{"role": "user", "content": user}]}
        if state["requests"] >= request_budget:
            return {"complete": False, "paused": True, "parent_id": parent_id,
                    "completed_originals": len(state["completed"]), "error": "explicit_request_budget_exhausted",
                    "checkpoint": str(checkpoint)}
        state["requests"] += 1
        _write_checkpoint(checkpoint, state)
        record["http_attempts"] = 0
        def before_request_attempt():
            record["http_attempts"] += 1
        try:
            response = call_model(client, messages=record["messages"], system=system, temperature=model["temperature"],
                                       max_tokens=model["max_tokens"], reasoning_effort=model["reasoning_effort"],
                                       before_request_attempt=before_request_attempt)
            if isinstance(response, dict) and response.get("error"):
                raise RuntimeError(f"provider_error:{response['error']}")
            raw = _response_text(response)
            left, right = _parse_pair(raw)
            if _norm(left) == _norm(origin["question"]) or _norm(right) == _norm(origin["question"]):
                raise ValueError("paraphrase_matches_original_question")
            if _norm(left) == _norm(right):
                raise ValueError("paraphrases_are_not_distinct")
            record["response"] = raw
            record["usage"] = response.get("usage") if isinstance(response, dict) else None
            state["completed"][parent_id] = {"discovery": left, "bandit": right}
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}:{exc}"
            with request_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            _write_checkpoint(checkpoint, state)
            return {"complete": False, "paused": True, "parent_id": parent_id,
                    "completed_originals": len(state["completed"]), "error": record["error"],
                    "checkpoint": str(checkpoint)}
        with request_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _write_checkpoint(checkpoint, state)
    if len(state["completed"]) != 100:
        raise ValueError("paraphrase preparation incomplete")
    cases = []
    for origin in origins:
        parent_id = str(origin["id"])
        paraphrases = state["completed"][parent_id]
        common = {key: origin.get(key) for key in
                  ("pattern", "starting_url", "reporter_info", "page_id", "task_id", "ideal") if key in origin}
        original = {"id": f"{parent_id}-original", **common, "split": "heldout",
                    "question": origin["question"],
                    "provenance": {"kind": "original", "parent_id": parent_id,
                                   "method": "provided original question", "source_artifact": origin.get("source_artifact")}}
        cases.append(original)
        for split in ("discovery", "bandit"):
            cases.append({"id": f"{parent_id}-{split}", **common, "split": split,
                          "question": paraphrases[split],
                          "provenance": {"kind": "paraphrase", "parent_id": parent_id,
                                         "parent_question": origin["question"],
                                         "method": "requested model paraphrase; frozen from saved request/response",
                                         "source_artifact": str(request_log)}})
    from defipi.methods.autoguard.data import validate_cases
    validate_cases(cases, require_paper_cardinality=True)
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases), encoding="utf-8")
    state["complete"] = True
    state["output"] = str(out)
    _write_checkpoint(checkpoint, state)
    return {"complete": True, "cases": len(cases), "training_paraphrases": 200,
            "heldout_originals": 100, "requests": state["requests"], "output": str(out),
            "request_log": str(request_log), "checkpoint": str(checkpoint)}


def _write_checkpoint(path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
