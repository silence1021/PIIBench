"""Integrity, privacy, and offline-browser validation for synthetic snapshots."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .manifest import FieldBinding
from .normalizers import decode_pure_mailto_payload
from .privacy import (
    has_contact_findings,
    phone_scan_text,
    scan_unlisted_contacts,
)
from .server import make_server
from defipi.jsonio import write_json as _write_json


_TEXT_SUFFIXES = {".css", ".html", ".htm", ".js", ".json", ".mjs", ".svg", ".txt", ".xml"}
_SUBRESOURCE_ATTRIBUTES = {
    "audio": ("src",),
    "embed": ("src",),
    "iframe": ("src",),
    "img": ("src", "srcset"),
    "input": ("src",),
    "link": ("href",),
    "object": ("data",),
    "script": ("src",),
    "source": ("src", "srcset"),
    "track": ("src",),
    "video": ("src", "poster"),
}
_RUNTIME_VALIDATION_ERRORS = {
    "offline browser replay validation failed",
    "external subresource URLs remain in snapshot markup",
    "navigable non-contact links remain in snapshot markup",
    "unlisted email or phone contacts remain in snapshot artifacts",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _extract_build_errors(previous: dict[str, object]) -> list[str]:
    if "build_errors" not in previous:
        raise ValueError("validation.json has no build_errors; rebuild the snapshot")
    candidates = list(previous["build_errors"])
    return [
        str(error) for error in candidates if str(error) not in _RUNTIME_VALIDATION_ERRORS
    ]


def _synthetic_fields(snapshot: dict[str, object]) -> tuple[FieldBinding, ...]:
    fields: list[FieldBinding] = []
    raw_fields = snapshot.get("synthetic_fields") or []
    if not isinstance(raw_fields, list):
        raise ValueError("snapshot.synthetic_fields must be a list")
    for index, raw in enumerate(raw_fields):
        if not isinstance(raw, dict):
            raise ValueError(f"snapshot synthetic field {index} must be an object")
        kind = str(raw.get("kind") or "")
        synthetic = str(raw.get("synthetic") or "")
        selectors = tuple(str(item) for item in raw.get("selectors") or [])
        fields.append(
            FieldBinding(
                kind=kind,
                original=f"__unavailable_original_{index}__",
                synthetic=synthetic,
                selectors=selectors,
            )
        )
    return tuple(fields)


def _text_artifacts(page_dir: Path) -> list[Path]:
    paths = [page_dir / "source.html", page_dir / "static" / "index.html", page_dir / "scripted" / "index.html"]
    asset_dir = page_dir / "assets"
    if asset_dir.exists():
        paths.extend(path for path in asset_dir.iterdir() if path.suffix.lower() in _TEXT_SUFFIXES)
    return [path for path in paths if path.exists()]


def _external_markup_resources(page_dir: Path) -> list[str]:
    findings: set[str] = set()
    for relative in ("source.html", "static/index.html", "scripted/index.html"):
        path = page_dir / relative
        if not path.exists():
            continue
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        for tag_name, attributes in _SUBRESOURCE_ATTRIBUTES.items():
            for tag in soup.find_all(tag_name):
                for attribute in attributes:
                    raw_value = tag.get(attribute)
                    if not isinstance(raw_value, str):
                        continue
                    candidates = [part.strip().split()[0] for part in raw_value.split(",") if part.strip()]
                    for candidate in candidates:
                        if urlparse(candidate).scheme.lower() in {"http", "https"}:
                            findings.add(candidate)
    return sorted(findings)


def _non_placeholder_navigation_links(page_dir: Path) -> list[str]:
    findings: set[str] = set()
    for relative in ("source.html", "static/index.html", "scripted/index.html"):
        path = page_dir / relative
        if not path.exists():
            continue
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        for tag in soup.find_all("a"):
            href = tag.get("href")
            if not isinstance(href, str):
                continue
            value = href.strip()
            if value and not value.startswith(("#", "mailto:", "tel:")):
                findings.add(value)
    return sorted(findings)


def _placeholder_errors(page_dir: Path, privacy: dict[str, object]) -> list[str]:
    selectors = tuple(str(item) for item in privacy.get("placeholder_image_selectors") or [])
    placeholder = privacy.get("placeholder_image")
    if not selectors:
        return []
    if not isinstance(placeholder, str) or not placeholder:
        return ["snapshot privacy policy is missing its placeholder image path"]
    placeholder_filename = Path(placeholder).name
    errors: list[str] = []
    if not (page_dir / placeholder).is_file():
        errors.append("placeholder image asset is missing")

    def points_to_placeholder(tag: object) -> bool:
        for attribute in ("src", "poster", "data-src", "data-original"):
            value = tag.get(attribute)
            if isinstance(value, str) and not value.endswith(placeholder_filename):
                return False
        for attribute in ("srcset", "data-srcset"):
            value = tag.get(attribute)
            if not isinstance(value, str):
                continue
            candidates = [part.strip().split()[0] for part in value.split(",") if part.strip()]
            if any(not candidate.endswith(placeholder_filename) for candidate in candidates):
                return False
        return True

    for variant in ("static", "scripted"):
        soup = BeautifulSoup(
            (page_dir / variant / "index.html").read_text(encoding="utf-8"),
            "html.parser",
        )
        for selector in selectors:
            try:
                matches = soup.select(selector)
            except Exception as exc:
                errors.append(f"invalid placeholder selector in {variant}: {selector!r} ({type(exc).__name__})")
                continue
            if not matches:
                errors.append(f"placeholder selector did not match in {variant}: {selector!r}")
                continue
            for tag in matches:
                if tag.name != "img" or not points_to_placeholder(tag):
                    errors.append(f"placeholder selector is not bound to the local image in {variant}: {selector!r}")
                if tag.parent is not None and getattr(tag.parent, "name", None) == "picture":
                    for source in tag.parent.find_all("source"):
                        if not points_to_placeholder(source):
                            errors.append(
                                f"picture source does not use the local placeholder in {variant}: {selector!r}"
                            )
    return errors


def _browser_validate(
    page_dir: Path,
    fields: tuple[FieldBinding, ...],
    privacy: dict[str, object],
    resource_expectations: dict[str, object],
    settings: dict[str, object],
) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Browser validation requires Playwright. Install requirements-rendering.txt and run "
            "`python -m playwright install chromium`."
        ) from exc

    server = make_server(page_dir, host="127.0.0.1", port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = server.server_address[:2]
    origin = f"http://{host}:{port}"
    variants: dict[str, object] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, timeout=settings["browser_launch_timeout_ms"])
            try:
                for variant in ("static", "scripted"):
                    context = browser.new_context(service_workers="block", viewport=settings["viewport"])
                    context.set_default_timeout(settings["action_timeout_ms"])
                    external_attempts: list[str] = []
                    page_errors: list[str] = []

                    def route_request(route: object) -> None:
                        request_url = route.request.url
                        if request_url.startswith(origin + "/"):
                            route.continue_()
                        else:
                            external_attempts.append(request_url)
                            route.abort()

                    context.route("**/*", route_request)
                    page = context.new_page()
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    response = page.goto(
                        f"{origin}/{variant}/index.html",
                        wait_until="domcontentloaded",
                        timeout=settings["navigation_timeout_ms"],
                    )
                    page.wait_for_timeout(settings["wait_after_navigation_ms"])
                    selector_errors: list[str] = []
                    for field in fields:
                        for selector in field.selectors:
                            locator = page.locator(selector)
                            count = locator.count()
                            if count == 0:
                                selector_errors.append(f"{selector!r} did not match")
                                continue
                            outer_html = locator.evaluate_all("elements => elements.map(element => element.outerHTML)")
                            contains_synthetic = any(
                                field.synthetic in str(fragment) for fragment in outer_html
                            )
                            if not contains_synthetic and field.kind == "email":
                                for fragment in outer_html:
                                    parsed_fragment = BeautifulSoup(str(fragment), "html.parser")
                                    encoded = parsed_fragment.find(attrs={"data-md5": True})
                                    if encoded is None:
                                        continue
                                    decoded = decode_pure_mailto_payload(
                                        str(encoded.get("data-md5") or "")
                                    )
                                    if decoded and decoded.casefold() == field.synthetic.casefold():
                                        contains_synthetic = True
                                        break
                            if not contains_synthetic:
                                selector_errors.append(
                                    f"{selector!r} does not contain synthetic {field.kind}"
                                )
                            visible_matches = locator.evaluate_all(
                                """elements => elements.map(element => {
                                    const style = getComputedStyle(element);
                                    const rect = element.getBoundingClientRect();
                                    return style.display !== 'none'
                                        && style.visibility !== 'hidden'
                                        && Number(style.opacity || 1) > 0
                                        && rect.width > 0
                                        && rect.height > 0;
                                })"""
                            )
                            if not any(bool(value) for value in visible_matches):
                                selector_errors.append(f"{selector!r} is not visibly rendered")
                    navigation_link_count = (
                        page.locator(
                            "a[href]:not([href='']):not([href^='#']):not([href^='mailto:']):not([href^='tel:'])"
                        ).count()
                        if privacy.get("neutralize_external_links")
                        else 0
                    )
                    placeholder_selector_errors: list[str] = []
                    placeholder_filename = Path(str(privacy.get("placeholder_image") or "")).name
                    for selector in privacy.get("placeholder_image_selectors") or []:
                        locator = page.locator(str(selector))
                        if locator.count() == 0:
                            placeholder_selector_errors.append(f"{selector!r} did not match")
                            continue
                        sources = locator.evaluate_all(
                            "elements => elements.map(element => element.currentSrc || element.getAttribute('src') || '')"
                        )
                        if not placeholder_filename or not all(
                            str(source).endswith(placeholder_filename) for source in sources
                        ):
                            placeholder_selector_errors.append(
                                f"{selector!r} does not use the local placeholder"
                            )
                    resource_state = page.evaluate(
                        """() => {
                            const links = Array.from(document.querySelectorAll('link[rel~="stylesheet"][href]'));
                            const images = Array.from(document.querySelectorAll('img[src]'));
                            return {
                                stylesheet_link_count: links.length,
                                loaded_stylesheet_count: links.filter(link => link.sheet !== null).length,
                                image_count: images.length,
                                broken_image_count: images.filter(image => image.complete && image.naturalWidth === 0).length,
                                pending_image_count: images.filter(image => !image.complete).length,
                                script_count: document.querySelectorAll('script[src]').length,
                            };
                        }"""
                    )
                    resource_errors: list[str] = []
                    expected_stylesheets = int(resource_expectations.get("stylesheets") or 0)
                    expected_images = int(resource_expectations.get("images") or 0)
                    expected_scripts = (
                        int(resource_expectations.get("scripts") or 0)
                        if variant == "scripted"
                        else 0
                    )
                    if int(resource_state["stylesheet_link_count"]) < expected_stylesheets:
                        resource_errors.append("localized stylesheet links are missing")
                    if int(resource_state["loaded_stylesheet_count"]) < int(
                        resource_state["stylesheet_link_count"]
                    ):
                        resource_errors.append("one or more local stylesheets failed to load")
                    if int(resource_state["image_count"]) < expected_images:
                        resource_errors.append("localized image elements are missing")
                    if int(resource_state["broken_image_count"]):
                        resource_errors.append("one or more local images failed to load")
                    if int(resource_state["pending_image_count"]):
                        resource_errors.append("one or more local images did not finish loading")
                    if int(resource_state["script_count"]) < expected_scripts:
                        resource_errors.append("localized scripts are missing")
                    maximum_script_count = max(
                        expected_scripts * 4, expected_scripts + 16
                    )
                    if int(resource_state["script_count"]) > maximum_script_count:
                        resource_errors.append(
                            "runtime script expansion exceeded the replay safety limit"
                        )
                    variants[variant] = {
                        "loaded": response is not None and response.ok,
                        "status": response.status if response is not None else None,
                        "external_attempt_count": len(set(external_attempts)),
                        "selector_errors": selector_errors,
                        "navigation_link_count": navigation_link_count,
                        "placeholder_selector_errors": placeholder_selector_errors,
                        "page_error_count": len(page_errors),
                        "resource_state": resource_state,
                        "resource_errors": resource_errors,
                    }
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    ok = all(
        result.get("loaded")
        and not result.get("external_attempt_count")
        and not result.get("selector_errors")
        and not result.get("navigation_link_count")
        and not result.get("placeholder_selector_errors")
        and not result.get("resource_errors")
        for result in variants.values()
        if isinstance(result, dict)
    ) and len(variants) == 2
    return {"ok": ok, "variants": variants}


def validate_snapshot_page(page_dir: str | Path, *, browser: bool, validation_settings: dict[str, object] | None = None) -> dict[str, object]:
    _validate_settings(browser, validation_settings)
    page_dir = Path(page_dir).expanduser().resolve()
    snapshot = _read_json(page_dir / "snapshot.json")
    previous = _read_json(page_dir / "validation.json")
    fields = _synthetic_fields(snapshot)
    allowlist = tuple(str(item) for item in snapshot.get("contact_allowlist") or [])
    privacy = snapshot.get("privacy") or {}
    if not isinstance(privacy, dict):
        raise ValueError("snapshot.privacy must be an object")
    normalization = snapshot.get("normalization") or {}
    if not isinstance(normalization, dict):
        raise ValueError("snapshot.normalization must be an object")
    resource_expectations = snapshot.get("rendered_resource_expectations") or {}
    if not isinstance(resource_expectations, dict):
        raise ValueError("snapshot.rendered_resource_expectations must be an object")

    build_errors = _extract_build_errors(previous)
    errors = list(build_errors)
    external_resources = _external_markup_resources(page_dir)
    if external_resources:
        errors.append("external subresource URLs remain in snapshot markup")
    navigation_links = (
        _non_placeholder_navigation_links(page_dir)
        if privacy.get("neutralize_external_links")
        else []
    )
    if navigation_links:
        errors.append("navigable non-contact links remain in snapshot markup")
    placeholder_errors = _placeholder_errors(page_dir, privacy)
    errors.extend(placeholder_errors)
    normalization_errors = [str(error) for error in normalization.get("errors") or []]
    if normalization.get("structure_preserved") is not True:
        normalization_errors.append("PII normalization did not preserve the DOM structure")
    errors.extend(normalization_errors)

    placeholder_filename = Path(str(privacy.get("placeholder_image") or "")).name
    contact_text_parts: list[str] = []
    phone_text_parts: list[str] = []
    for path in _text_artifacts(page_dir):
        if path.name == placeholder_filename:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        contact_text_parts.append(text)
        filtered_phone_text = phone_scan_text(text, path.suffix)
        if filtered_phone_text:
            phone_text_parts.append(filtered_phone_text)
    contacts = scan_unlisted_contacts(
        "\n".join(contact_text_parts),
        fields,
        allowlist,
        phone_text="\n".join(phone_text_parts),
    )
    if has_contact_findings(contacts):
        errors.append("unlisted email or phone contacts remain in snapshot artifacts")

    browser_report: dict[str, object]
    if browser:
        try:
            browser_report = _browser_validate(
                page_dir, fields, privacy, resource_expectations, validation_settings
            )
        except Exception as exc:
            browser_report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not browser_report.get("ok"):
            errors.append("offline browser replay validation failed")
    else:
        browser_report = {"ok": None, "skipped": True}

    errors = list(dict.fromkeys(str(error) for error in errors))
    validation = {
        **previous,
        "validated_at": _utc_now(),
        "build_errors": build_errors,
        "external_markup_resource_count": len(external_resources),
        "navigable_link_count": len(navigation_links),
        "placeholder_errors": placeholder_errors,
        "normalization_errors": normalization_errors,
        "normalization_structure_preserved": normalization.get("structure_preserved"),
        "unlisted_contacts": contacts,
        "offline_browser": browser_report,
        "offline_ok": browser_report.get("ok"),
        "errors": errors,
        "ok": not errors and (browser_report.get("ok") is True or not browser),
    }
    _write_json(page_dir / "validation.json", validation)

    marker = page_dir / ".piibench-syntheticized"
    if validation["ok"] and browser:
        marker.write_text(
            json.dumps(
                {
                    "version": 1,
                    "capture_id": snapshot.get("capture_id"),
                    "page_id": snapshot.get("page_id"),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif marker.exists():
        marker.unlink()
    return validation


def validate_capture(snapshot_dir: str | Path, *, browser: bool, validation_settings: dict[str, object] | None = None) -> dict[str, object]:
    _validate_settings(browser, validation_settings)
    snapshot_dir = Path(snapshot_dir).expanduser().resolve()
    if (snapshot_dir / "snapshot.json").exists():
        page_dirs = [snapshot_dir]
    else:
        page_dirs = sorted(
            path for path in snapshot_dir.iterdir() if path.is_dir() and (path / "snapshot.json").exists()
        )
    if not page_dirs:
        raise ValueError(f"No snapshot pages found under {snapshot_dir}")

    pages: list[dict[str, object]] = []
    for page_dir in page_dirs:
        try:
            result = validate_snapshot_page(page_dir, browser=browser, validation_settings=validation_settings)
            pages.append(
                {
                    "page_id": page_dir.name,
                    "ok": result["ok"],
                    "errors": result.get("errors") or [],
                }
            )
        except Exception as exc:
            pages.append({"page_id": page_dir.name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    failed = [item["page_id"] for item in pages if not item.get("ok")]
    report = {
        "version": 1,
        "validated_at": _utc_now(),
        "status": "ok" if not failed else "error",
        "failed_page_ids": failed,
        "pages": pages,
    }
    if not (snapshot_dir / "snapshot.json").exists():
        _write_json(snapshot_dir / "validation_summary.json", report)
    return report


def _validate_settings(browser, settings):
    from defipi.configuration.generation import SNAPSHOT_VALIDATION_FIELDS
    from defipi.configuration.schema import validate_config
    if type(browser) is not bool:
        raise ValueError("browser must be bool")
    if browser:
        validate_config(settings, SNAPSHOT_VALIDATION_FIELDS)
    elif settings is not None:
        raise ValueError("validation_settings is inactive when browser is false")
