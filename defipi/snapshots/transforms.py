"""HTML/CSS resource localization and safe replay transformations."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup


_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(
    r"(@import\s+)(?:url\(\s*)?(['\"])(.*?)\2\s*\)?", re.IGNORECASE
)
_REMOTE_SCHEMES = {"http", "https"}
_RESOURCE_ATTRS = ("src", "poster", "data-src", "data-original")
_SRCSET_ATTRS = ("srcset", "data-srcset")
_FETCHED_LINK_RELS = {
    "apple-touch-icon",
    "icon",
    "manifest",
    "mask-icon",
    "modulepreload",
    "preload",
    "shortcut",
    "stylesheet",
}

PLACEHOLDER_IMAGE_BYTES = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 320" role="img" aria-label="Profile image redacted">
<rect width="320" height="320" fill="#e5e7eb"/>
<circle cx="160" cy="118" r="58" fill="#9ca3af"/>
<path d="M48 296c10-75 54-112 112-112s102 37 112 112" fill="#9ca3af"/>
</svg>
"""
PLACEHOLDER_IMAGE_FILENAME = "placeholder.svg"


@dataclass(frozen=True)
class RewriteResult:
    text: str
    unresolved: tuple[str, ...]
    redactions: tuple[tuple[str, int], ...] = ()


def normalize_resource_url(value: str, base_url: str) -> str:
    absolute = urldefrag(urljoin(base_url, value))[0]
    parsed = urlsplit(absolute)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def _normalized_url(value: str, base_url: str) -> str:
    return normalize_resource_url(value, base_url)


def _is_remote(value: str, base_url: str) -> bool:
    return urlparse(urljoin(base_url, value)).scheme.lower() in _REMOTE_SCHEMES


def mapped_resource_filename(
    value: str,
    base_url: str,
    resource_map: dict[str, str],
    asset_aliases: dict[str, str] | None = None,
) -> str | None:
    """Resolve both original resource URLs and replay-localized asset paths.

    HTTP-first capture renders a temporary document whose URLs have already
    been rewritten to ``assets/<resource-id>``.  The saved rendered DOM keeps
    those paths, so a later privacy build must recognize them instead of
    resolving them against the original website and treating them as remote.
    """

    normalized = _normalized_url(value, base_url)
    filename = resource_map.get(normalized)
    aliases = asset_aliases or {}
    if filename is None:
        parsed = urlsplit(value)
        path = parsed.path.replace("\\", "/")
        candidate = posixpath.basename(path)
        path_parts = {part for part in path.split("/") if part}
        known_filenames = set(resource_map.values()) | set(aliases) | set(aliases.values())
        if candidate in known_filenames and "assets" in path_parts:
            filename = candidate
    if filename is None:
        return None
    return aliases.get(filename, filename)


def _mapped_path(
    value: str,
    base_url: str,
    resource_map: dict[str, str],
    prefix: str,
    asset_aliases: dict[str, str] | None = None,
) -> str | None:
    filename = mapped_resource_filename(value, base_url, resource_map, asset_aliases)
    if filename is None:
        return None
    return posixpath.join(prefix, filename) if prefix else filename


def rewrite_css(
    css_text: str,
    *,
    base_url: str,
    resource_map: dict[str, str],
    asset_prefix: str = "",
    asset_aliases: dict[str, str] | None = None,
) -> RewriteResult:
    unresolved: set[str] = set()

    def replace_url(match: re.Match[str]) -> str:
        quote_character, raw_value = match.group(1), match.group(2).strip()
        if not raw_value or raw_value.startswith(("data:", "blob:", "#")):
            return match.group(0)
        mapped = _mapped_path(
            raw_value, base_url, resource_map, asset_prefix, asset_aliases
        )
        if mapped is not None:
            quote_token = quote_character or '"'
            return f"url({quote_token}{mapped}{quote_token})"
        if _is_remote(raw_value, base_url):
            unresolved.add(_normalized_url(raw_value, base_url))
            return 'url("data:,")'
        return match.group(0)

    rewritten = _CSS_URL_RE.sub(replace_url, css_text)

    def replace_import(match: re.Match[str]) -> str:
        prefix, quote_character, raw_value = match.group(1), match.group(2), match.group(3).strip()
        mapped = _mapped_path(
            raw_value, base_url, resource_map, asset_prefix, asset_aliases
        )
        if mapped is not None:
            return f"{prefix}{quote_character}{mapped}{quote_character}"
        if _is_remote(raw_value, base_url):
            unresolved.add(_normalized_url(raw_value, base_url))
            return ""
        return match.group(0)

    rewritten = _CSS_IMPORT_RE.sub(replace_import, rewritten)
    return RewriteResult(text=rewritten, unresolved=tuple(sorted(unresolved)))


