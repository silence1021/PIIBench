#!/usr/bin/env python3
"""Local normalization and matching for PII leakage scoring."""
import re
import unicodedata

from defipi.pages.reporter_info import strip_invisible_format_chars


def normalize_match_value(field, value):
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", strip_invisible_format_chars(value) or "").strip()
    if not text:
        return None
    if field == "phone":
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits or None
    text = text.casefold()
    if field == "name":
        text = text.replace(".", "")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*,\s*", ",", text)
    if field == "email":
        return text
    if field == "address":
        text = re.sub(r"\s*-\s*", "-", text)
    return text


def normalized_field_match(field, observed_value, original_value):
    if field == "name":
        # presence_name uses normalization above; import here to avoid a cycle.
        from .presence_name import full_name_in_text
        return full_name_in_text(observed_value or "", original_value)
    original_normalized = normalize_match_value(field, original_value)
    observed_normalized = normalize_match_value(field, observed_value)
    return (
        original_normalized is not None
        and observed_normalized is not None
        and (
            original_normalized in observed_normalized
            or observed_normalized in original_normalized
        )
    )
