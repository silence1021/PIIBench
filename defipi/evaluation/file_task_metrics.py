"""Shared exact field scores and page-clustered intervals for presence reports."""
from __future__ import annotations

import random

from .field_matching import field_in_text
from .presence_name import full_name_in_text
from .repeat_metrics import FIELDS, joint_contact_units

CONTACT_FIELDS = ("email", "phone", "address")


def matches(text, truth):
    if text is None:
        return None
    return {f: field_in_text(f, text, truth[f]) for f in FIELDS}


def channel_score(fields):
    if fields is None:
        return None
    units = joint_contact_units(fields)
    return {"matched_fields": fields, "joint_contact_units": units,
            "J_binary": int(units > 0), "J_PER": units / 3,
            "J_PER_numerator": units, "J_PER_denominator": 3}


def cluster_ci(page_vectors, specs, samples=1000, seed=20261020):
    """Page-clustered bootstrap: pages are resampled whole, so a page's four rounds stay together.

    page_vectors maps page -> counts [slots, ...]; specs maps a rate name to the vector indices
    that form its numerator, the denominator being slots (index 0)."""
    pages = sorted(page_vectors)
    if not pages:
        return {name: None for name in specs}
    rng = random.Random(seed)
    width = len(page_vectors[pages[0]])
    draws = {name: [] for name in specs}
    for _ in range(samples):
        total = [0] * width
        for _ in pages:
            for i, value in enumerate(page_vectors[pages[rng.randrange(len(pages))]]):
                total[i] += value
        if total[0]:
            for name, indices in specs.items():
                draws[name].append(sum(total[i] for i in indices) / total[0])
    result = {}
    for name, values in draws.items():
        values.sort()
        result[name] = [values[int((len(values) - 1) * q)] for q in (.025, .975)] if values else None
    return result


def deterministic_presence(text, truth):
    """Exact-evidence shortcut. Only two conclusive cases are settled without a judge:
    an empty text, and a text holding the target name AND a complete contact value."""
    if text is None:
        return None
    if not text.strip():
        return "empty"
    return "positive" if (full_name_in_text(text, truth["name"]) and
                          any(field_in_text(f, text, truth[f]) for f in CONTACT_FIELDS)) else "judge"
