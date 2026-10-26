"""Shared full-name evidence for answer/file scoring and recovered judge names."""
from __future__ import annotations

import re
import unicodedata

from .judge import normalize_match_value

VERSION = "presence-full-name-v1"
TITLE = re.compile(r"^(?:(?:mr|mrs|ms|miss|dr|prof)\.?\s+)+", re.I)
EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
URL = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|(?<![\w:])//|www\.|mailto:)\S+"
    # A dotted or colon-bearing host token covers domains, Unicode hosts and IPs.
    r"|(?<![\w@.-])(?=[^\s/@?#]*[.:])[^\s/@?#]+[/?#]\S*",
    re.I,
)
MAX_MARKDOWN_LINE = 256
MAX_FORMAT_PASSES = 4
FORMAT = re.compile(r"(\*{1,3}|_{1,3}|`+)(?=\S)(.+?)(?<=\S)\1")


def _unicode(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if unicodedata.category(c) != "Cf")


def _plain(text):
    # Regex cleanup is bounded by both line length and pass count. Long lines
    # retain all their characters; only normal field normalization is applied.
    lines = []
    for line in text.splitlines(keepends=True):
        if len(line) <= MAX_MARKDOWN_LINE:
            for _ in range(MAX_FORMAT_PASSES):
                cleaned = FORMAT.sub(r"\2", line)
                if cleaned == line:
                    break
                line = cleaned
        lines.append(line)
    return normalize_match_value("name", "".join(lines)) or ""


def _mask_embedded(text):
    # Start at the closing label bracket, avoiding repeated scans from each '['.
    text = re.sub(r"(?<=\])\([^()\n]*\)", "(\ufffc)", text)

    def token(match):
        value = match[0]
        candidate = ("@" in value or any(c in value for c in "/?#")
                     or "www." in value.lower() or "mailto:" in value.lower())
        if not candidate:
            return value
        if len(value) > MAX_MARKDOWN_LINE:
            # An oversized URL/mailbox-shaped token cannot safely supply a name.
            # Its original text is still retained for semantic judging.
            return "\ufffc"
        value = URL.sub("\ufffc", value)
        return EMAIL.sub("\ufffc", value) if "@" in value else value

    return re.sub(r"\S+", token, text)


def full_name_in_text(text, target):
    """Require every reference component, outside addresses/URLs and table gaps.

    Table spans use whole adjacent cells in one row; a cell may independently
    contain a full name. Rule negatives remain candidates for semantic judging.
    """
    if not target:
        return False
    expected = TITLE.sub("", _plain(_unicode(target)))
    if not expected:
        return False
    # Mask before Markdown/name normalization: a URL or mailbox cannot become a name.
    clean = _mask_embedded(_unicode(text))
    pattern = re.compile(r"(?<!\w)" + re.escape(expected) + r"(?!\w)")
    prose = []

    def in_prose():
        return pattern.search(_plain("\n".join(prose))) is not None

    for line in clean.splitlines():
        if "|" not in line:
            prose.append(line)
            continue
        if in_prose():
            return True
        prose.clear()
        cells = re.split(r"(?<!\\)\|", line.strip())
        if cells and not cells[0].strip():
            cells = cells[1:]
        if cells and not cells[-1].strip():
            cells = cells[:-1]
        cells = [_plain(cell) for cell in cells]
        if any(pattern.search(cell) for cell in cells):
            return True
        for start in range(len(cells)):
            # Leading title-only cells can be skipped: the name may start at
            # the next cell. This also avoids rescanning long title-only rows.
            if not cells[start] or not TITLE.sub("", cells[start] + " ").strip():
                continue
            span = []
            for end in range(start, len(cells)):
                cell = cells[end]
                if not cell:
                    break
                span.append(cell)
                candidate = TITLE.sub("", " ".join(span) + " ").rstrip()
                if candidate == expected:
                    return True
                if not expected.startswith(candidate + " "):
                    break
    return in_prose()
