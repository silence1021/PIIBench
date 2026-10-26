"""Portable materialized page bundles; independent of methods and evaluation."""
import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from bs4 import BeautifulSoup

# Frozen origin used by the formal GEPA search inputs.
ORIGIN = "http://piiguard.test"
ARTIFACT_SCHEMA_VERSION = "piibench-artifact-v1"
class ArtifactError(ValueError):
    pass

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def resolve_from(base, value, label):
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"{label} must be a non-empty path")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def unique(values, label):
    seen = set()
    for value in values:
        if value in seen:
            raise ArtifactError(f"Duplicate {label}: {value}")
        seen.add(value)


def safe_relative(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ArtifactError(f"{label} must remain within its resource root: {value!r}")
    return path.as_posix()


def include_site_file(relative, documents, original_documents=()):
    """Keep selected pages, their Publications and static/runtime resources."""
    path = Path(relative)
    if path.as_posix() in documents or path.suffix.lower() not in {".html", ".htm", ".xhtml"}:
        return True
    return any(path.is_relative_to(Path(document).parent / "publications")
               for document in (*documents, *original_documents))


def declared_resources(html_text, resource_root, document_path):
    """Check static HTML/CSS subresources using the snapshot URL normalizers."""
    from ..snapshots.http_capture import _discover_html_resources
    from ..snapshots.transforms import extract_css_resource_urls, normalize_resource_url

    root = Path(resource_root).resolve()
    base_url = f"{ORIGIN}/{document_path}"
    soup = BeautifulSoup(html_text, "html.parser")
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = normalize_resource_url(str(base_tag["href"]), base_url)
    pending = list(_discover_html_resources(html_text, base_url))
    for tag in soup.find_all(True):
        style = tag.get("style")
        if isinstance(style, str):
            pending.extend(extract_css_resource_urls(style, base_url))
    for style_tag in soup.find_all("style"):
        pending.extend(extract_css_resource_urls(style_tag.get_text(), base_url))
    visited = set()
    while pending:
        resource_url = pending.pop()
        if resource_url in visited:
            continue
        visited.add(resource_url)
        parsed = urlsplit(resource_url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != urlsplit(ORIGIN).netloc.lower():
            raise ArtifactError(f"Remote or unsupported page subresource cannot be frozen: {resource_url}")
        relative = Path(unquote(parsed.path).lstrip("/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ArtifactError(f"Page subresource escapes resource_root: {resource_url}")
        local = (root / relative).resolve()
        if not local.is_relative_to(root) or not local.is_file():
            raise ArtifactError(f"Missing declared page subresource: {resource_url}")
        if local.suffix.lower() == ".css":
            css = local.read_text(encoding="utf-8")
            css_url = f"{ORIGIN}/{relative.as_posix()}"
            pending.extend(extract_css_resource_urls(css, css_url))
    return sorted(Path(unquote(urlsplit(value).path).lstrip("/")).as_posix() for value in visited)

def relative_url_path(value):
    """A site-relative URL path, preserving directory trailing slashes."""
    safe_relative(value, "url_path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in value:
        raise ArtifactError("url_path must be a site-relative URL path")
    decoded = unquote(parsed.path)
    safe_relative(decoded, "url_path")
    if decoded.startswith("/"):
        raise ArtifactError("url_path must be relative to base_url")
    return value


def validate_page_url(value, label="url"):
    if (not isinstance(value, str) or any(c.isspace() for c in value)
            or urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname):
        raise ArtifactError(f"{label} requires an HTTP(S) URL")


def load_artifact(path, *, url_channel=False):
    path = Path(path).resolve()
    data = read_json(path)
    if data.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactError(f"artifact schema must be {ARTIFACT_SCHEMA_VERSION}")
    if not isinstance(data.get("benchmark_id"), str) or not data["benchmark_id"].strip():
        raise ArtifactError("benchmark_id is required")
    if data.get("scenario") not in ("academic", "news"):
        raise ArtifactError("scenario must be academic or news")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(data.get("method_id", ""))):
        raise ArtifactError("Invalid method_id")
    if not data.get("artifact_id"):
        raise ArtifactError("artifact_id is required")
    root = None if url_channel else resolve_from(path.parent, data.get("site_root"), "site_root")
    base = data.get("base_url")
    if not isinstance(data.get("pages"), list) or not data["pages"]:
        raise ArtifactError("Materialized pages are required")
    pages = []
    for item in data["pages"]:
        if not isinstance(item.get("page_id"), str) or not item["page_id"].strip():
            raise ArtifactError("Each page requires page_id")
        page = {"page_id": item["page_id"]}
        if url_channel:
            url = item.get("url")
            if url is None:
                validate_page_url(base, "base_url")
                url_path = relative_url_path(item.get("url_path", item.get("document")))
                url = urljoin(base.rstrip("/") + "/", url_path)
            validate_page_url(url)
            page["url"] = url
        else:
            doc = safe_relative(item.get("document"), "document")
            html = (root / doc).resolve()
            if not html.is_relative_to(root) or not html.is_file():
                raise ArtifactError(f"Missing resource document: {html}")
            declared_resources(html.read_text(encoding="utf-8"), root, doc)
            page.update(html_path=str(html), resource_root=str(root), resource_document_path=doc)
            if "url_path" in item:
                page["url_path"] = relative_url_path(item["url_path"])
        pages.append(page)
    unique([p["page_id"] for p in pages], "page_id")
    return {**data, "pages": pages}


def _write_artifact(output, pages, exported, *, method_id, artifact_id, base_url=None, code_revision=None):
    output = Path(output).resolve()
    if output.exists():
        raise ArtifactError(f"Artifact directory already exists: {output}")
    source = Path(pages["site_root"]).resolve()
    if output.is_relative_to(source):
        raise ArtifactError("Artifact output must be outside input site")
    index = {p["page_id"]: p for p in pages["pages"]}
    rendered = exported["pages"]
    unique([p["page_id"] for p in rendered], "page_id")
    if {p["page_id"] for p in rendered} != set(index):
        raise ArtifactError("Export must cover exactly the benchmark pages")
    manifest_pages = []
    changed_originals = set()
    for item in rendered:
        original = index[item["page_id"]]
        original_doc = safe_relative(original["document"], "document")
        doc = safe_relative(item.get("document", original_doc), "document")
        page = {"page_id": item["page_id"], "document": doc}
        if doc != original_doc:
            changed_originals.add(original_doc)
        if "url_path" in item:
            page["url_path"] = relative_url_path(item["url_path"])
        elif doc == original_doc and "url_path" in original:
            page["url_path"] = relative_url_path(original["url_path"])
        manifest_pages.append(page)
    original_documents = {page["document"] for page in pages["pages"]}
    final_documents = {page["document"] for page in manifest_pages}
    (output / "site").mkdir(parents=True)
    if not exported.get("complete_site"):
        for original in source.rglob("*"):
            relative = original.relative_to(source)
            if not original.is_file() or not include_site_file(relative, final_documents, original_documents):
                continue
            target = output / "site" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        for document in changed_originals - final_documents:
            target = output / "site" / document
            if target.is_file():
                target.unlink()
    for item, page in zip(rendered, manifest_pages):
        target = output / "site" / page["document"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if "html" in item:
            target.write_text(item["html"], encoding="utf-8")
    for relative, value in exported.get("files", {}).items():
        relative = safe_relative(relative, "file")
        if not include_site_file(relative, final_documents, original_documents):
            continue
        target = output / "site" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value if isinstance(value, bytes) else value.encode("utf-8"))
    manifest = {"schema_version": ARTIFACT_SCHEMA_VERSION,
                "benchmark_id": pages["benchmark_id"], "scenario": pages["scenario"],
                "method_id": method_id, "artifact_id": artifact_id,
                "defense_variant": exported.get("defense_variant"),
                "site_root": "site", "base_url": base_url,
                "provenance": exported.get("provenance", {}),
                "code_revision": code_revision, "pages": manifest_pages}
    path = output / "artifact.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    load_artifact(path)
    if base_url is not None:
        load_artifact(path, url_channel=True)
    return path

def write_artifact(output, pages, exported, *, method_id, artifact_id, base_url=None, code_revision=None):
    output = Path(output).resolve()
    source = Path(pages["site_root"]).resolve()
    if output.exists():
        raise ArtifactError(f"Artifact directory already exists: {output}")
    if output.is_relative_to(source):
        raise ArtifactError("Artifact output must be outside input site")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".artifact-", dir=output.parent) as temporary:
        stage = Path(temporary) / "prepared"
        _write_artifact(stage, pages, exported, method_id=method_id, artifact_id=artifact_id, base_url=base_url, code_revision=code_revision)
        stage.rename(output)
    return output / "artifact.json"
