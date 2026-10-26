"""Strict manifest parsing for private real-page snapshot corpora."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from defipi.config import load_config_file
from defipi.configuration.schema import Field, Problem, validate_config
from defipi.configuration.generation import VIEWPORT_FIELDS

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_ALLOWED_FIELD_KINDS = {"name", "email", "phone", "address"}
_ALLOWED_CAPTURE_MODES = {"browser_first", "http_first"}


@dataclass(frozen=True)
class FieldBinding:
    kind: str
    original: str
    synthetic: str
    selectors: tuple[str, ...]


@dataclass(frozen=True)
class CaptureOptions:
    mode: str
    ready_selector: str | None
    wait_after_ready_ms: int
    dismiss_selectors: tuple[str, ...]
    contains_visual_pii: bool
    viewport: dict[str, int]
    action_timeout_ms: int
    navigation_timeout_ms: int
    robots_timeout_seconds: float
    ready_timeout_ms: int | None
    dismiss_timeout_ms: int | None
    http_timeout_seconds: float | None
    max_resources: int | None
    browser_user_agent: str | None


@dataclass(frozen=True)
class PrivacyOptions:
    neutralize_external_links: bool
    placeholder_image_selectors: tuple[str, ...]


@dataclass(frozen=True)
class PageSpec:
    id: str
    url: str
    capture: CaptureOptions
    privacy: PrivacyOptions
    fields: tuple[FieldBinding, ...]
    contact_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotManifest:
    version: int
    pages: tuple[PageSpec, ...]
    path: Path


STRINGS = Field(list, items=Field(str))
CAPTURE_FIELDS = {
    "mode": Field(str, choices=tuple(sorted(_ALLOWED_CAPTURE_MODES))),
    "ready_selector": Field(str, nullable=True),
    "wait_after_ready_ms": Field(int, minimum=0, maximum=30000),
    "dismiss_selectors": STRINGS, "contains_visual_pii": Field(bool),
    "viewport": Field(dict, schema=VIEWPORT_FIELDS),
    "navigation_timeout_ms": Field(int, minimum=1),
    "action_timeout_ms": Field(int, minimum=1),
    "robots_timeout_seconds": Field((int, float), minimum=0.001),
    "ready_timeout_ms": Field(int, minimum=1, required=False),
    "dismiss_timeout_ms": Field(int, minimum=1, required=False),
    "http_timeout_seconds": Field((int, float), minimum=0.001, required=False),
    "browser_user_agent": Field(str, required=False),
    "max_resources": Field(int, minimum=0, required=False),
}
FIELD_FIELDS = {
    "kind": Field(str, choices=tuple(sorted(_ALLOWED_FIELD_KINDS))),
    "original": Field(str), "synthetic": Field(str), "selectors": STRINGS,
}
PAGE_FIELDS = {
    "id": Field(str), "url": Field(str), "capture": Field(dict, schema=CAPTURE_FIELDS),
    "privacy": Field(dict, schema={"neutralize_external_links": Field(bool),
                                   "placeholder_image_selectors": Field(list, required=False, items=Field(str))}),
    "subject": Field(dict, schema={"fields": Field(list, items=Field(dict, schema=FIELD_FIELDS))}),
    "contact_allowlist": Field(list, required=False, items=Field(str)),
}
MANIFEST_FIELDS = {"version": Field(int, choices=(1,)),
                   "pages": Field(list, items=Field(dict, schema=PAGE_FIELDS))}


def _semantic_rules(data, problems):
    pages = data.get("pages")
    if type(pages) is not list:
        return
    if not pages:
        problems.append(Problem("pages", "must not be empty"))
    ids = set()
    for index, page in enumerate(pages):
        if type(page) is not dict:
            continue
        prefix = f"pages[{index}]"
        page_id = page.get("id")
        if type(page_id) is str:
            if not _ID_RE.fullmatch(page_id):
                problems.append(Problem(prefix + ".id", f"must match {_ID_RE.pattern!r}"))
            if page_id in ids:
                problems.append(Problem(prefix + ".id", "duplicate page id"))
            ids.add(page_id)
        if type(page.get("url")) is str and not re.match(r"^https?://", page["url"], re.IGNORECASE):
            problems.append(Problem(prefix + ".url", "must be an http(s) URL"))
        capture = page.get("capture")
        if type(capture) is dict:
            for name, active in (
                ("ready_timeout_ms", type(capture.get("ready_selector")) is str),
                ("dismiss_timeout_ms", bool(capture.get("dismiss_selectors"))),
                ("http_timeout_seconds", capture.get("mode") == "http_first"),
                ("max_resources", capture.get("mode") == "http_first"),
                ("browser_user_agent", capture.get("mode") == "browser_first"),
            ):
                if active and name not in capture:
                    problems.append(Problem(prefix + ".capture." + name, "required for this capture branch"))
                if not active and name in capture:
                    problems.append(Problem(prefix + ".capture." + name, "inactive for this capture branch"))
            for name in ("ready_selector", "browser_user_agent"):
                if type(capture.get(name)) is str and not capture[name].strip():
                    problems.append(Problem(prefix + ".capture." + name, "must not be empty"))
        subject = page.get("subject")
        fields = subject.get("fields") if type(subject) is dict else None
        if type(fields) is list:
            if not any(type(f) is dict and f.get("kind") == "name" for f in fields):
                problems.append(Problem(prefix + ".subject.fields", "must define the subject name"))
            for field_index, field in enumerate(fields):
                if type(field) is not dict:
                    continue
                label = prefix + f".subject.fields[{field_index}]"
                for name in ("original", "synthetic"):
                    if type(field.get(name)) is str and not field[name].strip():
                        problems.append(Problem(label + "." + name, "must not be empty"))
                if type(field.get("original")) is str and field.get("original") == field.get("synthetic"):
                    problems.append(Problem(label, "original and synthetic values must differ"))
                if field.get("selectors") == []:
                    problems.append(Problem(label + ".selectors", "must not be empty"))
        # Selector lists are content, but supplied entries must be usable selectors.
        lists = [(prefix + ".contact_allowlist", page.get("contact_allowlist"))]
        if type(capture) is dict:
            lists.append((prefix + ".capture.dismiss_selectors", capture.get("dismiss_selectors")))
        privacy = page.get("privacy")
        if type(privacy) is dict:
            lists.append((prefix + ".privacy.placeholder_image_selectors", privacy.get("placeholder_image_selectors")))
        if type(fields) is list:
            lists.extend((prefix + f".subject.fields[{i}].selectors", f.get("selectors"))
                         for i, f in enumerate(fields) if type(f) is dict)
        for label, values in lists:
            if type(values) is list:
                for i, value in enumerate(values):
                    if type(value) is str and not value.strip():
                        problems.append(Problem(f"{label}[{i}]", "must not be empty"))


def load_manifest(path: str | Path) -> SnapshotManifest:
    manifest_path = Path(path).expanduser().resolve()
    document = load_config_file(manifest_path)
    validate_config(document, MANIFEST_FIELDS, rules=(_semantic_rules,))
    pages = []
    for data in document["pages"]:
        capture = data["capture"]
        pages.append(PageSpec(
            id=data["id"], url=data["url"],
            capture=CaptureOptions(**{**capture,
                "dismiss_selectors": tuple(capture["dismiss_selectors"]),
                **{name: capture.get(name) for name in ("ready_timeout_ms", "dismiss_timeout_ms", "http_timeout_seconds", "max_resources", "browser_user_agent")}}),
            privacy=PrivacyOptions(data["privacy"]["neutralize_external_links"], tuple(data["privacy"].get("placeholder_image_selectors", ()))),
            fields=tuple(FieldBinding(f["kind"], f["original"].strip(), f["synthetic"].strip(), tuple(s.strip() for s in f["selectors"])) for f in data["subject"]["fields"]),
            contact_allowlist=tuple(s.strip() for s in data.get("contact_allowlist", ())),
        ))
    return SnapshotManifest(version=document["version"], pages=tuple(pages), path=manifest_path)
