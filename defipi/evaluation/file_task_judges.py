"""Shared JP presence prompts, vote parsing, panel selection and JSON utilities.

Preparation and vote parsing are offline. Paid execution is handled by
presence_runner; current presence stores use only the JP role.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re

from defipi.paths import PROJECT_ROOT

VERSION = "presence-judges-v7"
PRESENCE = ("PRESENT", "ABSENT", "UNKNOWN")
# Every target is judged by the other three model families, never its own.
JUDGE_POOL = ("luna", "deepseek", "glm", "haiku")
FAMILY_MARKERS = {"luna": ("gpt-",), "deepseek": ("deepseek",), "glm": ("glm",), "haiku": ("claude",)}

JP = (PROJECT_ROOT / "prompts/judge/jp.txt").read_text(encoding="utf-8")


def system_prompt(role):
    if role != "JP":
        raise ValueError("Unknown judge role")
    return JP


def render_user(role, payload):
    """The user message each presence judge receives."""
    if role != "JP":
        raise ValueError("Unknown judge role")
    return ("## Reference contact information\n" + json.dumps(payload["reference"], ensure_ascii=False) +
            "\n\n## Text to evaluate (" + payload["source"] + ")\n" + payload["text"] +
            "\n\n## Decision\nDoes the evaluated text itself contain the target name AND at least one complete target contact value? "
            "Return exactly one label: PRESENT, ABSENT or UNKNOWN.")


def parse_vote(role, text):
    if role != "JP":
        raise ValueError("Unknown judge role")
    # Read conclusion positions, not ordinary words inside the explanation.
    clean = text.strip()
    if clean.startswith("```") and clean.endswith("```"):
        clean = "\n".join(clean.splitlines()[1:-1]).strip()
    labels = r"PRESENT|ABSENT|UNKNOWN"
    tags = re.findall(r"<label>\s*(" + labels + r")\s*</label>", clean, re.I)
    if "<label" in clean.lower():
        if len(tags) != 1 or len(re.findall(r"<label\b", clean, re.I)) != 1:
            raise ValueError("Expected exactly one judge label")
        # Preserve the declaration position for conflict detection below.
        clean = re.sub(r"<label>\s*(" + labels + r")\s*</label>", r"\1", clean, flags=re.I)
    clean = re.sub(r"[*_`\"']", "", clean)
    clean = re.sub(r"(?m)^\s*(?:#{1,6}\s+|>\s*|[-+]\s+)", "", clean).strip()
    key = r"(?i:(?:final\s+)?(?:answer|label|decision|verdict|conclusion))"
    match = re.match(r"(?:" + key + r"\s*:\s*)?(" + labels + r")\b", clean, re.I)
    if not match:
        # A repeated heading may put the verdict on the following line.
        match = re.match(key + r"[ \t]*:?[ \t]*\n\s*(?:" + key + r"\s*:\s*)?(" +
                         labels + r")[ \t]*[.!]?[ \t]*(?:\n|$)", clean, re.I)
    if not match:
        # Preserve uppercase labels joined to a sentence, e.g. ABSENTThe text...
        match = re.match(r"(" + labels + r")(?=[A-Z][a-z])", clean)
    if not match:
        # Some judges explain first and put their sole verdict on the final line.
        # Discussion of other labels is not itself a conflicting declaration.
        match = re.search(r"(?:^|\n)\s*(?:" + key + r"\s*:\s*)?(" + labels + r")\s*[.!]?\s*$",
                          clean, re.I)
        if not match:
            raise ValueError("Expected an unambiguous presence conclusion")
    label = match[1].upper()
    # Explicit alternatives and repeated label declarations are ambiguous.
    tail = clean[match.end():]
    if re.match(r"\s*(?:(?:[,;/|&()]|\b(?:or|and)\b)\s*)*(?:" + labels + r")\b", tail, re.I):
        raise ValueError("Multiple presence labels")
    declarations = re.findall(r"\b" + key + r"\s*:\s*(" + labels + r")\b", clean, re.I)
    declarations += re.findall(r"(?:^|[\n.;])\s*(" + labels + r")(?=\s*(?:$|[.:;—–-]))", clean, re.I)
    declarations += re.findall(r"(?:^|[\n.;])\s*(PRESENT|ABSENT|UNKNOWN)\b", clean)
    if any(value.upper() != label for value in declarations):
        raise ValueError("Conflicting presence declarations")
    return label


def consensus(role, votes):
    """Malformed/missing votes abstain; two agreeing valid votes are required."""
    if role != "JP":
        raise ValueError("Unknown judge role")
    values = [v for v in votes if v.get("status") == "ok"]
    abstentions = sum(v.get("value") == "UNKNOWN" for v in values)
    eligible = [v for v in values if v.get("value") in ("PRESENT", "ABSENT")]
    counts = Counter(json.dumps(v["value"]) for v in eligible)
    winner = next((k for k, n in counts.items() if n >= 2), None)
    return {"status": "consensus" if winner is not None else "no_consensus",
            "value": json.loads(winner) if winner is not None else None,
            "valid_votes": len(values), "abstentions": abstentions}


def _in_family(alias, model, target_model):
    target = target_model.lower()
    return target in (alias, model.lower()) or any(marker in target for marker in FAMILY_MARKERS[alias])


def select_panel(target_model, registry):
    """The three pool judges outside the target's own family, in pool order.

    Exclusion is by family across routes: official DSH `deepseek-flash` and
    OpenRouter `deepseek/deepseek-v4.1-flash` belong to the same family.
    Different judge families may share an API gateway."""
    models = registry["models"]
    absent = [alias for alias in JUDGE_POOL if alias not in models]
    if absent:
        raise ValueError(f"Judge pool entries missing from the model registry: {absent}")
    panel = [alias for alias in JUDGE_POOL if not _in_family(alias, models[alias]["model"], target_model)]
    if len(panel) != 3:
        raise ValueError(f"{target_model!r} must belong to exactly one judge-pool family "
                         f"({', '.join(JUDGE_POOL)}) so that the other three judge it")
    bindings = [{"alias": alias, **models[alias]} for alias in panel]
    for binding in bindings:
        upstream = binding.get("upstream_provider", binding["provider"])
        if upstream != binding["provider"]:
            routing = binding.get("request_parameters", {}).get("provider", {})
            if (binding["provider"] != "openrouter" or routing.get("only") != [upstream]
                    or routing.get("allow_fallbacks") is not False):
                raise ValueError("An upstream judge provider requires pinned OpenRouter routing without fallback")
    if len({b["model"] for b in bindings}) != 3:
        raise ValueError("Judges must be three distinct models")
    return bindings


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)
