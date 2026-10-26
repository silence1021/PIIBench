"""Playwright-based capture of raw source, rendered DOM, and response assets."""

from __future__ import annotations

import uuid
import mimetypes
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from .http_capture import prepare_http_replay
from .manifest import PageSpec, SnapshotManifest
from .normalizers import decode_pure_mailto_payload
from .privacy import field_key
from .server import make_server
from defipi.jsonio import write_json as _write_json


CAPTURE_USER_AGENT = "PIIBenchSnapshotResearch/1.0"
_RESOURCE_TYPES = {
    "stylesheet",
    "script",
    "image",
    "font",
    "media",
    "xhr",
    "fetch",
    "manifest",
    "other",
}
_MIME_EXTENSIONS = {
    "application/javascript": ".js",
    "application/json": ".json",
    "application/ld+json": ".json",
    "application/manifest+json": ".json",
    "application/octet-stream": ".bin",
    "application/pdf": ".pdf",
    "application/wasm": ".wasm",
    "font/otf": ".otf",
    "font/ttf": ".ttf",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "image/svg+xml": ".svg",
    "text/css": ".css",
    "text/html": ".html",
    "text/javascript": ".js",
    "text/plain": ".txt",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _capture_id(manifest: SnapshotManifest) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex}"


def _content_type(headers: dict[str, str]) -> str:
    return str(headers.get("content-type") or "application/octet-stream").split(";", 1)[0].strip().lower()


def _asset_extension(url: str, content_type: str) -> str:
    if content_type in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[content_type]
    guessed = mimetypes.guess_extension(content_type, strict=False)
    if guessed:
        return guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    if 1 < len(suffix) <= 10 and suffix[1:].isalnum():
        return suffix
    return ".bin"


def _robots_decision(url: str, user_agent: str, timeout_seconds: float) -> dict[str, object]:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    request = Request(robots_url, headers={"User-Agent": user_agent})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code in {401, 403}:
            return {"url": robots_url, "status": exc.code, "allowed": False, "reason": "access_denied", "crawl_delay": 0.0}
        if exc.code == 404:
            return {"url": robots_url, "status": 404, "allowed": True, "reason": "not_found", "crawl_delay": 0.0}
        return {"url": robots_url, "status": exc.code, "allowed": True, "reason": "unavailable", "crawl_delay": 0.0}
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "url": robots_url,
            "status": None,
            "allowed": True,
            "reason": f"unavailable:{type(exc).__name__}",
            "crawl_delay": 0.0,
        }
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(payload.splitlines())
    crawl_delay = parser.crawl_delay(user_agent)
    if crawl_delay is None:
        crawl_delay = parser.crawl_delay("*")
    return {
        "url": robots_url,
        "status": 200,
        "allowed": bool(parser.can_fetch(user_agent, url)),
        "reason": "parsed",
        "crawl_delay": float(crawl_delay or 0.0),
    }


def _redirect_chain(request: object) -> list[str]:
    chain: list[str] = []
    current = request
    while current is not None:
        chain.append(str(getattr(current, "url", "")))
        current = getattr(current, "redirected_from", None)
    chain.reverse()
    return [item for item in chain if item]


