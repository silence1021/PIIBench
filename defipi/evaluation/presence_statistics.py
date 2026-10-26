"""Offline JP vote counts shared by status and text reports."""
from __future__ import annotations

from collections import Counter, defaultdict

LABELS = ("PRESENT", "ABSENT", "UNKNOWN")


def _counts(votes):
    labels = Counter(v["value"] for v in votes if v["status"] == "ok" and v.get("value") in LABELS)
    denominator = sum(labels.values())
    return {"unique_jobs": len(votes), "states": dict(Counter(v["status"] for v in votes)),
            "label_counts": {label: labels[label] for label in LABELS},
            "evaluable_votes": denominator, "abstention_votes": labels["UNKNOWN"],
            "abstention_rate": labels["UNKNOWN"] / denominator if denominator else None}


def judge_vote_summary(votes):
    """Count each JP job once, regardless of sends or text references.

    Missing/error/unparsed jobs remain visible but do not enter the denominator
    of legitimate completed P/A/U votes. This is separate from text uncertainty.
    """
    unique = {}
    for vote in votes:
        if vote.get("role", "JP") != "JP":
            continue
        key = vote["job_id"]
        if key in unique and unique[key] != vote:
            raise ValueError("Conflicting states for the same judge job")
        unique[key] = vote
    values = list(unique.values())
    judges, providers = defaultdict(list), defaultdict(list)
    for vote in values:
        judges[(vote["model"], vote["provider"], vote.get("alias"))].append(vote)
        providers[vote["provider"]].append(vote)
    return {**_counts(values),
            "by_judge": [{"model": model, "provider": provider, "alias": alias, **_counts(rows)}
                         for (model, provider, alias), rows in sorted(judges.items(), key=lambda item: str(item[0]))],
            "by_provider": [{"provider": provider, **_counts(rows)} for provider, rows in sorted(providers.items())]}
