"""Structure-preserving normalizers for known site-side PII encodings."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .manifest import FieldBinding
from .privacy import field_key


@dataclass(frozen=True)
class NormalizationResult:
    text: str
    counts: dict[str, int]
    audits: tuple[dict[str, object], ...]
    errors: tuple[str, ...]
    structure_preserved: bool


def _structure_token(node: object) -> object:
    if isinstance(node, Comment):
        return ("comment",)
    if isinstance(node, NavigableString):
        return ("text", bool(str(node).strip()))
    if isinstance(node, Tag):
        return (
            "tag",
            node.name,
            tuple(sorted(str(key) for key in node.attrs)),
            tuple(_structure_token(child) for child in node.children),
        )
    children = getattr(node, "children", ())
    return ("document", tuple(_structure_token(child) for child in children))


def decode_pure_mailto_payload(value: str) -> str | None:
    token = value.strip()
    if not token:
        return None
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.b64decode(token + padding, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not decoded.lower().startswith("mailto:"):
        return None
    email = decoded[len("mailto:"):].strip()
    return email or None


def _encode_mailto_payload(email: str) -> str:
    return base64.b64encode(f"mailto:{email}".encode("utf-8")).decode("ascii")


def _separator_for_script(tag: Tag) -> str | None:
    script = tag.get_text("", strip=True)
    if re.search(r"\bencryptedA\s*\(\s*\)", script):
        return "@"
    if re.search(r"\bencryptedDot\s*\(\s*\)", script):
        return "."
    return None


def _email_chunks(anchor: Tag) -> tuple[list[list[NavigableString]], list[str]]:
    chunks: list[list[NavigableString]] = [[]]
    separators: list[str] = []
    for child in anchor.children:
        if isinstance(child, NavigableString):
            if str(child).strip():
                chunks[-1].append(child)
            continue
        if isinstance(child, Tag) and child.name == "script":
            separator = _separator_for_script(child)
            if separator is not None:
                separators.append(separator)
                chunks.append([])
    return chunks, separators


def _replace_text_node(node: NavigableString, value: str) -> None:
    raw = str(node)
    leading = raw[: len(raw) - len(raw.lstrip())]
    trailing = raw[len(raw.rstrip()):]
    node.replace_with(leading + value + trailing)


def _synthetic_segments(email: str) -> tuple[list[str], list[str]]:
    if email.count("@") != 1:
        raise ValueError("synthetic email must contain exactly one @")
    local, domain = email.split("@", 1)
    domain_parts = domain.split(".")
    if not local or any(not part for part in domain_parts):
        raise ValueError("synthetic email has an invalid local/domain topology")
    return [local, *domain_parts], ["@", *(["."] * (len(domain_parts) - 1))]


def normalize_pure_emails(html_text: str, fields: tuple[FieldBinding, ...]) -> NormalizationResult:
    """Rewrite Pure Portal email payload/text values without changing its DOM tree."""
    soup = BeautifulSoup(html_text, "html.parser")
    structure_before = _structure_token(soup)
    email_fields = {
        field.original.casefold(): (index, field)
        for index, field in enumerate(fields)
        if field.kind == "email"
    }
    counts = {field_key(field, index): 0 for index, field in enumerate(fields)}
    audits: list[dict[str, object]] = []
    errors: list[str] = []

    for anchor_index, anchor in enumerate(soup.select("a.email[data-md5]")):
        payload = str(anchor.get("data-md5") or "")
        decoded_email = decode_pure_mailto_payload(payload)
        if decoded_email is None:
            errors.append(f"Pure email anchor {anchor_index} has an invalid data-md5 mailto payload")
            continue
        matched = email_fields.get(decoded_email.casefold())
        if matched is None:
            errors.append(
                f"Pure email anchor {anchor_index} is not declared in the manifest"
            )
            continue
        field_index, field = matched
        chunks, separators = _email_chunks(anchor)
        audit: dict[str, object] = {
            "normalizer": "pure_email",
            "field": field_key(field, field_index),
            "separator_topology": "".join(separators),
        }

        if separators:
            if any(len(chunk) != 1 for chunk in chunks):
                errors.append(
                    f"Pure email anchor {anchor_index} must have exactly one text node per encoded segment"
                )
                continue
            reconstructed = "".join(
                str(chunks[index][0]).strip()
                + (separators[index] if index < len(separators) else "")
                for index in range(len(chunks))
            )
            if reconstructed.casefold() != decoded_email.casefold():
                errors.append(
                    f"Pure email anchor {anchor_index} text/script reconstruction does not match data-md5"
                )
                continue
            try:
                synthetic_chunks, synthetic_separators = _synthetic_segments(field.synthetic)
            except ValueError as exc:
                errors.append(f"Pure email field {field_index}: {exc}")
                continue
            if synthetic_separators != separators or len(synthetic_chunks) != len(chunks):
                errors.append(
                    f"Pure email field {field_index} synthetic address must preserve separator topology"
                )
                continue
            for node, value in zip((chunk[0] for chunk in chunks), synthetic_chunks):
                _replace_text_node(node, value)

        anchor["data-md5"] = _encode_mailto_payload(field.synthetic)
        counts[field_key(field, field_index)] += 1
        audit["status"] = "normalized"
        audits.append(audit)

    structure_after = _structure_token(soup)
    structure_preserved = structure_before == structure_after
    if not structure_preserved:
        errors.append("Pure email normalization changed the DOM node/attribute-name structure")
    return NormalizationResult(
        text=str(soup),
        counts=counts,
        audits=tuple(audits),
        errors=tuple(errors),
        structure_preserved=structure_preserved,
    )
