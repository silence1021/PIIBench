"""Build privacy-safe source/static/scripted snapshots from raw captures."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .manifest import PageSpec, SnapshotManifest
from .normalizers import decode_pure_mailto_payload, normalize_pure_emails
from .privacy import (
    field_key,
    has_contact_findings,
    phone_scan_text,
    replace_pii,
    residual_original_fields,
    scan_unlisted_contacts,
)
from .transforms import (
    PLACEHOLDER_IMAGE_BYTES,
    PLACEHOLDER_IMAGE_FILENAME,
    mapped_resource_filename,
    normalize_resource_url,
    rewrite_css,
    rewrite_html,
    rewrite_known_urls,
)
from defipi.jsonio import write_json as _write_json


_TEXT_CONTENT_TYPES = {
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/manifest+json",
    "application/xml",
    "image/svg+xml",
    "text/css",
    "text/html",
    "text/javascript",
    "text/plain",
    "text/xml",
}
_TEXT_SUFFIXES = {".css", ".html", ".htm", ".js", ".json", ".mjs", ".svg", ".txt", ".xml"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _is_text_asset(record: dict[str, object], filename: str) -> bool:
    content_type = str(record.get("content_type") or "").lower()
    return content_type.startswith("text/") or content_type in _TEXT_CONTENT_TYPES or Path(filename).suffix.lower() in _TEXT_SUFFIXES


def _selector_errors(rendered_html: str, spec: PageSpec) -> list[str]:
    soup = BeautifulSoup(rendered_html, "html.parser")
    errors: list[str] = []
    for index, field in enumerate(spec.fields):
        key = field_key(field, index)
        for selector in field.selectors:
            try:
                matches = soup.select(selector)
            except Exception as exc:
                errors.append(f"invalid selector for {key}: {selector!r} ({type(exc).__name__})")
                continue
            if not matches:
                errors.append(f"selector did not match rendered capture for {key}: {selector!r}")
                continue
            needle = field.original.casefold() if field.kind == "email" else field.original
            contains = any(
                needle in (str(match).casefold() if field.kind == "email" else str(match))
                for match in matches
            )
            if not contains and field.kind == "email":
                for match in matches:
                    encoded = match.find(attrs={"data-md5": True})
                    if encoded is None and match.get("data-md5") is not None:
                        encoded = match
                    if encoded is None:
                        continue
                    decoded = decode_pure_mailto_payload(
                        str(encoded.get("data-md5") or "")
                    )
                    if decoded and decoded.casefold() == field.original.casefold():
                        contains = True
                        break
            if not contains:
                errors.append(f"selector does not contain declared original value for {key}: {selector!r}")
    return errors


def _resource_map(page_capture: dict[str, object]) -> tuple[dict[str, str], list[dict[str, object]]]:
    mapping: dict[str, str] = {}
    failures: list[dict[str, object]] = []
    resources = page_capture.get("resources") or []
    if not isinstance(resources, list):
        raise ValueError("capture resources must be a list")
    for raw_record in resources:
        if not isinstance(raw_record, dict):
            continue
        record = dict(raw_record)
        url = str(record.get("url") or "")
        filename = record.get("filename")
        if record.get("captured") and isinstance(filename, str) and url:
            mapping[normalize_resource_url(url, url)] = filename
        elif url:
            failures.append(
                {
                    "resource_type": record.get("resource_type"),
                    "error_type": str(record.get("error") or "capture_failed").split(":", 1)[0],
                }
            )
    return mapping, failures


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _selected_image_filenames(
    rendered_html: str,
    spec: PageSpec,
    document_url: str,
    resource_map: dict[str, str],
) -> tuple[set[str], list[str]]:
    soup = BeautifulSoup(rendered_html, "html.parser")
    filenames: set[str] = set()
    errors: list[str] = []
    for selector in spec.privacy.placeholder_image_selectors:
        try:
            matches = soup.select(selector)
        except Exception as exc:
            errors.append(f"invalid placeholder image selector {selector!r}: {type(exc).__name__}")
            continue
        if not matches:
            errors.append(f"placeholder image selector did not match rendered capture: {selector!r}")
            continue
        for tag in matches:
            if tag.name != "img":
                errors.append(f"placeholder image selector must match <img>, got <{tag.name}>: {selector!r}")
                continue
            candidates: list[str] = []
            for attribute in ("src", "data-src", "data-original"):
                value = tag.get(attribute)
                if isinstance(value, str):
                    candidates.append(value)
            for attribute in ("srcset", "data-srcset"):
                value = tag.get(attribute)
                if isinstance(value, str):
                    candidates.extend(
                        part.strip().split()[0] for part in value.split(",") if part.strip()
                    )
            if tag.parent is not None and getattr(tag.parent, "name", None) == "picture":
                for source in tag.parent.find_all("source"):
                    for attribute in ("src", "srcset"):
                        value = source.get(attribute)
                        if isinstance(value, str):
                            candidates.extend(
                                part.strip().split()[0] for part in value.split(",") if part.strip()
                            )
            for candidate in candidates:
                if candidate.startswith(("data:", "blob:")):
                    continue
                filename = mapped_resource_filename(
                    candidate, document_url, resource_map
                )
                if filename is not None:
                    filenames.add(filename)
    return filenames, errors


def _resource_reference_counts(
    html_text: str,
    document_url: str,
    resource_map: dict[str, str],
    asset_aliases: dict[str, str],
    neutralize_script_filenames: frozenset[str] = frozenset(),
) -> dict[str, int]:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all(["noscript", "iframe", "frame", "object", "embed"]):
        tag.decompose()

    def resolves(value: object) -> bool:
        return isinstance(value, str) and mapped_resource_filename(
            value, document_url, resource_map, asset_aliases
        ) is not None

    stylesheets = sum(
        1
        for tag in soup.find_all("link")
        if "stylesheet" in {str(value).lower() for value in tag.get("rel") or []}
        and resolves(tag.get("href"))
    )
    scripts = 0
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if not resolves(src):
            continue
        filename = mapped_resource_filename(
            str(src), document_url, resource_map, asset_aliases
        )
        if filename not in neutralize_script_filenames:
            scripts += 1
    images = 0
    for tag in soup.find_all("img"):
        candidates = [tag.get(name) for name in ("src", "data-src", "data-original")]
        for name in ("srcset", "data-srcset"):
            value = tag.get(name)
            if isinstance(value, str):
                candidates.extend(
                    part.strip().split()[0]
                    for part in value.split(",")
                    if part.strip()
                )
        if any(resolves(candidate) for candidate in candidates):
            images += 1
    return {"stylesheets": stylesheets, "scripts": scripts, "images": images}


_SCRIPT_ELEMENT_RE = re.compile(
    r"createElement\s*\(\s*['\"]script['\"]\s*\)", re.IGNORECASE
)


def _problematic_script_loader_filenames(
    raw_page_dir: Path,
    page_capture: dict[str, object],
) -> frozenset[str]:
    """Identify filename-sensitive loaders unsafe under content addressing.

    These loaders derive another script URL from ``document.currentScript``.
    Replaying an already-rendered DOM under a resource filename can therefore make
    the loader append itself indefinitely. The rendered DOM already contains
    the loader's result, so only the loader tag is neutralized in scripted mode.
    """

    filenames: set[str] = set()
    for raw_record in page_capture.get("resources") or []:
        if not isinstance(raw_record, dict) or not raw_record.get("captured"):
            continue
        filename = raw_record.get("filename")
        if not isinstance(filename, str) or Path(filename).suffix.lower() not in {
            ".js",
            ".mjs",
        }:
            continue
        path = raw_page_dir / "assets" / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        if (
            "document.currentscript" in lowered
            and "appendchild" in lowered
            and _SCRIPT_ELEMENT_RE.search(text)
        ):
            filenames.add(filename)
    return frozenset(filenames)


def _build_assets(
    raw_page_dir: Path,
    safe_page_dir: Path,
    spec: PageSpec,
    page_capture: dict[str, object],
    resource_map: dict[str, str],
    asset_aliases: dict[str, str],
    excluded_filenames: set[str],
) -> tuple[dict[str, int], list[str], list[dict[str, object]], list[str]]:
    raw_assets = raw_page_dir / "assets"
    safe_assets = safe_page_dir / "assets"
    safe_assets.mkdir()
    replacement_counts: dict[str, int] = {}
    residual: set[str] = set()
    unresolved: set[str] = set()
    built_assets: list[dict[str, object]] = []

    resources_by_filename: dict[str, dict[str, object]] = {}
    for raw_record in page_capture.get("resources") or []:
        if isinstance(raw_record, dict) and raw_record.get("captured") and isinstance(raw_record.get("filename"), str):
            resources_by_filename.setdefault(str(raw_record["filename"]), dict(raw_record))

    for filename, record in sorted(resources_by_filename.items()):
        if filename in excluded_filenames:
            continue
        source_path = raw_assets / filename
        if not source_path.exists():
            raise FileNotFoundError(f"Captured asset is missing: {source_path}")
        target_path = safe_assets / filename
        source_bytes = source_path.read_bytes()
        if _is_text_asset(record, filename):
            source_text = source_bytes.decode("utf-8", errors="replace")
            replacement = replace_pii(source_text, spec.fields)
            _merge_counts(replacement_counts, replacement.counts)
            transformed = replacement.text
            asset_url = str(record.get("url") or page_capture.get("final_url") or spec.url)
            content_type = str(record.get("content_type") or "").lower()
            if content_type == "text/css" or Path(filename).suffix.lower() == ".css":
                css_result = rewrite_css(
                    transformed,
                    base_url=asset_url,
                    resource_map=resource_map,
                    asset_aliases=asset_aliases,
                )
                transformed = css_result.text
                unresolved.update(css_result.unresolved)
            else:
                transformed = rewrite_known_urls(
                    transformed,
                    base_url=asset_url,
                    resource_map=resource_map,
                    asset_aliases=asset_aliases,
                )
            residual.update(residual_original_fields(transformed, spec.fields))
            target_path.write_text(transformed, encoding="utf-8")
        else:
            shutil.copyfile(source_path, target_path)
        built_assets.append(
            {
                "filename": filename,
                "content_type": record.get("content_type"),
                "byte_size": target_path.stat().st_size,
            }
        )
    return replacement_counts, sorted(residual), built_assets, sorted(unresolved)


def _text_artifacts(page_dir: Path) -> list[Path]:
    paths = [page_dir / "source.html", page_dir / "static" / "index.html", page_dir / "scripted" / "index.html"]
    paths.extend(path for path in (page_dir / "assets").iterdir() if path.suffix.lower() in _TEXT_SUFFIXES)
    return paths


def _build_page(
    raw_page_dir: Path,
    safe_page_dir: Path,
    spec: PageSpec,
    capture_id: str,
) -> dict[str, object]:
    page_capture = _read_json(raw_page_dir / "capture.json")
    if page_capture.get("status") != "ok":
        raise ValueError(f"Raw capture for {spec.id} is not valid: {page_capture.get('errors')}")
    source_html = (raw_page_dir / "source.html").read_text(encoding="utf-8")
    rendered_html = (raw_page_dir / "rendered.html").read_text(encoding="utf-8")
    errors = _selector_errors(rendered_html, spec)

    resource_map, resource_failures = _resource_map(page_capture)
    document_url = str(page_capture.get("final_url") or spec.url)
    neutralize_script_filenames = _problematic_script_loader_filenames(
        raw_page_dir, page_capture
    )
    selected_image_filenames, image_selector_errors = _selected_image_filenames(
        rendered_html, spec, document_url, resource_map
    )
    errors.extend(image_selector_errors)
    excluded_filenames = set(selected_image_filenames)
    asset_aliases = {filename: filename for filename in resource_map.values()}
    for filename in selected_image_filenames:
        asset_aliases[filename] = PLACEHOLDER_IMAGE_FILENAME

    safe_page_dir.mkdir(parents=True, exist_ok=False)
    (safe_page_dir / "static").mkdir()
    (safe_page_dir / "scripted").mkdir()

    counts, asset_residual, built_assets, asset_unresolved = _build_assets(
        raw_page_dir,
        safe_page_dir,
        spec,
        page_capture,
        resource_map,
        asset_aliases,
        excluded_filenames,
    )
    if spec.privacy.placeholder_image_selectors:
        placeholder_path = safe_page_dir / "assets" / PLACEHOLDER_IMAGE_FILENAME
        placeholder_path.write_bytes(PLACEHOLDER_IMAGE_BYTES)
        built_assets.append(
            {
                "filename": PLACEHOLDER_IMAGE_FILENAME,
                "content_type": "image/svg+xml",
                "byte_size": placeholder_path.stat().st_size,
                "role": "profile_image_placeholder",
            }
        )
    residual: set[str] = set(asset_residual)
    unresolved: set[str] = set(asset_unresolved)
    source_normalization = normalize_pure_emails(source_html, spec.fields)
    rendered_normalization = normalize_pure_emails(rendered_html, spec.fields)
    errors.extend(source_normalization.errors)
    errors.extend(rendered_normalization.errors)
    _merge_counts(counts, source_normalization.counts)
    _merge_counts(counts, rendered_normalization.counts)
    source_replacement = replace_pii(source_normalization.text, spec.fields)
    rendered_replacement = replace_pii(rendered_normalization.text, spec.fields)
    _merge_counts(counts, source_replacement.counts)
    _merge_counts(counts, rendered_replacement.counts)

    source_result = rewrite_html(
        source_replacement.text,
        document_url=document_url,
        resource_map=resource_map,
        asset_prefix="assets",
        mode="source",
        neutralize_external_links=spec.privacy.neutralize_external_links,
        placeholder_image_selectors=spec.privacy.placeholder_image_selectors,
        placeholder_image_filename=(
            PLACEHOLDER_IMAGE_FILENAME if spec.privacy.placeholder_image_selectors else None
        ),
        asset_aliases=asset_aliases,
        neutralize_script_filenames=neutralize_script_filenames,
    )
    static_result = rewrite_html(
        rendered_replacement.text,
        document_url=document_url,
        resource_map=resource_map,
        asset_prefix="../assets",
        mode="static",
        neutralize_external_links=spec.privacy.neutralize_external_links,
        placeholder_image_selectors=spec.privacy.placeholder_image_selectors,
        placeholder_image_filename=(
            PLACEHOLDER_IMAGE_FILENAME if spec.privacy.placeholder_image_selectors else None
        ),
        asset_aliases=asset_aliases,
        neutralize_script_filenames=neutralize_script_filenames,
    )
    scripted_result = rewrite_html(
        rendered_replacement.text,
        document_url=document_url,
        resource_map=resource_map,
        asset_prefix="../assets",
        mode="scripted",
        neutralize_external_links=spec.privacy.neutralize_external_links,
        placeholder_image_selectors=spec.privacy.placeholder_image_selectors,
        placeholder_image_filename=(
            PLACEHOLDER_IMAGE_FILENAME if spec.privacy.placeholder_image_selectors else None
        ),
        asset_aliases=asset_aliases,
        neutralize_script_filenames=neutralize_script_filenames,
    )
    resource_expectations = _resource_reference_counts(
        rendered_replacement.text,
        document_url,
        resource_map,
        asset_aliases,
        neutralize_script_filenames,
    )
    representation_resource_counts = {
        "static": _resource_reference_counts(
            static_result.text, document_url, resource_map, asset_aliases
        ),
        "scripted": _resource_reference_counts(
            scripted_result.text,
            document_url,
            resource_map,
            asset_aliases,
            neutralize_script_filenames,
        ),
    }
    for variant in ("static", "scripted"):
        actual = representation_resource_counts[variant]
        for kind in ("stylesheets", "images"):
            if actual[kind] < resource_expectations[kind]:
                errors.append(
                    f"{variant} snapshot lost localized {kind}: "
                    f"expected {resource_expectations[kind]}, found {actual[kind]}"
                )
    if representation_resource_counts["scripted"]["scripts"] < resource_expectations["scripts"]:
        errors.append(
            "scripted snapshot lost localized scripts: "
            f"expected {resource_expectations['scripts']}, "
            f"found {representation_resource_counts['scripted']['scripts']}"
        )
    unresolved.update(source_result.unresolved)
    unresolved.update(static_result.unresolved)
    unresolved.update(scripted_result.unresolved)
    redaction_counts = {
        "source": dict(source_result.redactions),
        "static": dict(static_result.redactions),
        "scripted": dict(scripted_result.redactions),
    }
    for selector in spec.privacy.placeholder_image_selectors:
        key = f"placeholder:{selector}"
        for mode in ("static", "scripted"):
            if redaction_counts[mode].get(key, 0) == 0:
                errors.append(f"{mode} snapshot did not replace placeholder image selector: {selector!r}")

    source_final = replace_pii(source_result.text, spec.fields)
    static_final = replace_pii(static_result.text, spec.fields)
    scripted_final = replace_pii(scripted_result.text, spec.fields)
    _merge_counts(counts, source_final.counts)
    _merge_counts(counts, static_final.counts)
    _merge_counts(counts, scripted_final.counts)
    (safe_page_dir / "source.html").write_text(source_final.text, encoding="utf-8")
    (safe_page_dir / "static" / "index.html").write_text(static_final.text, encoding="utf-8")
    (safe_page_dir / "scripted" / "index.html").write_text(scripted_final.text, encoding="utf-8")

    contact_text_parts: list[str] = []
    phone_text_parts: list[str] = []
    for path in _text_artifacts(safe_page_dir):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name != PLACEHOLDER_IMAGE_FILENAME:
            contact_text_parts.append(text)
            filtered_phone_text = phone_scan_text(text, path.suffix)
            if filtered_phone_text:
                phone_text_parts.append(filtered_phone_text)
        residual.update(residual_original_fields(text, spec.fields))
    contacts = scan_unlisted_contacts(
        "\n".join(contact_text_parts),
        spec.fields,
        spec.contact_allowlist,
        phone_text="\n".join(phone_text_parts),
    )

    for index, field in enumerate(spec.fields):
        key = field_key(field, index)
        if counts.get(key, 0) == 0:
            errors.append(f"declared original value was not found for {key}")
    if residual:
        errors.append("declared original PII remains in built text artifacts")
    if has_contact_findings(contacts):
        errors.append("unlisted email or phone contacts remain in built text artifacts")

    synthetic_fields = [
        {
            "kind": field.kind,
            "synthetic": field.synthetic,
            "selectors": list(field.selectors),
        }
        for field in spec.fields
    ]
    snapshot = {
        "version": 1,
        "capture_id": capture_id,
        "page_id": spec.id,
        "built_at": _utc_now(),
        "synthetic_fields": synthetic_fields,
        "contact_allowlist": list(spec.contact_allowlist),
        "normalization": {
            "structure_preserved": (
                source_normalization.structure_preserved
                and rendered_normalization.structure_preserved
            ),
            "source": {
                "audits": list(source_normalization.audits),
            },
            "rendered": {
                "audits": list(rendered_normalization.audits),
            },
            "errors": [
                *source_normalization.errors,
                *rendered_normalization.errors,
            ],
        },
        "privacy": {
            "neutralize_external_links": spec.privacy.neutralize_external_links,
            "placeholder_image_selectors": list(spec.privacy.placeholder_image_selectors),
            "placeholder_image": (
                f"assets/{PLACEHOLDER_IMAGE_FILENAME}"
                if spec.privacy.placeholder_image_selectors
                else None
            ),
            "redaction_counts": redaction_counts,
        },
        "representations": {
            "source": "source.html",
            "static": "static/index.html",
            "scripted": "scripted/index.html",
        },
        "rendered_resource_expectations": resource_expectations,
        "representation_resource_counts": representation_resource_counts,
        "script_neutralization": {
            "count": len(neutralize_script_filenames),
        },
        "assets": built_assets,
        "resource_failures": resource_failures,
        "unresolved_resource_count": len(unresolved),
    }
    _write_json(safe_page_dir / "snapshot.json", snapshot)
    validation = {
        "version": 1,
        "validated_at": _utc_now(),
        "page_id": spec.id,
        "privacy_ok": (
            not residual
            and not has_contact_findings(contacts)
            and not image_selector_errors
            and source_normalization.structure_preserved
            and rendered_normalization.structure_preserved
            and not source_normalization.errors
            and not rendered_normalization.errors
        ),
        "selector_ok": not any(error.startswith(("selector", "invalid selector")) for error in errors),
        "resource_complete": not resource_failures and not unresolved,
        "replacement_counts": counts,
        "residual_field_keys": sorted(residual),
        "unlisted_contacts": contacts,
        "build_errors": errors,
        "errors": errors,
        "warnings": (["some captured or referenced resources could not be localized"] if resource_failures or unresolved else []),
        "ok": not errors,
    }
    _write_json(safe_page_dir / "validation.json", validation)
    return validation


def build_capture(
    capture_dir: str | Path,
    manifest: SnapshotManifest,
    output_root: str | Path,
    *, validation_settings: dict[str, object],
) -> dict[str, object]:
    from defipi.configuration.generation import SNAPSHOT_VALIDATION_FIELDS
    from defipi.configuration.schema import validate_config
    validate_config(validation_settings, SNAPSHOT_VALIDATION_FIELDS)
    capture_dir = Path(capture_dir).expanduser().resolve()
    output_root = Path(output_root).expanduser().resolve()
    capture_report = _read_json(capture_dir / "capture.json")
    capture_id = str(capture_report.get("capture_id") or capture_dir.name)
    if capture_report.get("status") != "ok":
        raise ValueError("Cannot build a capture containing failed pages")
    captured_ids = {
        str(item.get("page_id"))
        for item in capture_report.get("pages") or []
        if isinstance(item, dict)
    }
    manifest_ids = {page.id for page in manifest.pages}
    if captured_ids != manifest_ids:
        raise ValueError(
            f"Capture/manifest page mismatch: capture={sorted(captured_ids)}, manifest={sorted(manifest_ids)}"
        )

    safe_capture_dir = output_root / "synthetic" / capture_id
    safe_capture_dir.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, object]] = []
    for spec in manifest.pages:
        try:
            validation = _build_page(
                capture_dir / spec.id,
                safe_capture_dir / spec.id,
                spec,
                capture_id,
            )
            results.append(
                {
                    "page_id": spec.id,
                    "ok": validation["ok"],
                    "errors": validation.get("errors") or [],
                }
            )
        except Exception as exc:
            results.append({"page_id": spec.id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    failed = [item["page_id"] for item in results if not item.get("ok")]
    report = {
        "version": 1,
        "capture_id": capture_id,
        "built_at": _utc_now(),
        "status": "ok" if not failed else "error",
        "failed_page_ids": failed,
        "pages": results,
    }
    _write_json(safe_capture_dir / "build.json", report)
    from .validation import validate_capture

    validation_report = validate_capture(safe_capture_dir, browser=True, validation_settings=validation_settings)
    final_failed = sorted(set(failed) | set(validation_report["failed_page_ids"]))
    report.update(
        {
            "status": "ok" if not final_failed else "error",
            "failed_page_ids": final_failed,
            "validation": validation_report,
        }
    )
    _write_json(safe_capture_dir / "build.json", report)
    return {**report, "snapshot_dir": str(safe_capture_dir)}
