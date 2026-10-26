"""Curate and validate defense-free scripted academic-page artifacts."""

from __future__ import annotations

import functools
import json
import re
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from ..synthetic_personas import SyntheticPersona
from .layout import require_academic_sidebar_layout


CURATION_POLICY = "academic-curated-scripted-base-v3"
_REMOTE_SCHEMES = {"http", "https", "//"}
_RESOURCE_ATTRIBUTES = {
    "script": ("src",),
    "link": ("href",),
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
}
_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<![\w/])(?:\+\d{1,3}[\s().-]*)?(?:\(?\d{2,4}\)?[\s.-]){2,4}\d{2,4}(?![\w/])"
)
_FORBIDDEN_ARTIFACT_TOKENS = (
    "piiguard",
    "defense_fragment",
    "injection_position",
    "attack_instruction",
    "ignore previous instructions",
    "system directive",
    "[instruction]",
)
_SECTION_IDS = (
    "academic-position",
    "about",
    "research-interests",
    "prospective-students",
    "contact",
    "selected-publications",
    "projects",
    "teaching",
    "news",
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_remote(value: str) -> bool:
    stripped = str(value or "").strip()
    return stripped.startswith("//") or urlparse(stripped).scheme.lower() in {"http", "https"}


def _install_csp(soup: BeautifulSoup) -> None:
    for tag in soup.find_all("meta"):
        if str(tag.get("http-equiv") or "").lower() == "content-security-policy":
            tag.decompose()
    head = soup.head
    if head is None:
        html_tag = soup.html or soup.new_tag("html")
        if soup.html is None:
            soup.append(html_tag)
        head = soup.new_tag("head")
        html_tag.insert(0, head)
    meta = soup.new_tag("meta")
    meta["http-equiv"] = "Content-Security-Policy"
    meta["content"] = (
        "default-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' data:; "
        "img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' data: blob:; "
        "connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; "
        "form-action 'none'; base-uri 'none'"
    )
    head.insert(0, meta)


def curate_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all("base"):
        tag.decompose()
    for tag in soup.find_all("meta"):
        http_equiv = str(tag.get("http-equiv") or "").lower()
        if http_equiv == "refresh" or str(tag.get("property") or "").lower() == "og:url":
            tag.decompose()
    for tag in soup.find_all("link"):
        rel_values = [str(item).lower() for item in (tag.get("rel") or [])]
        link_type = str(tag.get("type") or "").lower()
        if "canonical" in rel_values or (
            "alternate" in rel_values and link_type in {"application/atom+xml", "application/rss+xml"}
        ):
            tag.decompose()
    for tag in soup.find_all(["iframe", "frame", "object", "embed"]):
        tag.decompose()
    for tag in soup.find_all("script"):
        if "serviceworker" in tag.get_text("", strip=False).replace(" ", "").lower():
            tag.decompose()

    for form in soup.find_all("form"):
        form["action"] = "#"
        form["method"] = "get"
        form["aria-disabled"] = "true"
    for control in soup.find_all(["button", "input"]):
        if str(control.get("type") or "").lower() in {"submit", "image"}:
            control["disabled"] = ""

    for tag in list(soup.find_all(True)):
        for attribute in list(tag.attrs):
            if attribute.lower().startswith("data-piiguard"):
                del tag.attrs[attribute]
        if tag.name in {"script", "link"}:
            tag.attrs.pop("integrity", None)
            tag.attrs.pop("crossorigin", None)
        if tag.name == "a" and isinstance(tag.get("href"), str):
            href = str(tag.get("href")).strip()
            if _is_remote(href) or urlparse(href).scheme.lower() == "javascript":
                tag["href"] = "#"
                tag["aria-disabled"] = "true"
                tag["tabindex"] = "-1"
                tag.attrs.pop("target", None)
                tag.attrs.pop("ping", None)
        for attribute in _RESOURCE_ATTRIBUTES.get(str(tag.name), ()):
            value = tag.get(attribute)
            if not isinstance(value, str):
                continue
            if attribute == "srcset":
                candidates = []
                for candidate in value.split(","):
                    parts = candidate.strip().split()
                    if parts and not _is_remote(parts[0]):
                        candidates.append(" ".join(parts))
                if candidates:
                    tag[attribute] = ", ".join(candidates)
                else:
                    tag.attrs.pop(attribute, None)
            elif _is_remote(value):
                if tag.name in {"script", "link"}:
                    tag.decompose()
                    break
                tag.attrs.pop(attribute, None)

    _install_csp(soup)
    return str(soup)


class CuratedScriptedBuilder:
    """Package Home and Publications while preserving local template assets."""

    def package(
        self,
        source_site: str | Path,
        destination: str | Path,
        *,
        template_license: str | Path,
    ) -> Path:
        source = Path(source_site).expanduser().resolve()
        target = Path(destination).expanduser().resolve()
        if target.exists():
            raise FileExistsError(f"Curated page destination already exists: {target}")
        index_path = source / "index.html"
        if not index_path.is_file():
            raise FileNotFoundError(f"Jekyll homepage is missing: {index_path}")
        target.mkdir(parents=True)
        for dirname in ("assets", "images"):
            candidate = source / dirname
            if candidate.is_dir():
                shutil.copytree(candidate, target / dirname)
        for relative in (Path("index.html"), Path("publications/index.html")):
            source_html = source / relative
            if not source_html.is_file():
                raise FileNotFoundError(f"Jekyll page is missing: {source_html}")
            target_html = target / relative
            target_html.parent.mkdir(parents=True, exist_ok=True)
            curated = curate_html(source_html.read_text(encoding="utf-8"))
            target_html.write_text(curated, encoding="utf-8")
        license_path = Path(template_license).expanduser().resolve()
        if not license_path.is_file():
            raise FileNotFoundError(f"Template license is missing: {license_path}")
        shutil.copy2(license_path, target / "LICENSE.template.txt")
        return target


def _normalize_space(value: str) -> str:
    return " ".join(str(value or "").split())


def _local_reference_path(build_root: Path, value: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw or raw.startswith(("#", "data:", "blob:", "mailto:", "tel:")):
        return None
    parsed = urlparse(raw)
    if parsed.scheme or raw.startswith("//"):
        return None
    clean = unquote(parsed.path)
    if clean.startswith("/"):
        return build_root / clean.lstrip("/")
    return None


def _visible_phone_findings(text: str) -> set[str]:
    findings: set[str] = set()
    for match in _PHONE_RE.finditer(text):
        digits = "".join(character for character in match.group(0) if character.isdigit())
        if len(digits) >= 7 and not re.fullmatch(r"(?:19|20)\d{6}", digits):
            findings.add(digits)
    return findings


def _declared_contacts(soup: BeautifulSoup) -> tuple[set[str], set[str]]:
    emails: set[str] = set()
    phones: set[str] = set()
    for tag in soup.select('a[href^="mailto:"]'):
        value = str(tag.get("href") or "")[7:].split("?", 1)[0].strip().lower()
        if value:
            emails.add(value)
    for tag in soup.select('a[href^="tel:"]'):
        value = "".join(character for character in str(tag.get("href") or "")[4:] if character.isdigit())
        if value:
            phones.add(value)
    return emails, phones


def _dom_structure(soup: BeautifulSoup) -> str:
    tokens = []
    for tag in soup.find_all(True):
        classes = ".".join(sorted(str(item) for item in (tag.get("class") or [])))
        tokens.append(f"{tag.name}.{classes}" if classes else str(tag.name))
    return "\n".join(tokens)


def _section_projection(soup: BeautifulSoup) -> dict[str, str]:
    projection: dict[str, str] = {}
    for section_id in _SECTION_IDS:
        node = soup.select_one(f"section#{section_id}")
        if node is not None:
            projection[section_id] = _normalize_space(node.get_text(" ", strip=True))
    return projection


def _has_local_navigation_link(soup: BeautifulSoup, label: str) -> bool:
    expected = label.casefold()
    for anchor in soup.find_all("a"):
        if _normalize_space(anchor.get_text(" ", strip=True)).casefold() != expected:
            continue
        href = str(anchor.get("href") or "").strip()
        if href and href != "#" and not _is_remote(href):
            return True
    return False


def _audit_publications_static(
    build_root: Path,
    page_record: dict[str, Any],
    expected: SyntheticPersona | None,
) -> dict[str, Any]:
    errors: list[str] = []
    publication_path = build_root / str(page_record.get("publication_html_path") or "")
    if not publication_path.is_file():
        return {
            "errors": [f"missing Publications HTML file: {publication_path}"],
            "section_projection": {},
        }

    html_text = publication_path.read_text(encoding="utf-8", errors="replace")
    lowered = html_text.lower()
    for token in _FORBIDDEN_ARTIFACT_TOKENS:
        if token in lowered:
            errors.append(f"forbidden defense/instruction token remains: {token}")
    if "serviceworker" in lowered.replace(" ", ""):
        errors.append("service worker code remains in Publications HTML")

    soup = BeautifulSoup(html_text, "html.parser")
    csp = soup.find("meta", attrs={"http-equiv": re.compile("content-security-policy", re.IGNORECASE)})
    if csp is None or "connect-src 'none'" not in str(csp.get("content") or ""):
        errors.append("offline content-security-policy is missing")
    if not soup.select_one("h1"):
        errors.append("Publications page has no h1")
    if not soup.select_one("section#selected-publications"):
        errors.append("Publications section is missing")
    if not _has_local_navigation_link(soup, "Home"):
        errors.append("Publications page has no local Home navigation link")

    for tag_name, attributes in _RESOURCE_ATTRIBUTES.items():
        for tag in soup.find_all(tag_name):
            for attribute in attributes:
                value = tag.get(attribute)
                if not isinstance(value, str):
                    continue
                candidates = (
                    [item.strip().split()[0] for item in value.split(",")]
                    if attribute == "srcset"
                    else [value]
                )
                for candidate in candidates:
                    if _is_remote(candidate):
                        errors.append(f"remote subresource remains: {candidate}")
                    local_path = _local_reference_path(build_root, candidate)
                    if local_path is not None and not local_path.is_file():
                        errors.append(f"local subresource is missing: {candidate}")
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "")
        if _is_remote(href):
            errors.append(f"navigable external link remains: {href}")

    visible_text = _normalize_space(soup.get_text(" ", strip=True))
    if expected is not None:
        for item in expected.publications:
            for value in (item.title, item.authors, item.venue, item.year):
                if value and _normalize_space(value) not in visible_text:
                    errors.append(f"expected publication content is not visible: {value!r}")
        if not expected.publications and "No publication information is available" not in visible_text:
            errors.append("empty Publications state is not visible")

    return {
        "errors": sorted(set(errors)),
        "section_projection": _section_projection(soup),
    }


def _audit_page_static(
    build_root: Path,
    page_record: dict[str, Any],
    expected: SyntheticPersona | None,
) -> dict[str, Any]:
    errors: list[str] = []
    html_path = build_root / str(page_record.get("html_path") or "")
    if not html_path.is_file():
        return {"ok": False, "errors": [f"missing HTML file: {html_path}"], "dom_structure": None}
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    lowered = html_text.lower()
    for token in _FORBIDDEN_ARTIFACT_TOKENS:
        if token in lowered:
            errors.append(f"forbidden defense/instruction token remains: {token}")
    if "serviceworker" in lowered.replace(" ", ""):
        errors.append("service worker code remains in homepage HTML")
    soup = BeautifulSoup(html_text, "html.parser")
    csp = soup.find("meta", attrs={"http-equiv": re.compile("content-security-policy", re.IGNORECASE)})
    if csp is None or "connect-src 'none'" not in str(csp.get("content") or ""):
        errors.append("offline content-security-policy is missing")
    if not soup.select_one("h1"):
        errors.append("homepage has no h1")
    if not soup.select_one("section#contact"):
        errors.append("homepage contact section is missing")
    if page_record.get("template_backend") == "academic_pages":
        try:
            require_academic_sidebar_layout(html_text)
        except ValueError as exc:
            errors.append(str(exc))

    for tag_name, attributes in _RESOURCE_ATTRIBUTES.items():
        for tag in soup.find_all(tag_name):
            for attribute in attributes:
                value = tag.get(attribute)
                if not isinstance(value, str):
                    continue
                candidates = [item.strip().split()[0] for item in value.split(",")] if attribute == "srcset" else [value]
                for candidate in candidates:
                    if _is_remote(candidate):
                        errors.append(f"remote subresource remains: {candidate}")
                    local_path = _local_reference_path(build_root, candidate)
                    if local_path is not None and not local_path.is_file():
                        errors.append(f"local subresource is missing: {candidate}")
    for anchor in soup.find_all("a"):
        href = str(anchor.get("href") or "")
        if _is_remote(href):
            errors.append(f"navigable external link remains: {href}")
    for form in soup.find_all("form"):
        if str(form.get("action") or "") != "#" or str(form.get("aria-disabled") or "") != "true":
            errors.append("enabled form remains in curated homepage")

    visible_text = _normalize_space(soup.get_text(" ", strip=True))
    declared_emails, declared_phones = _declared_contacts(soup)
    found_emails = {item.lower() for item in _EMAIL_RE.findall(visible_text + " " + html_text)}
    unknown_emails = sorted(found_emails - declared_emails)
    if unknown_emails:
        errors.append("undeclared email contacts remain: " + ", ".join(unknown_emails))
    unknown_phones = sorted(_visible_phone_findings(visible_text) - declared_phones)
    if unknown_phones:
        errors.append("undeclared phone contacts remain: " + ", ".join(unknown_phones))

    if expected is not None:
        for value in expected.expected_home_visible_strings():
            if _normalize_space(value) not in visible_text:
                errors.append(f"expected synthetic content is not visible: {value!r}")
        if declared_emails != {expected.email.lower()}:
            errors.append("declared email does not match the synthetic persona")
        expected_phone = "".join(character for character in expected.phone if character.isdigit())
        if declared_phones != {expected_phone}:
            errors.append("declared phone does not match the synthetic persona")

    if not _has_local_navigation_link(soup, "Publications"):
        errors.append("homepage has no local Publications navigation link")

    publications = _audit_publications_static(build_root, page_record, expected)
    errors.extend(f"Publications: {error}" for error in publications["errors"])

    for asset in html_path.parent.rglob("*.js"):
        if "serviceworker" in asset.read_text(encoding="utf-8", errors="ignore").replace(" ", "").lower():
            errors.append(f"service worker code remains in local script: {asset.relative_to(build_root)}")
    for asset in html_path.parent.rglob("*.css"):
        css = asset.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"(?:@import\s+|url\(\s*['\"]?)https?://", css, re.IGNORECASE):
            errors.append(f"remote CSS dependency remains: {asset.relative_to(build_root)}")

    return {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "dom_structure": _dom_structure(soup),
        "section_projection": _section_projection(soup),
        "publication_section_projection": publications["section_projection"],
        "declared_emails": sorted(declared_emails),
        "declared_phones": sorted(declared_phones),
        "stylesheet_count": len(soup.select('link[rel~="stylesheet"]')),
        "script_count": len(soup.select("script[src]")),
    }


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _browser_audit(build_root: Path, page_records: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required for browser validation") from exc

    handler = functools.partial(_QuietHandler, directory=str(build_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    origin = f"http://{host}:{port}"
    results: dict[str, dict[str, Any]] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, timeout=settings["browser_launch_timeout_ms"])
            try:
                for record in page_records:
                    page_id = str(record["page_id"])
                    page = browser.new_page(viewport=settings["desktop_viewport"])
                    page.set_default_timeout(settings["action_timeout_ms"])
                    external_requests: list[str] = []
                    page_errors: list[str] = []
                    console_errors: list[str] = []

                    def route_handler(route: Any) -> None:
                        parsed = urlparse(route.request.url)
                        if parsed.hostname not in {host, "127.0.0.1", "localhost"}:
                            external_requests.append(route.request.url)
                            route.abort()
                        else:
                            route.continue_()

                    page.route("**/*", route_handler)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on(
                        "console",
                        lambda message: console_errors.append(message.text)
                        if message.type == "error"
                        else None,
                    )
                    url = origin + "/" + str(record["html_path"]).replace("index.html", "")
                    response = page.goto(url, wait_until="networkidle", timeout=settings["navigation_timeout_ms"])
                    visible = page.locator("h1").first.is_visible() and page.locator("section#contact").is_visible()
                    stylesheets = page.evaluate("document.styleSheets.length")
                    body_height = page.evaluate("document.body.scrollHeight")
                    theme_toggle_ok = None
                    if str(record.get("template_backend")) == "al_folio":
                        toggle = page.locator("#light-toggle")
                        if toggle.count() == 1 and toggle.is_visible():
                            before_theme = page.evaluate(
                                "document.documentElement.getAttribute('data-theme-setting')"
                            )
                            toggle.click()
                            page.wait_for_timeout(settings["theme_wait_ms"])
                            after_theme = page.evaluate(
                                "document.documentElement.getAttribute('data-theme-setting')"
                            )
                            theme_toggle_ok = before_theme != after_theme
                        else:
                            theme_toggle_ok = False
                    page.set_viewport_size(settings["mobile_viewport"])
                    page.wait_for_timeout(settings["viewport_wait_ms"])
                    mobile_visible = (
                        page.locator("h1").first.is_visible()
                        and page.locator("section#contact").is_visible()
                    )
                    horizontal_overflow = page.evaluate(
                        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
                    )
                    status = response.status if response is not None else None
                    publication_url = origin + "/" + str(
                        record["publication_html_path"]
                    ).replace("index.html", "")
                    page.set_viewport_size(settings["desktop_viewport"])
                    publication_response = page.goto(
                        publication_url,
                        wait_until="networkidle",
                        timeout=settings["navigation_timeout_ms"],
                    )
                    publication_visible = (
                        page.locator("h1").first.is_visible()
                        and page.locator("section#selected-publications").is_visible()
                    )
                    publication_stylesheets = page.evaluate("document.styleSheets.length")
                    page.set_viewport_size(settings["mobile_viewport"])
                    page.wait_for_timeout(settings["viewport_wait_ms"])
                    publication_mobile_visible = (
                        page.locator("h1").first.is_visible()
                        and page.locator("section#selected-publications").is_visible()
                    )
                    publication_horizontal_overflow = page.evaluate(
                        "document.documentElement.scrollWidth > "
                        "document.documentElement.clientWidth + 2"
                    )
                    publication_status = (
                        publication_response.status if publication_response is not None else None
                    )
                    errors = []
                    if status != 200:
                        errors.append(f"browser returned HTTP status {status}")
                    if not visible:
                        errors.append("required homepage content is not visible")
                    if int(stylesheets or 0) < 1:
                        errors.append("no stylesheet loaded in browser")
                    if int(body_height or 0) < 400:
                        errors.append("rendered homepage is unexpectedly short")
                    if not mobile_visible:
                        errors.append("required homepage content is not visible at mobile width")
                    if horizontal_overflow:
                        errors.append("homepage has horizontal overflow at mobile width")
                    if publication_status != 200:
                        errors.append(
                            f"Publications browser returned HTTP status {publication_status}"
                        )
                    if not publication_visible:
                        errors.append("required Publications content is not visible")
                    if int(publication_stylesheets or 0) < 1:
                        errors.append("no stylesheet loaded on Publications page")
                    if not publication_mobile_visible:
                        errors.append(
                            "required Publications content is not visible at mobile width"
                        )
                    if publication_horizontal_overflow:
                        errors.append("Publications page has horizontal overflow at mobile width")
                    if theme_toggle_ok is False:
                        errors.append("al-folio dark-mode toggle is not functional")
                    if external_requests:
                        errors.append("external browser requests were attempted")
                    if page_errors:
                        errors.append("browser page errors occurred")
                    if console_errors:
                        errors.append("browser console errors occurred")
                    results[page_id] = {
                        "ok": not errors,
                        "errors": errors,
                        "external_request_count": len(external_requests),
                        "page_error_count": len(page_errors),
                        "console_error_count": len(console_errors),
                        "visible": bool(visible),
                        "mobile_visible": bool(mobile_visible),
                        "horizontal_overflow": bool(horizontal_overflow),
                        "publication_visible": bool(publication_visible),
                        "publication_mobile_visible": bool(publication_mobile_visible),
                        "publication_horizontal_overflow": bool(
                            publication_horizontal_overflow
                        ),
                        "theme_toggle_ok": theme_toggle_ok,
                        "stylesheet_count": int(stylesheets or 0),
                        "publication_stylesheet_count": int(publication_stylesheets or 0),
                    }
                    page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return results


def audit_build(
    build_dir: str | Path,
    page_records: Iterable[dict[str, Any]],
    *,
    personas: dict[str, SyntheticPersona] | None = None,
    browser: bool,
    validation_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(build_dir).expanduser().resolve()
    records = [dict(item) for item in page_records]
    from defipi.configuration.generation import ACADEMIC_VALIDATION_FIELDS
    from defipi.configuration.schema import validate_config
    if type(browser) is not bool:
        raise ValueError("browser must be bool")
    if browser:
        validate_config(validation_settings, ACADEMIC_VALIDATION_FIELDS)
    elif validation_settings is not None:
        raise ValueError("validation_settings is inactive when browser is false")
    browser_results = _browser_audit(root, records, validation_settings) if browser else {}
    page_reports = []
    errors: list[str] = []
    by_persona: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        persona_id = str(record.get("persona_id") or "")
        static = _audit_page_static(root, record, (personas or {}).get(persona_id))
        browser_report = browser_results.get(str(record.get("page_id")))
        page_errors = list(static.get("errors") or [])
        if browser_report:
            page_errors.extend(browser_report.get("errors") or [])
        report = {
            "page_id": record.get("page_id"),
            "persona_id": persona_id,
            "template_backend": record.get("template_backend"),
            "ok": not page_errors,
            "errors": sorted(set(page_errors)),
            "static_errors": sorted(set(static.get("errors") or [])),
            "dom_structure": static.get("dom_structure"),
            "section_projection": static.get("section_projection"),
            "publication_section_projection": static.get(
                "publication_section_projection"
            ),
            "declared_emails": static.get("declared_emails"),
            "declared_phones": static.get("declared_phones"),
            "browser": browser_report,
        }
        page_reports.append(report)
        by_persona.setdefault(persona_id, []).append(report)
        errors.extend(f"{record.get('page_id')}: {error}" for error in report["errors"])

    for persona_id, reports in by_persona.items():
        backends = {str(item.get("template_backend")) for item in reports}
        if backends != {"academic_pages", "al_folio"} or len(reports) != 2:
            errors.append(f"{persona_id}: expected exactly one page from each backend")
            continue
        projections = [item.get("section_projection") for item in reports]
        if projections[0] != projections[1]:
            errors.append(f"{persona_id}: shared homepage content differs between backends")
        publication_projections = [
            item.get("publication_section_projection") for item in reports
        ]
        if publication_projections[0] != publication_projections[1]:
            errors.append(
                f"{persona_id}: shared Publications content differs between backends"
            )
        structures = {item.get("dom_structure") for item in reports}
        if len(structures) != 2:
            errors.append(f"{persona_id}: backend DOM structures are not distinct")
        contacts = [
            (item.get("declared_emails"), item.get("declared_phones"))
            for item in reports
        ]
        if contacts[0] != contacts[1]:
            errors.append(f"{persona_id}: declared contacts differ between backends")

    return {
        "version": 1,
        "policy": CURATION_POLICY,
        "ok": not errors,
        "errors": sorted(set(errors)),
        "page_count": len(page_reports),
        "pages": sorted(page_reports, key=lambda item: str(item.get("page_id"))),
        "browser_validation": browser,
    }


def finalize_build_validation(
    build_dir: str | Path,
    manifest: dict[str, Any],
    *,
    personas: dict[str, SyntheticPersona],
    browser: bool,
    validation_settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(build_dir).expanduser().resolve()
    report = audit_build(root, manifest.get("pages") or [], personas=personas, browser=browser, validation_settings=validation_settings)
    if not report["ok"]:
        raise ValueError("Academic page validation failed: " + "; ".join(report["errors"]))
    report_by_id = {str(item["page_id"]): item for item in report["pages"]}
    pages = []
    for page in manifest.get("pages") or []:
        page_copy = dict(page)
        pages.append(page_copy)
    finalized = {**manifest, "pages": pages}
    (root / "generation_manifest.json").write_text(
        json.dumps(finalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return finalized, report


def validate_build(
    build_dir: str | Path,
    *,
    browser: bool,
    validation_settings: dict[str, Any] | None = None,
    write_report: bool,
) -> dict[str, Any]:
    root = Path(build_dir).expanduser().resolve()
    manifest_path = root / "generation_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"generation_manifest.json is missing under {root}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = audit_build(root, manifest.get("pages") or [], browser=browser, validation_settings=validation_settings)
    report["errors"] = sorted(set(report["errors"]))
    report["ok"] = not report["errors"]
    if write_report:
        (root / "validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return report
