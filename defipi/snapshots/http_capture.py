"""HTTP-first acquisition and localhost replay preparation."""

from __future__ import annotations

import uuid
import mimetypes
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bs4 import BeautifulSoup

from .transforms import (
    extract_css_resource_urls,
    localize_html_for_replay,
    normalize_resource_url,
    rewrite_css,
    rewrite_known_urls,
)


_HTML_RESOURCE_ATTRS = {
    "audio": ("src",),
    "img": ("src", "srcset", "data-src", "data-srcset", "data-original"),
    "input": ("src",),
    "script": ("src",),
    "source": ("src", "srcset", "data-src", "data-srcset"),
    "track": ("src",),
    "video": ("src", "poster"),
}
_FETCHED_LINK_RELS = {"modulepreload", "preload", "stylesheet"}
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
_MIME_EXTENSIONS = {
    "application/javascript": ".js",
    "application/json": ".json",
    "application/manifest+json": ".json",
    "application/wasm": ".wasm",
    "font/otf": ".otf",
    "font/ttf": ".ttf",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "image/svg+xml": ".svg",
    "text/css": ".css",
    "text/html": ".html",
    "text/javascript": ".js",
}


@dataclass(frozen=True)
class HttpDocument:
    body: bytes
    status: int
    final_url: str
    content_type: str
    charset: str
    redirect_chain: tuple[str, ...]


class _RecordingRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirects: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.redirects.append(str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, user_agent: str, max_bytes: int, timeout_seconds: float) -> HttpDocument:
    redirect_handler = _RecordingRedirectHandler()
    opener = build_opener(redirect_handler)
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    with opener.open(request, timeout=timeout_seconds) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"HTTP response exceeds {max_bytes} bytes")
        final_url = str(response.geturl())
        content_type = response.headers.get_content_type().lower()
        charset = response.headers.get_content_charset() or "utf-8"
        chain = tuple([url, *redirect_handler.redirects])
        if not chain or chain[-1] != final_url:
            chain = (*chain, final_url)
        return HttpDocument(
            body=body,
            status=int(getattr(response, "status", 200)),
            final_url=final_url,
            content_type=content_type,
            charset=charset,
            redirect_chain=chain,
        )


def _origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.netloc.lower()


def _extension(url: str, content_type: str) -> str:
    if content_type in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[content_type]
    guessed = mimetypes.guess_extension(content_type, strict=False)
    if guessed:
        return guessed
    suffix = Path(urlsplit(url).path).suffix.lower()
    if 1 < len(suffix) <= 10 and suffix[1:].isalnum():
        return suffix
    return ".bin"


def _resource_type(tag_name: str, attribute: str, rels: set[str]) -> str:
    if tag_name == "script":
        return "script"
    if tag_name == "link" and "stylesheet" in rels:
        return "stylesheet"
    if tag_name in {"img", "source", "input"}:
        return "image"
    if tag_name in {"audio", "video", "track"}:
        return "media"
    return "other"


def _srcset_urls(value: str) -> list[str]:
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