def _selector_audit(page: object, spec: PageSpec) -> tuple[list[dict[str, object]], list[str]]:
    findings: list[dict[str, object]] = []
    errors: list[str] = []
    for index, field in enumerate(spec.fields):
        key = field_key(field, index)
        for selector in field.selectors:
            try:
                locator = page.locator(selector)
                count = locator.count()
                outer_html = locator.evaluate_all("elements => elements.map(element => element.outerHTML)") if count else []
            except Exception as exc:  # Playwright selector errors are page-specific data failures.
                findings.append({"field": key, "selector": selector, "count": 0, "contains_original": False})
                errors.append(f"invalid selector for {key}: {selector!r} ({type(exc).__name__})")
                continue
            casefold = field.kind == "email"
            needle = field.original.casefold() if casefold else field.original
            contains_original = any(
                needle in (str(fragment).casefold() if casefold else str(fragment))
                for fragment in outer_html
            )
            matched_encoding = None
            if not contains_original and field.kind == "email":
                for fragment in outer_html:
                    parsed = BeautifulSoup(str(fragment), "html.parser")
                    encoded = parsed.find(attrs={"data-md5": True})
                    if encoded is None:
                        continue
                    decoded = decode_pure_mailto_payload(str(encoded.get("data-md5") or ""))
                    if decoded and decoded.casefold() == field.original.casefold():
                        contains_original = True
                        matched_encoding = "pure_base64"
                        break
            findings.append(
                {
                    "field": key,
                    "selector": selector,
                    "count": count,
                    "contains_original": contains_original,
                    "matched_encoding": matched_encoding,
                }
            )
            if count == 0:
                errors.append(f"selector did not match for {key}: {selector!r}")
            elif not contains_original:
                errors.append(f"selector does not contain the declared original value for {key}: {selector!r}")
    return findings, errors


