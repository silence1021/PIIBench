"""PII replacement and residual-contact auditing."""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote, quote_plus

from bs4 import BeautifulSoup

from .manifest import FieldBinding


_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<![\w/])(?:\+\d{1,3}[\s().-]*)?(?:\(?\d{2,4}\)?[\s.-]){2,4}\d{2,4}(?![\w/])"
)
_TEL_URI_RE = re.compile(r"tel:\s*([+0-9][0-9\s().-]{6,}[0-9])", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ORCID_RE = re.compile(r"(?<!\d)\d{4}-\d{4}-\d{4}-[\dX]{4}(?!\d)", re.IGNORECASE)


@dataclass(frozen=True)
class ReplacementResult:
    text: str
    counts: dict[str, int]


def field_key(field: FieldBinding, index: int) -> str:
    return f"{index}:{field.kind}"


def _json_escape(value: str, *, ensure_ascii: bool) -> str:
    return json.dumps(value, ensure_ascii=ensure_ascii)[1:-1]


def _numeric_entities(value: str, *, hexadecimal: bool) -> str:
    if hexadecimal:
        return "".join(f"&#x{ord(character):x};" for character in value)
    return "".join(f"&#{ord(character)};" for character in value)


def _lower_percent_hex(value: str) -> str:
    encoded = quote(value, safe="")
    return re.sub(r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), encoded)


def encoded_pairs(field: FieldBinding) -> tuple[tuple[str, str], ...]:
    encoders = [
        lambda value: value,
        lambda value: html.escape(value, quote=False),
        lambda value: html.escape(value, quote=True),
        lambda value: value.replace("@", "&#64;"),
        lambda value: value.replace("@", "&#x40;"),
        lambda value: value.replace("@", "&commat;"),
        lambda value: value.replace(" ", "&#32;"),
        lambda value: value.replace(" ", "&nbsp;"),
        lambda value: _numeric_entities(value, hexadecimal=False),
        lambda value: _numeric_entities(value, hexadecimal=True),
        lambda value: quote(value, safe=""),
        _lower_percent_hex,
        lambda value: quote_plus(value, safe=""),
        lambda value: _json_escape(value, ensure_ascii=False),
        lambda value: _json_escape(value, ensure_ascii=True),
    ]
    if field.kind == "email":
        encoders.extend(
            [
                lambda value: base64.b64encode(value.encode("utf-8")).decode("ascii"),
                lambda value: base64.b64encode(f"mailto:{value}".encode("utf-8")).decode("ascii"),
            ]
        )
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for encoder in encoders:
        original = encoder(field.original)
        synthetic = encoder(field.synthetic)
        if original and original not in seen:
            seen.add(original)
            pairs.append((original, synthetic))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return tuple(pairs)


def replace_pii(text: str, fields: Iterable[FieldBinding]) -> ReplacementResult:
    output = text
    counts: dict[str, int] = {}
    for index, field in enumerate(fields):
        key = field_key(field, index)
        count = 0
        for original, synthetic in encoded_pairs(field):
            flags = re.IGNORECASE if field.kind == "email" else 0
            pattern = re.compile(re.escape(original), flags)
            output, replaced = pattern.subn(lambda _match, value=synthetic: value, output)
            count += replaced
        counts[key] = count
    return ReplacementResult(text=output, counts=counts)


def residual_original_fields(text: str, fields: Iterable[FieldBinding]) -> tuple[str, ...]:
    residual: list[str] = []
    for index, field in enumerate(fields):
        for original, _synthetic in encoded_pairs(field):
            flags = re.IGNORECASE if field.kind == "email" else 0
            if re.search(re.escape(original), text, flags):
                residual.append(field_key(field, index))
                break
    return tuple(residual)


def _normalize_phone(value: str) -> str:
    prefix = "+" if value.strip().startswith("+") else ""
    return prefix + "".join(character for character in value if character.isdigit())


def _allowed_contact_sets(
    fields: Iterable[FieldBinding], allowlist: Iterable[str]
) -> tuple[set[str], set[str]]:
    allowed_emails: set[str] = set()
    allowed_phones: set[str] = set()
    for field in fields:
        if field.kind == "email":
            allowed_emails.add(field.synthetic.lower())
        elif field.kind == "phone":
            allowed_phones.add(_normalize_phone(field.synthetic))
    for value in allowlist:
        if "@" in value:
            allowed_emails.add(value.lower())
        if sum(character.isdigit() for character in value) >= 7:
            allowed_phones.add(_normalize_phone(value))
    return allowed_emails, allowed_phones


def phone_scan_text(text: str, suffix: str) -> str:
    """Remove non-linguistic markup before applying the loose phone heuristic.

    Exact declared-value checks and email/tel URI scans still use the original
    artifact text. This filter only prevents vector coordinates and CSS numeric
    data from being interpreted as previously undeclared telephone numbers.
    """

    normalized_suffix = suffix.lower()
    if normalized_suffix in {".svg", ".css"}:
        return ""
    if normalized_suffix in {".html", ".htm"}:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup.find_all(["svg", "style"]):
            tag.decompose()
        return str(soup)
    return text


def _overlaps(span: tuple[int, int], excluded: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < excluded_end and excluded_start < end for excluded_start, excluded_end in excluded)


def scan_unlisted_contacts(
    text: str,
    fields: Iterable[FieldBinding],
    allowlist: Iterable[str],
    *,
    phone_text: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    allowed_emails, allowed_phones = _allowed_contact_sets(fields, allowlist)
    email_findings: dict[str, str] = {}
    phone_findings: dict[str, str] = {}

    for match in _EMAIL_RE.finditer(text):
        value = match.group(0)
        normalized = value.lower()
        if normalized not in allowed_emails:
            email_findings.setdefault(normalized, f"email-{len(email_findings)}")

    heuristic_text = text if phone_text is None else phone_text
    orcid_spans = [match.span() for match in _ORCID_RE.finditer(heuristic_text)]
    for match in _PHONE_RE.finditer(heuristic_text):
        value = match.group(0).strip()
        if _ISO_DATE_RE.fullmatch(value) or _overlaps(match.span(), orcid_spans):
            continue
        normalized = _normalize_phone(value)
        digit_count = sum(character.isdigit() for character in normalized)
        if 7 <= digit_count <= 15 and normalized not in allowed_phones:
            phone_findings.setdefault(normalized, f"phone-{len(phone_findings)}")
    for match in _TEL_URI_RE.finditer(text):
        value = match.group(1).strip()
        normalized = _normalize_phone(value)
        digit_count = sum(character.isdigit() for character in normalized)
        if 7 <= digit_count <= 15 and normalized not in allowed_phones:
            phone_findings.setdefault(normalized, f"phone-{len(phone_findings)}")

    return {
        "emails": [
            {"finding_id": finding_id}
            for _value, finding_id in sorted(email_findings.items())
        ],
        "phones": [
            {"finding_id": finding_id}
            for _value, finding_id in sorted(phone_findings.items())
        ],
    }


def has_contact_findings(findings: dict[str, list[dict[str, str]]]) -> bool:
    return any(findings.get(kind) for kind in ("emails", "phones"))
