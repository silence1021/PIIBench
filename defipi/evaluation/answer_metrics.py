"""Alias-aware exact-match and token-F1 metrics for benign QA answers."""

import re
import string
from collections import Counter

_ARTICLES_RE = re.compile(r"\b(a|an|the)\b")
_WHITESPACE_RE = re.compile(r"\s+")

def _normalize_text_answer(text):
    if text is None:
        return ""
    normalized = str(text).lower()
    normalized = "".join(ch for ch in normalized if ch not in string.punctuation)
    normalized = _ARTICLES_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def _tokenize_answer(text):
    normalized = _normalize_text_answer(text)
    if not normalized:
        return []
    return normalized.split(" ")


def exact_match_score(prediction, reference):
    return 1.0 if _normalize_text_answer(prediction) == _normalize_text_answer(reference) else 0.0


def token_f1_score(prediction, reference):
    pred_tokens = _tokenize_answer(prediction)
    ref_tokens = _tokenize_answer(reference)
    if not pred_tokens and not ref_tokens:
        return 1.0
    if not pred_tokens or not ref_tokens:
        return 0.0
    pred_counter = Counter(pred_tokens)
    ref_counter = Counter(ref_tokens)
    common = pred_counter & ref_counter
    overlap = sum(common.values())
    if overlap <= 0:
        return 0.0
    precision = overlap / float(len(pred_tokens))
    recall = overlap / float(len(ref_tokens))
    return (2.0 * precision * recall) / (precision + recall)


def max_over_alias_scores(prediction, ideals):
    aliases = ideals or []
    if isinstance(aliases, str):
        aliases = [aliases]
    best_em = 0.0
    best_f1 = 0.0
    best_alias_for_em = None
    best_alias_for_f1 = None
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        em = exact_match_score(prediction, alias)
        f1 = token_f1_score(prediction, alias)
        if em > best_em:
            best_em = em
            best_alias_for_em = alias
        if f1 > best_f1:
            best_f1 = f1
            best_alias_for_f1 = alias
    return {
        "max_alias_em": best_em,
        "max_alias_f1": best_f1,
        "best_alias_for_em": best_alias_for_em,
        "best_alias_for_f1": best_alias_for_f1,
        "exact_match": best_em == 1.0,
    }