def _capture_http_first_page(
    browser: object,
    spec: PageSpec,
    page_dir: Path,
    robots: dict[str, object],
    max_resource_bytes: int,
) -> dict[str, object]:
    context = None
    server = None
    server_thread = None
    external_attempts: list[str] = []
    console_errors: list[str] = []
    try:
        acquisition = prepare_http_replay(
            source_url=spec.url,
            page_dir=page_dir,
            user_agent=CAPTURE_USER_AGENT,
            crawl_delay=float(robots.get("crawl_delay") or 0.0),
            max_resource_bytes=max_resource_bytes,
            timeout_seconds=spec.capture.http_timeout_seconds,
            max_resources=spec.capture.max_resources,
        )
        source_text = str(acquisition["source_text"])
        (page_dir / "source.html").write_text(source_text, encoding="utf-8")

        server = make_server(page_dir, host="127.0.0.1", port=0)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        host, port = server.server_address[:2]
        origin = f"http://{host}:{port}"

        context = browser.new_context(
            viewport=spec.capture.viewport,
            service_workers="block",
        )

        context.set_default_timeout(spec.capture.action_timeout_ms)

        def route_request(route: object) -> None:
            request_url = route.request.url
            if request_url.startswith(origin + "/"):
                route.continue_()
            else:
                external_attempts.append(request_url)
                route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        local_response = page.goto(
            f"{origin}/{acquisition['replay_relative_path']}",
            wait_until="domcontentloaded",
            timeout=spec.capture.navigation_timeout_ms,
        )
        if local_response is None or not local_response.ok:
            raise RuntimeError("localhost replay navigation failed")
        if spec.capture.ready_selector:
            page.wait_for_selector(spec.capture.ready_selector, state="attached", timeout=spec.capture.ready_timeout_ms)
        for selector in spec.capture.dismiss_selectors:
            locator = page.locator(selector).first
            if locator.count():
                locator.click(timeout=spec.capture.dismiss_timeout_ms)
        if spec.capture.wait_after_ready_ms:
            page.wait_for_timeout(spec.capture.wait_after_ready_ms)

        rendered_text = page.content()
        screenshot_path = page_dir / "rendered.png"
        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled")
        selector_audit, selector_errors = _selector_audit(page, spec)
        (page_dir / "rendered.html").write_text(rendered_text, encoding="utf-8")

        resources = list(acquisition.get("resources") or [])
        request_failures = [
            {
                "url": str(record.get("url") or ""),
                "error": str(record.get("error") or "http_first_resource_not_captured"),
            }
            for record in resources
            if not record.get("captured")
        ]
        request_failures.extend(
            {"url": url, "error": "blocked_during_local_replay"}
            for url in sorted(set(external_attempts))
        )
        result = {
            "page_id": spec.id,
            "status": "ok" if not selector_errors else "error",
            "captured_at": _utc_now(),
            "source_url": spec.url,
            "final_url": acquisition["final_url"],
            "status_code": acquisition["status_code"],
            "redirect_chain": acquisition["redirect_chain"],
            "viewport": spec.capture.viewport,
            "user_agent": CAPTURE_USER_AGENT,
            "acquisition_mode": "http_first",
            "render_mode": "localhost_replay",
            "robots": robots,
            "selector_audit": selector_audit,
            "resources": resources,
            "request_failures": request_failures,
            "replay_unresolved_count": len(acquisition.get("replay_unresolved") or []),
            "replay_external_attempt_count": len(set(external_attempts)),
            "console_error_count": len(console_errors),
            "errors": selector_errors,
        }
    except Exception as exc:
        result = {
            "page_id": spec.id,
            "status": "error",
            "captured_at": _utc_now(),
            "source_url": spec.url,
            "acquisition_mode": "http_first",
            "robots": robots,
            "request_failures": [
                {"url": url, "error": "blocked_during_local_replay"}
                for url in sorted(set(external_attempts))
            ],
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    finally:
        if context is not None:
            context.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
    _write_json(page_dir / "capture.json", result)
    return result


def _capture_page(browser: object, spec: PageSpec, page_dir: Path, max_resource_bytes: int) -> dict[str, object]:
    page_dir.mkdir(parents=True, exist_ok=False)
    assets_dir = page_dir / "assets"
    assets_dir.mkdir()
    (page_dir / ".piibench-raw").write_text(
        "Contains original third-party content and PII. Never pass this directory to an LLM.\n",
        encoding="utf-8",
    )

    if spec.capture.contains_visual_pii:
        result = {
            "page_id": spec.id,
            "status": "unsupported",
            "captured_at": _utc_now(),
            "source_url": spec.url,
            "errors": ["PII embedded in images or canvas is unsupported by the first snapshot builder"],
        }
        _write_json(page_dir / "capture.json", result)
        return result

    robots = _robots_decision(spec.url, CAPTURE_USER_AGENT, spec.capture.robots_timeout_seconds)
    if not robots["allowed"]:
        result = {
            "page_id": spec.id,
            "status": "error",
            "captured_at": _utc_now(),
            "source_url": spec.url,
            "robots": robots,
            "errors": ["robots.txt disallows capture"],
        }
        _write_json(page_dir / "capture.json", result)
        return result

    if spec.capture.mode == "http_first":
        return _capture_http_first_page(
            browser,
            spec,
            page_dir,
            robots,
            max_resource_bytes,
        )

    context = browser.new_context(
        viewport=spec.capture.viewport,
        user_agent=spec.capture.browser_user_agent,
        service_workers="block",
    )
    context.set_default_timeout(spec.capture.action_timeout_ms)
    page = context.new_page()
    responses: list[object] = []
    request_failures: list[dict[str, str]] = []
    console_errors: list[str] = []
    page.on("response", lambda response: responses.append(response))
    page.on(
        "requestfailed",
        lambda request: request_failures.append(
            {"url": request.url, "error": str(request.failure or "request failed")}
        ),
    )
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

    errors: list[str] = []
    try:
        main_response = page.goto(spec.url, wait_until="domcontentloaded", timeout=spec.capture.navigation_timeout_ms)
        if main_response is None:
            raise RuntimeError("navigation returned no main document response")
        if spec.capture.ready_selector:
            page.wait_for_selector(spec.capture.ready_selector, state="attached", timeout=spec.capture.ready_timeout_ms)
        for selector in spec.capture.dismiss_selectors:
            locator = page.locator(selector).first
            if locator.count():
                locator.click(timeout=spec.capture.dismiss_timeout_ms)
        if spec.capture.wait_after_ready_ms:
            page.wait_for_timeout(spec.capture.wait_after_ready_ms)

        source_text = main_response.text()
        rendered_text = page.content()
        screenshot_path = page_dir / "rendered.png"
        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled")
        final_url = page.url
        status_code = main_response.status
        redirect_chain = _redirect_chain(main_response.request)
        selector_audit, selector_errors = _selector_audit(page, spec)
        errors.extend(selector_errors)

        (page_dir / "source.html").write_text(source_text, encoding="utf-8")
        (page_dir / "rendered.html").write_text(rendered_text, encoding="utf-8")

        resources: list[dict[str, object]] = []
        seen_urls: set[str] = set()
        for response in responses:
            request = response.request
            resource_type = request.resource_type
            resource_url = response.url.split("#", 1)[0]
            if resource_type not in _RESOURCE_TYPES or resource_url in seen_urls:
                continue
            seen_urls.add(resource_url)
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            content_type = _content_type(headers)
            record: dict[str, object] = {
                "url": resource_url,
                "status": response.status,
                "resource_type": resource_type,
                "content_type": content_type,
            }
            try:
                body = response.body()
                if len(body) > max_resource_bytes:
                    raise ValueError(f"resource exceeds {max_resource_bytes} bytes")
                filename = uuid.uuid4().hex + _asset_extension(resource_url, content_type)
                target = assets_dir / filename
                if not target.exists():
                    target.write_bytes(body)
                record.update(
                    {
                        "filename": filename,
                        "byte_size": len(body),
                        "captured": True,
                    }
                )
            except Exception as exc:
                record.update({"captured": False, "error": f"{type(exc).__name__}: {exc}"})
            resources.append(record)

        result = {
            "page_id": spec.id,
            "status": "ok" if not errors else "error",
            "captured_at": _utc_now(),
            "source_url": spec.url,
            "final_url": final_url,
            "status_code": status_code,
            "redirect_chain": redirect_chain,
            "viewport": spec.capture.viewport,
            "user_agent": CAPTURE_USER_AGENT,
            "acquisition_mode": "browser_first",
            "render_mode": "origin_browser",
            "robots": robots,
            "selector_audit": selector_audit,
            "resources": resources,
            "request_failures": request_failures,
            "console_error_count": len(console_errors),
            "errors": errors,
        }
    except Exception as exc:
        result = {
            "page_id": spec.id,
            "status": "error",
            "captured_at": _utc_now(),
            "source_url": spec.url,
            "robots": robots,
            "request_failures": request_failures,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
    finally:
        context.close()

    _write_json(page_dir / "capture.json", result)
    return result


def capture_manifest(
    manifest: SnapshotManifest,
    output_root: str | Path,
    *,
    delay_seconds: float,
    max_resource_bytes: int,
    browser_launch_timeout_ms: int,
) -> dict[str, object]:
    from defipi.configuration.schema import Field, validate_config
    validate_config({"delay_seconds": delay_seconds, "max_resource_bytes": max_resource_bytes, "browser_launch_timeout_ms": browser_launch_timeout_ms}, {
        "delay_seconds": Field((int, float), minimum=0), "max_resource_bytes": Field(int, minimum=1), "browser_launch_timeout_ms": Field(int, minimum=1)})
    output_root = Path(output_root).expanduser().resolve()
    capture_id = _capture_id(manifest)
    capture_dir = output_root / "raw" / capture_id
    capture_dir.mkdir(parents=True, exist_ok=False)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Snapshot capture requires Playwright. Install requirements-rendering.txt and run "
            "`python -m playwright install chromium`."
        ) from exc

    page_results: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=browser_launch_timeout_ms)
        try:
            for index, spec in enumerate(manifest.pages):
                if index and delay_seconds:
                    time.sleep(delay_seconds)
                page_results.append(
                    _capture_page(browser, spec, capture_dir / spec.id, max_resource_bytes)
                )
        finally:
            browser.close()

    failed = [result["page_id"] for result in page_results if result.get("status") != "ok"]
    report = {
        "version": 1,
        "capture_id": capture_id,
        "created_at": _utc_now(),
        "page_count": len(page_results),
        "failed_page_ids": failed,
        "status": "ok" if not failed else "error",
        "pages": [
            {
                "page_id": result["page_id"],
                "status": result.get("status"),
                "path": result["page_id"],
            }
            for result in page_results
        ],
    }
    _write_json(capture_dir / "capture.json", report)
    return {**report, "capture_dir": str(capture_dir)}