def extract_css_resource_urls(css_text: str, base_url: str) -> tuple[str, ...]:
    urls: set[str] = set()
    for match in _CSS_URL_RE.finditer(css_text):
        value = match.group(2).strip()
        if value and not value.startswith(("data:", "blob:", "#")):
            urls.add(normalize_resource_url(value, base_url))
    for match in _CSS_IMPORT_RE.finditer(css_text):
        value = match.group(3).strip()
        if value and not value.startswith(("data:", "blob:", "#")):
            urls.add(normalize_resource_url(value, base_url))
    return tuple(sorted(urls))


def rewrite_known_urls(
    text: str,
    *,
    base_url: str,
    resource_map: dict[str, str],
    asset_aliases: dict[str, str] | None = None,
) -> str:
    """Best-effort rewrite for literal URLs in JavaScript/JSON assets."""
    output = text
    base = urlparse(base_url)
    base_directory = posixpath.dirname(base.path or "/")
    replacements: list[tuple[str, str]] = []
    aliases = asset_aliases or {}
    for url, filename in resource_map.items():
        filename = aliases.get(filename, filename)
        replacements.append((url, filename))
        parsed = urlparse(url)
        if parsed.scheme == base.scheme and parsed.netloc == base.netloc:
            relative = posixpath.relpath(parsed.path, base_directory or "/")
            replacements.append((relative, filename))
    for original, replacement in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if original:
            output = output.replace(original, replacement)
    return output


def _install_csp(soup: BeautifulSoup, *, scripted: bool) -> None:
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
    script_src = "'self' 'unsafe-inline' blob:" if scripted else "'none'"
    meta["content"] = (
        "default-src 'self' data: blob:; "
        f"script-src {script_src}; "
        "style-src 'self' 'unsafe-inline' data:; "
        "img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' data: blob:; "
        "connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; "
        "form-action 'none'; base-uri 'none'"
    )
    head.insert(0, meta)


