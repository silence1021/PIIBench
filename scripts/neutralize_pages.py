"""Neutralize an exported PIIBench artifact for static hosting.

Usage: python scripts/neutralize_pages.py --artifact outputs/export/artifact.json
    --output outputs/neutral --base-url https://example.org

The input is preserved. The new directory contains only artifact.json and site/.
Page text, contact values and defense instructions are preserved. This tool
renames template identifiers and local paths. It uses only the Python standard
library and makes no model or network requests.

Pass the resulting artifact.json to make_eval_config; reuse the original tasks.
For URL evaluations, host only the output site/ directory at --base-url.
The supported input is the static site produced by export_artifact, including
linked Publications pages, CSS, fonts, images and the WebCloak decoder.
"""
import argparse
from html import escape, unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import secrets
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit


IDS = {"autoguard-pii-hidden-payload": "page-extra"}
ATTRS = {
    "data-autoguard-payload": "data-page-extra", "data-muzzle-pii": "data-page-note",
    "data-ax-edit-single-sidebar-layout": "data-sidebar-layout",
    "data-ax-edit-contact": "data-contact-section", "data-ax-edit-field": "data-contact-field",
    "data-wc-decoder": "data-view-script", "data-wc-field": "data-view-field",
    "data-wc-slot": "data-view-slot", "data-wc-ready": "data-view-ready",
    "data-wc-error": "data-view-error",
}
CLASSES = {
    "ax-edit-single-sidebar-wrapper": "profile-layout",
    "ax-edit-sidebar-contact": "profile-contact", "ax-edit-address-value": "contact-address",
    "ax-edit-group-text": "contact-caption", "defense-fragment": "page-aside",
    **{f"defense-fragment-{old}": f"page-aside-{new}" for old, new in (
        ("title", "title"), ("policy", "body"), ("safe-contact", "contact"),
        ("note", "note"), ("decoy", "details"), ("label", "label"),
        ("footer_notice", "footer"), ("header_notice", "header"),
        ("contact_block", "section"), ("department_contact_note", "department"),
        ("bio_tail", "bio"), ("editorial_redirect", "editorial"))},
    "page-note-camouflaged": "page-note", "wc-semantic-context": "content-note",
    "wc-structure-host": "value-container",
}
WORDS = {**ATTRS, "--ipi-camouflage": "--page-note-color"}
LAYOUT = {"academic-contact-single-sidebar-layout-v1": "single-sidebar-v1"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
ATTRIBUTE = re.compile(r'''(?P<name>[^\s=/>]+)(?:\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+)))?''')
CSS_TOKEN = re.compile(r'''/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?P<class>\.[\w-]+)|(?P<id>\#[\w-]+)|(?P<word>[\w-]+)''')
CSS_URL = re.compile(r'''url\(\s*(?:"(?P<double>[^"\n]*)"|'(?P<single>[^'\n]*)'|(?P<bare>[^)\s]*))\s*\)|@import\s+(?P<quote>["'])(?P<import>[^"']+)(?P=quote)''', re.I)
JS_TOKEN = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|(?P<quote>["'])(?P<value>(?:\\.|(?!(?P=quote))[^\\])*)(?P=quote)''')


def replace_spans(text, edits):
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def css_identifiers(text):
    def rename(match):
        token, kind = match.group(), match.lastgroup
        if kind == "class":
            return "." + CLASSES.get(token[1:], token[1:])
        if kind == "id":
            return "#" + IDS.get(token[1:], token[1:])
        return WORDS.get(token, token) if kind == "word" else token
    return CSS_TOKEN.sub(rename, text)


class Site:
    def __init__(self, root):
        self.root = root
        self.routes, self.files, self.script_owners = {}, {}, {}
        for source in sorted(root.rglob("*")):
            if not source.is_file():
                continue
            if not source.resolve().is_relative_to(root):
                raise ValueError(f"Resource is outside the site: {source}")
            relative = source.relative_to(root).as_posix()
            suffix = source.suffix.lower()
            token = secrets.token_hex(10)
            self.routes[relative] = (f"/p/{token}/" if suffix in HTML_SUFFIXES
                                     else f"/r/{token}{suffix}")
            self.files[relative] = source

    def resolve(self, value, document):
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return None, parsed
        path = unquote(urlsplit(urljoin("https://local.invalid/" + quote(document), value)).path).lstrip("/")
        source = (self.root / path).resolve()
        if not source.is_relative_to(self.root):
            raise ValueError(f"Local reference is outside the site: {value}")
        if source.is_dir():
            path = path.rstrip("/") + "/index.html" if path else "index.html"
        return path, parsed

    def url(self, value, document, *, required=False):
        path, parsed = self.resolve(value, document)
        fragment = IDS.get(parsed.fragment, parsed.fragment)
        if path is None:
            if not parsed.scheme and not parsed.netloc and parsed.fragment:
                return urlunsplit(("", "", "", parsed.query, fragment))
            return value
        if path not in self.routes:
            if required:
                raise ValueError(f"Missing local resource in {document}: {value}")
            # Preserve broken navigation as a 404 without exposing its old path.
            self.routes[path] = f"/p/{secrets.token_hex(10)}/"
        return self.routes[path] + ("?" + parsed.query if parsed.query else "") + ("#" + fragment if fragment else "")

    def css(self, text, document):
        text = css_identifiers(text)
        edits = []
        for match in CSS_URL.finditer(text):
            field = next(key for key in ("double", "single", "bare", "import") if match[key] is not None)
            edits.append((*match.span(field), self.url(match[field], document, required=True)))
        return replace_spans(text, edits)

    def javascript(self, text, document, owners=()):
        # These property accesses must follow the renamed WebCloak data attributes.
        text = text.replace("host.dataset.wcReady", "host.dataset.viewReady").replace(
            "host.dataset.wcError", "host.dataset.viewError")
        def rewrite(match):
            value = match["value"]
            if value is None:
                return match.group()
            if "\\" in value:
                return match.group()
            new = IDS.get(value, CLASSES.get(value, ATTRS.get(value, value)))
            if value == "data-wc-":
                new = "data-view-"
            elif value.startswith((".", "#", "[")):
                new = css_identifiers(new)
            # ES imports resolve against the script; fetch/DOM URLs resolve
            # against the document. Only rewrite literals that name site files.
            prefix = text[max(0, match.start() - 80):match.start()]
            is_import = re.search(r"(?:\bfrom|\bimport|\bimport\s*\()\s*$", prefix)
            contexts = [document] if is_import else list(owners) or [document]
            destinations = set()
            if re.search(r"\.[a-zA-Z0-9]{1,8}(?:[?#].*)?$", value) and not re.search(r"[\s\[\]{}]", value):
                for context in contexts:
                    path, _ = self.resolve(value, context)
                    if path in self.files:
                        destinations.add(self.url(value, context))
            if len(destinations) > 1:
                raise ValueError(f"Shared script has an ambiguous document-relative URL: {value}")
            if destinations:
                new = destinations.pop()
            return match["quote"] + new + match["quote"]
        return JS_TOKEN.sub(rewrite, text)


class Page(HTMLParser):
    def __init__(self, text, document, site):
        super().__init__(convert_charrefs=False)
        self.text, self.document, self.site = text, document, site
        self.lines = [0] + [match.end() for match in re.finditer("\n", text)]
        self.edits, self.mode = [], None
        self.feed(text)
        self.close()

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == "base":
            raise ValueError("Export the artifact without a base element before neutralizing it")
        raw, offset = self.get_starttag_text(), self.source_offset()
        attributes = dict(attrs)
        for match in ATTRIBUTE.finditer(raw, re.match(r"<\s*[^\s/>]+", raw).end()):
            name = match["name"].lower()
            if name in ATTRS:
                self.edits.append((offset + match.start("name"), offset + match.end("name"), ATTRS[name]))
            field = next((key for key in ("double", "single", "bare") if match[key] is not None), None)
            if field is None:
                continue
            value = unescape(match[field])
            new = value
            if name == "class":
                new = re.sub(r"\S+", lambda token: CLASSES.get(token.group(), token.group()), value)
            elif name in {"id", "for", "aria-labelledby", "aria-describedby", "aria-controls", "aria-owns"}:
                new = re.sub(r"\S+", lambda token: IDS.get(token.group(), token.group()), value)
            elif name in {"data-ax-edit-single-sidebar-layout", "data-sidebar-layout"}:
                new = LAYOUT.get(value, value)
            elif name == "style":
                new = self.site.css(value, self.document)
            elif name == "srcset":
                raise ValueError("Export the artifact with individual image sources before neutralizing it")
            elif name in {"href", "src", "poster", "action", "data", "xlink:href"}:
                required = name in {"src", "poster"} or (tag == "link" and attributes.get("rel") != "canonical")
                new = self.site.url(value, self.document, required=required)
                if tag == "script" and name == "src":
                    path, _ = self.site.resolve(value, self.document)
                    if path in self.site.files:
                        self.site.script_owners.setdefault(path, set()).add(self.document)
            elif name.startswith("on"):
                new = self.site.javascript(value, self.document)
            if new != value:
                encoded = escape(new, quote=True)
                if field == "bare":
                    encoded = '"' + encoded + '"'
                self.edits.append((offset + match.start(field), offset + match.end(field), encoded))
        if tag == "style":
            self.mode = "css"
        elif tag == "script":
            self.mode = "json" if attributes.get("type") == "application/ld+json" else "js"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.mode = None

    def handle_data(self, data):
        if self.mode == "css":
            new = self.site.css(data, self.document)
        elif self.mode == "js":
            new = self.site.javascript(data, self.document)
        elif self.mode == "json":
            def rewrite(value):
                if isinstance(value, dict):
                    return {key: self.site.url(item, self.document) if key in {"url", "@id"} and isinstance(item, str)
                            else rewrite(item) for key, item in value.items()}
                return [rewrite(item) for item in value] if isinstance(value, list) else value
            original = json.loads(data)
            changed = rewrite(original)
            new = json.dumps(changed, ensure_ascii=False).replace("</", "<\\/") if changed != original else data
        else:
            return
        if new != data:
            self.edits.append((self.source_offset(), self.source_offset() + len(data), new))


def neutralize(artifact, output, base_url=None):
    artifact, output = Path(artifact).resolve(), Path(output).resolve()
    manifest = json.loads(artifact.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "piibench-artifact-v1":
        raise ValueError("Expected a piibench-artifact-v1 artifact.json")
    root = (artifact.parent / manifest["site_root"]).resolve()
    if not root.is_dir():
        raise ValueError(f"Missing input site: {root}")
    if output == artifact.parent or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Output must be separate from the input artifact and site")
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    base_url = base_url if base_url is not None else manifest.get("base_url")
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("base-url must be an HTTP(S) origin without a path, query or fragment")
        base_url = base_url.rstrip("/")
    site = Site(root)
    pages, rendered = [], {}
    for item in manifest["pages"]:
        doc = item["document"]
        if doc not in site.files or site.files[doc].suffix.lower() not in HTML_SUFFIXES:
            raise ValueError(f"Missing HTML page: {doc}")
        path = site.routes[doc]
        page = {**item, "document": path.lstrip("/") + "index.html", "url_path": path.lstrip("/")}
        page.pop("url", None)
        if base_url:
            page["url"] = base_url + path
        pages.append(page)
    if not pages or len({p["page_id"] for p in pages}) != len(pages):
        raise ValueError("Expected nonempty pages with distinct page_id values")
    # Process HTML first so external scripts know their owning documents.
    for doc, source in site.files.items():
        if source.suffix.lower() in HTML_SUFFIXES:
            text = source.read_text(encoding="utf-8")
            parsed = Page(text, doc, site)
            rendered[doc] = replace_spans(text, parsed.edits).encode("utf-8")
    for doc, source in site.files.items():
        suffix = source.suffix.lower()
        if doc in rendered:
            continue
        raw = source.read_bytes()
        if suffix == ".css":
            raw = site.css(raw.decode("utf-8"), doc).encode("utf-8")
        elif suffix in {".js", ".mjs"}:
            raw = site.javascript(raw.decode("utf-8"), doc, site.script_owners.get(doc, ())).encode("utf-8")
        rendered[doc] = raw
    # Finish conversion before creating any output files.
    output.mkdir(parents=True)
    for doc, content in rendered.items():
        path = site.routes[doc].lstrip("/")
        if site.files[doc].suffix.lower() in HTML_SUFFIXES:
            path += "index.html"
        destination = output / "site" / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    result = {**manifest, "site_root": "site", "base_url": base_url, "pages": pages}
    target = output / "artifact.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifact", required=True, help="Materialized artifact.json from export_artifact")
    parser.add_argument("--output", required=True, help="New directory for artifact.json and site/")
    parser.add_argument("--base-url", help="Hosting origin, such as https://example.org")
    args = parser.parse_args(argv)
    try:
        path = neutralize(args.artifact, args.output, args.base_url)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps({"artifact": str(path)}, indent=2))


if __name__ == "__main__":
    main()