def _discover_html_resources(html_text: str, base_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    discovered: dict[str, str] = {}
    for tag_name, attributes in _HTML_RESOURCE_ATTRS.items():
        for tag in soup.find_all(tag_name):
            for attribute in attributes:
                value = tag.get(attribute)
                if not isinstance(value, str):
                    continue
                values = _srcset_urls(value) if "srcset" in attribute else [value]
                for raw_url in values:
                    if raw_url.startswith(("data:", "blob:", "#")):
                        continue
                    discovered.setdefault(
                        normalize_resource_url(raw_url, base_url),
                        _resource_type(tag_name, attribute, set()),
                    )
    for tag in soup.find_all("link"):
        href = tag.get("href")
        rels = {str(value).lower() for value in tag.get("rel") or []}
        if not isinstance(href, str) or not rels.intersection(_FETCHED_LINK_RELS):
            continue
        discovered.setdefault(
            normalize_resource_url(href, base_url),
            _resource_type("link", "href", rels),
        )
    return discovered


def _is_text(content_type: str, filename: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type in _TEXT_CONTENT_TYPES
        or Path(filename).suffix.lower() in {".css", ".html", ".js", ".json", ".mjs", ".svg", ".txt", ".xml"}
    )


def prepare_http_replay(
    *,
    source_url: str,
    page_dir: Path,
    user_agent: str,
    crawl_delay: float,
    max_resource_bytes: int,
    timeout_seconds: float,
    max_resources: int,
) -> dict[str, object]:
    document = _fetch(source_url, user_agent, max_resource_bytes, timeout_seconds)
    if document.status != 200 or document.content_type != "text/html":
        raise ValueError(
            f"HTTP-first main document must be a 200 text/html response; got {document.status} {document.content_type}"
        )
    source_text = document.body.decode(document.charset, errors="replace")
    main_origin = _origin(document.final_url)
    discovered = _discover_html_resources(source_text, document.final_url)
    queue: list[tuple[str, str]] = list(discovered.items())
    queued = set(discovered)
    resources: list[dict[str, object]] = []
    raw_assets_dir = page_dir / "assets"
    replay_dir = page_dir / "replay"
    replay_assets_dir = replay_dir / "assets"
    replay_assets_dir.mkdir(parents=True)
    last_request_at = time.monotonic()

    index = 0
    while index < len(queue) and len(resources) < max_resources:
        url, resource_type = queue[index]
        index += 1
        if _origin(url) != main_origin:
            resources.append(
                {
                    "url": url,
                    "resource_type": resource_type,
                    "content_type": "application/octet-stream",
                    "captured": False,
                    "error": "cross_origin_not_fetched",
                }
            )
            continue
        remaining = crawl_delay - (time.monotonic() - last_request_at)
        if remaining > 0:
            time.sleep(remaining)
        try:
            response = _fetch(url, user_agent, max_resource_bytes, timeout_seconds)
            last_request_at = time.monotonic()
            filename = uuid.uuid4().hex + _extension(response.final_url, response.content_type)
            target = raw_assets_dir / filename
            if not target.exists():
                target.write_bytes(response.body)
            record: dict[str, object] = {
                "url": normalize_resource_url(url, document.final_url),
                "final_url": normalize_resource_url(response.final_url, response.final_url),
                "status": response.status,
                "resource_type": resource_type,
                "content_type": response.content_type,
                "filename": filename,
                "byte_size": len(response.body),
                "captured": response.status == 200,
            }
            resources.append(record)
            if response.content_type == "text/css":
                css_text = response.body.decode(response.charset, errors="replace")
                for nested_url in extract_css_resource_urls(css_text, response.final_url):
                    if nested_url not in queued:
                        queued.add(nested_url)
                        queue.append((nested_url, "font" if "/font" in nested_url else "other"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            last_request_at = time.monotonic()
            resources.append(
                {
                    "url": url,
                    "resource_type": resource_type,
                    "content_type": "application/octet-stream",
                    "captured": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    if index < len(queue):
        for url, resource_type in queue[index:]:
            resources.append(
                {
                    "url": url,
                    "resource_type": resource_type,
                    "content_type": "application/octet-stream",
                    "captured": False,
                    "error": "resource_limit_exceeded",
                }
            )

    resource_map = {
        normalize_resource_url(str(record["url"]), document.final_url): str(record["filename"])
        for record in resources
        if record.get("captured") and isinstance(record.get("filename"), str)
    }
    records_by_filename = {
        str(record["filename"]): record
        for record in resources
        if record.get("captured") and isinstance(record.get("filename"), str)
    }
    for filename, record in records_by_filename.items():
        source_path = raw_assets_dir / filename
        target_path = replay_assets_dir / filename
        if _is_text(str(record.get("content_type") or ""), filename):
            text = source_path.read_text(encoding="utf-8", errors="replace")
            if str(record.get("content_type")) == "text/css" or filename.endswith(".css"):
                text = rewrite_css(
                    text,
                    base_url=str(record.get("final_url") or record.get("url")),
                    resource_map=resource_map,
                ).text
            else:
                text = rewrite_known_urls(
                    text,
                    base_url=str(record.get("final_url") or record.get("url")),
                    resource_map=resource_map,
                )
            target_path.write_text(text, encoding="utf-8")
        else:
            shutil.copyfile(source_path, target_path)

    replay = localize_html_for_replay(
        source_text,
        document_url=document.final_url,
        resource_map=resource_map,
        asset_prefix="assets",
    )
    (replay_dir / "index.html").write_text(replay.text, encoding="utf-8")
    return {
        "source_text": source_text,
        "status_code": document.status,
        "final_url": document.final_url,
        "content_type": document.content_type,
        "redirect_chain": list(document.redirect_chain),
        "resources": resources,
        "replay_relative_path": "replay/index.html",
        "replay_unresolved": list(replay.unresolved),
    }