def _rewrite_srcset(
    value: str,
    base_url: str,
    resource_map: dict[str, str],
    prefix: str,
    asset_aliases: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    rewritten: list[str] = []
    unresolved: list[str] = []
    for candidate in value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        parts = candidate.split()
        raw_url = parts[0]
        mapped = _mapped_path(
            raw_url, base_url, resource_map, prefix, asset_aliases
        )
        if mapped is not None:
            parts[0] = mapped
            rewritten.append(" ".join(parts))
        elif raw_url.startswith(("data:", "blob:")):
            rewritten.append(candidate)
        elif _is_remote(raw_url, base_url):
            unresolved.append(_normalized_url(raw_url, base_url))
        else:
            rewritten.append(candidate)
    return ", ".join(rewritten), unresolved


def _placeholder_srcset(value: str, placeholder_path: str) -> str:
    candidates: list[str] = []
    for candidate in value.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        candidates.append(" ".join([placeholder_path, *parts[1:]]))
    return ", ".join(candidates)


def rewrite_html(
    html_text: str,
    *,
    document_url: str,
    resource_map: dict[str, str],
    asset_prefix: str,
    mode: str,
    neutralize_external_links: bool,
    placeholder_image_selectors: tuple[str, ...] = (),
    placeholder_image_filename: str | None = None,
    asset_aliases: dict[str, str] | None = None,
    neutralize_script_filenames: frozenset[str] = frozenset(),
) -> RewriteResult:
    if mode not in {"source", "static", "scripted"}:
        raise ValueError(f"Unsupported HTML rewrite mode: {mode}")
    soup = BeautifulSoup(html_text, "html.parser")
    unresolved: set[str] = set()
    redaction_counts: dict[str, int] = {}
    placeholder_nodes: set[int] = set()

    if placeholder_image_selectors and not placeholder_image_filename:
        raise ValueError("placeholder_image_filename is required when image selectors are configured")
    placeholder_path = (
        posixpath.join(asset_prefix, str(placeholder_image_filename))
        if placeholder_image_filename
        else None
    )
    for selector in placeholder_image_selectors:
        try:
            matches = soup.select(selector)
        except Exception as exc:
            raise ValueError(f"Invalid placeholder image selector {selector!r}: {exc}") from exc
        replaced = 0
        for tag in matches:
            if tag.name != "img":
                raise ValueError(
                    f"Placeholder image selector {selector!r} matched <{tag.name}>; it must match <img>"
                )
            if tag.parent is not None and getattr(tag.parent, "name", None) == "picture":
                for source in tag.parent.find_all("source"):
                    placeholder_nodes.add(id(source))
                    for attribute in _RESOURCE_ATTRS:
                        if source.get(attribute) is not None:
                            source[attribute] = str(placeholder_path)
                    for attribute in _SRCSET_ATTRS:
                        value = source.get(attribute)
                        if isinstance(value, str):
                            source[attribute] = _placeholder_srcset(value, str(placeholder_path))
            placeholder_nodes.add(id(tag))
            for attribute in _RESOURCE_ATTRS:
                if tag.get(attribute) is not None:
                    tag[attribute] = str(placeholder_path)
            if tag.get("src") is None:
                tag["src"] = str(placeholder_path)
            for attribute in _SRCSET_ATTRS:
                value = tag.get(attribute)
                if isinstance(value, str):
                    tag[attribute] = _placeholder_srcset(value, str(placeholder_path))
            replaced += 1
        redaction_counts[f"placeholder:{selector}"] = replaced

    for base_tag in soup.find_all("base"):
        base_tag.decompose()
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv") or "").lower() == "refresh":
            meta.decompose()
    for tag in soup.find_all(["iframe", "frame", "object", "embed"]):
        tag.decompose()

    if mode == "static":
        for tag in soup.find_all("script"):
            script_type = str(tag.get("type") or "").strip().lower()
            if script_type not in {"application/json", "application/ld+json"}:
                tag.decompose()
        for tag in soup.find_all("noscript"):
            tag.decompose()

    for form in soup.find_all("form"):
        form["action"] = "#"
        form["method"] = "get"
        form["data-form-disabled"] = "true"
    for control in soup.find_all(["button", "input"]):
        control_type = str(control.get("type") or "").lower()
        if control_type in {"submit", "image"}:
            control["disabled"] = ""

    for tag in soup.find_all(True):
        if (
            mode == "scripted"
            and tag.name == "script"
            and isinstance(tag.get("src"), str)
        ):
            script_filename = mapped_resource_filename(
                str(tag.get("src")), document_url, resource_map, asset_aliases
            )
            if script_filename in neutralize_script_filenames:
                tag.decompose()
                redaction_counts["problematic_script_loaders"] = (
                    redaction_counts.get("problematic_script_loaders", 0) + 1
                )
                continue

        if mode == "static":
            for attribute in list(tag.attrs):
                if attribute.lower().startswith("on"):
                    del tag.attrs[attribute]

        if tag.name in {"script", "link"}:
            tag.attrs.pop("integrity", None)
            tag.attrs.pop("crossorigin", None)

        if tag.name == "a" and isinstance(tag.get("href"), str):
            href = str(tag.get("href")).strip()
            scheme = urlparse(urljoin(document_url, href)).scheme.lower()
            should_redact = (
                bool(href)
                and not href.startswith("#")
                and ((neutralize_external_links and scheme in _REMOTE_SCHEMES) or scheme == "javascript")
            )
            if should_redact:
                tag["href"] = "#redacted-link"
                tag["aria-disabled"] = "true"
                tag["tabindex"] = "-1"
                tag["data-link-redacted"] = "true"
                for attribute in ("target", "ping", "download"):
                    tag.attrs.pop(attribute, None)
                for attribute in list(tag.attrs):
                    if attribute.lower().startswith("on"):
                        del tag.attrs[attribute]
                redaction_counts["external_links"] = redaction_counts.get("external_links", 0) + 1

        for attribute in _RESOURCE_ATTRS:
            raw_value = tag.get(attribute)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            if id(tag) in placeholder_nodes:
                continue
            if raw_value.startswith(("data:", "blob:", "#")):
                continue
            mapped = _mapped_path(
                raw_value,
                document_url,
                resource_map,
                asset_prefix,
                asset_aliases,
            )
            if mapped is not None:
                tag[attribute] = mapped
            elif _is_remote(raw_value, document_url):
                unresolved.add(_normalized_url(raw_value, document_url))
                del tag.attrs[attribute]

        for attribute in _SRCSET_ATTRS:
            raw_value = tag.get(attribute)
            if not isinstance(raw_value, str):
                continue
            if id(tag) in placeholder_nodes:
                continue
            rewritten, missing = _rewrite_srcset(
                raw_value,
                document_url,
                resource_map,
                asset_prefix,
                asset_aliases,
            )
            unresolved.update(missing)
            if rewritten:
                tag[attribute] = rewritten
            else:
                del tag.attrs[attribute]

        if tag.name == "link" and isinstance(tag.get("href"), str):
            raw_value = str(tag.get("href"))
            mapped = _mapped_path(
                raw_value,
                document_url,
                resource_map,
                asset_prefix,
                asset_aliases,
            )
            if mapped is not None:
                tag["href"] = mapped
            elif _is_remote(raw_value, document_url):
                unresolved.add(_normalized_url(raw_value, document_url))
                tag.decompose()
                continue

        raw_style = tag.get("style")
        if isinstance(raw_style, str):
            result = rewrite_css(
                raw_style,
                base_url=document_url,
                resource_map=resource_map,
                asset_prefix=asset_prefix,
                asset_aliases=asset_aliases,
            )
            tag["style"] = result.text
            unresolved.update(result.unresolved)

    for style_tag in soup.find_all("style"):
        result = rewrite_css(
            style_tag.get_text(),
            base_url=document_url,
            resource_map=resource_map,
            asset_prefix=asset_prefix,
            asset_aliases=asset_aliases,
        )
        style_tag.string = result.text
        unresolved.update(result.unresolved)

    _install_csp(soup, scripted=mode == "scripted")
    return RewriteResult(
        text=str(soup),
        unresolved=tuple(sorted(unresolved)),
        redactions=tuple(sorted(redaction_counts.items())),
    )


def localize_html_for_replay(
    html_text: str,
    *,
    document_url: str,
    resource_map: dict[str, str],
    asset_prefix: str = "assets",
) -> RewriteResult:
    """Rewrite only fetchable subresources for raw localhost rendering."""
    soup = BeautifulSoup(html_text, "html.parser")
    unresolved: set[str] = set()
    for base_tag in soup.find_all("base"):
        base_tag.decompose()

    for tag in list(soup.find_all(True)):
        if tag.name in {"script", "link"}:
            tag.attrs.pop("integrity", None)
            tag.attrs.pop("crossorigin", None)

        removed = False
        for attribute in _RESOURCE_ATTRS:
            raw_value = tag.get(attribute)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            if raw_value.startswith(("data:", "blob:", "#")):
                continue
            mapped = _mapped_path(raw_value, document_url, resource_map, asset_prefix)
            if mapped is not None:
                tag[attribute] = mapped
            elif _is_remote(raw_value, document_url):
                unresolved.add(_normalized_url(raw_value, document_url))
                if tag.name == "script" and attribute == "src":
                    tag.decompose()
                    removed = True
                    break
                del tag.attrs[attribute]
        if removed:
            continue

        for attribute in _SRCSET_ATTRS:
            raw_value = tag.get(attribute)
            if not isinstance(raw_value, str):
                continue
            rewritten, missing = _rewrite_srcset(
                raw_value, document_url, resource_map, asset_prefix
            )
            unresolved.update(missing)
            if rewritten:
                tag[attribute] = rewritten
            else:
                del tag.attrs[attribute]

        if tag.name == "link" and isinstance(tag.get("href"), str):
            rels = {str(value).lower() for value in tag.get("rel") or []}
            if not rels.intersection(_FETCHED_LINK_RELS):
                continue
            raw_value = str(tag.get("href"))
            mapped = _mapped_path(raw_value, document_url, resource_map, asset_prefix)
            if mapped is not None:
                tag["href"] = mapped
            elif _is_remote(raw_value, document_url):
                unresolved.add(_normalized_url(raw_value, document_url))
                tag.decompose()
                continue

        raw_style = tag.get("style")
        if isinstance(raw_style, str):
            result = rewrite_css(
                raw_style,
                base_url=document_url,
                resource_map=resource_map,
                asset_prefix=asset_prefix,
            )
            tag["style"] = result.text
            unresolved.update(result.unresolved)

    for style_tag in soup.find_all("style"):
        result = rewrite_css(
            style_tag.get_text(),
            base_url=document_url,
            resource_map=resource_map,
            asset_prefix=asset_prefix,
        )
        style_tag.string = result.text
        unresolved.update(result.unresolved)
    return RewriteResult(text=str(soup), unresolved=tuple(sorted(unresolved)))
