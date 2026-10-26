"""Browser-rendered, full-page accessibility observations for local GEPA pages."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
import json
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


EXTRACTOR_VERSION = "chromium-cdp-ax-raw-v1"
# Frozen origin: present in the formal GEPA search observations.
ORIGIN = "http://piiguard.test"


class AXTreeError(RuntimeError):
    """An observation could not be rendered; it is not a scored answer."""


def _inspect_cdp_target(session, native_tree, expression):
    """Locate a DOM target, then inspect only nodes from the captured full AX tree."""
    evaluated = session.send("Runtime.evaluate", {
        "expression": expression, "returnByValue": False,
    })
    object_id = (evaluated.get("result") or {}).get("objectId")
    if not object_id:
        return {"realized": False, "error": "target_not_found", "ax_nodes": []}
    dom = session.send("DOM.describeNode", {"objectId": object_id, "depth": -1})["node"]
    backend_ids = set()
    pending = [dom]
    while pending:
        node = pending.pop()
        if node.get("backendNodeId") is not None:
            backend_ids.add(node["backendNodeId"])
        pending.extend(node.get("children") or [])
    nodes = native_tree.get("nodes") or []
    by_id = {node["nodeId"]: node for node in nodes}
    pending = [node["nodeId"] for node in nodes
               if node.get("backendDOMNodeId") in backend_ids]
    included = set()
    while pending:
        node_id = pending.pop()
        if node_id in included or node_id not in by_id:
            continue
        included.add(node_id)
        pending.extend(by_id[node_id].get("childIds") or [])
    scoped_nodes = [node for node in nodes if node["nodeId"] in included]
    target = next((node for node in scoped_nodes
                   if node.get("backendDOMNodeId") == dom["backendNodeId"]), {})
    return {
        "realized": bool(scoped_nodes),
        "node_id": target.get("nodeId"),
        "ignored": bool(target.get("ignored", True)),
        "role": (target.get("role") or {}).get("value"),
        "name": (target.get("name") or {}).get("value"),
        "properties": {item.get("name"): (item.get("value") or {}).get("value")
                       for item in target.get("properties") or []},
        "ax_nodes": scoped_nodes,
    }


def filtered_target_inspection(source_inspection, filtered_text):
    """Trace captured node IDs in the actual filtered observation, without rendering.

    A filter may return free text. It still reaches the target unchanged, but
    node realization is unknown when the returned text has no identifiable CDP nodes.
    """
    serialized = filtered_text.split("AXTree:\n", 1)[-1]
    try:
        tree = json.loads(serialized)
        nodes = tree["nodes"]
        if not isinstance(nodes, list) or any(
                not isinstance(node, dict) or "nodeId" not in node
                or any(field in node and not isinstance(node[field], dict) for field in ("role", "name"))
                for node in nodes):
            raise ValueError("Missing CDP nodes")
        by_id = {str(node["nodeId"]): node for node in nodes}
    except (ValueError, KeyError, TypeError):
        by_id = None
    result = {}
    for key, source in source_inspection.items():
        if key.startswith("_"):
            continue
        if by_id is None:
            result[key] = {"role": None, "name": None, "realized": None}
            continue
        node = by_id.get(str(source.get("node_id")), {})
        surviving = [by_id[str(item["nodeId"])] for item in source.get("ax_nodes", [])
                     if str(item.get("nodeId")) in by_id]
        result[key] = {"role": (node.get("role") or {}).get("value"),
                       "name": (node.get("name") or {}).get("value"),
                       "realized": bool(surviving), "ax_nodes": surviving}
    return result


class ThreadedAXTreeRenderer:
    """Own the synchronous Playwright instance on one dedicated thread."""

    def __init__(self, renderer_factory=None):
        self._factory = renderer_factory or AXTreeRenderer
        self._executor = None
        self._manager = None
        self._renderer = None
        self.settings = None

    def _open(self):
        self._manager = self._factory()
        self._renderer = self._manager.__enter__()
        return copy.deepcopy(self._renderer.settings)

    def __enter__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gepa-axtree")
        try:
            self.settings = self._executor.submit(self._open).result()
        except BaseException:
            try:
                self._executor.submit(self._close, None, None, None).result()
            finally:
                self._executor.shutdown(wait=True)
                self._executor = None
            raise
        return self

    def __exit__(self, *exc):
        if self._executor is not None:
            try:
                self._executor.submit(self._close, *exc).result()
            finally:
                self._executor.shutdown(wait=True)
                self._executor = None
                self._renderer = None

    def _close(self, *exc):
        if self._renderer is not None:
            try:
                self._manager.__exit__(*exc)
            finally:
                self._renderer = None

    def render(self, html, **kwargs):
        if self._executor is None:
            raise AXTreeError("ThreadedAXTreeRenderer must be entered before rendering")
        return self._executor.submit(self._renderer.render, html, **kwargs).result()


class AXTreeRenderer:
    """One Chromium per run, with an isolated context per distinct document.

    Only the candidate document and its existing local resources are served.
    Memoization is run-local and keyed by the document and rendering settings.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._cache = {}
        self.settings = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self.settings = {
                "extractor_version": EXTRACTOR_VERSION,
                "playwright_version": version("playwright"),
                "chromium_version": self._browser.version,
                "origin": ORIGIN,
                "viewport": {"width": 1280, "height": 900},
                "device_scale_factor": 1,
                "wait_until": "load",
                "timeout_ms": 30000,
                "snapshot_mode": "native",
                "snapshot_method": "Accessibility.getFullAXTree",
                "snapshot_root": "main_frame",
                "serialization": "raw-cdp-json-v1",
                "ignored_nodes": "preserve",
                "omitted_roles": [],
                "max_chars": None,
                "max_depth": None,
                "target_inspection_snapshot_mode": "same-full-cdp-tree",
            }
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._playwright = None
                self._cache.clear()

    def render(self, html, *, html_dir=None, html_file=None, inspect_targets=None):
        if self._browser is None:
            raise AXTreeError("AXTreeRenderer must be entered before rendering")
        root = Path(html_dir).resolve() if html_dir else None
        relative = Path(html_file) if html_file else Path("index.html")
        if relative.is_absolute():
            if root is None:
                root = relative.parent
            try:
                relative = relative.resolve().relative_to(root)
            except ValueError as exc:
                raise AXTreeError("HTML source is outside html_dir") from exc
        if ".." in relative.parts:
            raise AXTreeError("HTML source escapes html_dir")
        document_url = ORIGIN + "/" + quote(relative.as_posix(), safe="/")
        key = json.dumps(
            [html, str(root), document_url, self.settings, list(inspect_targets or ())], sort_keys=True,
        )
        if key in self._cache:
            return copy.deepcopy(self._cache[key])

        context = self._browser.new_context(
            viewport=self.settings["viewport"], device_scale_factor=1,
            service_workers="block",
        )
        errors = []

        def serve(route):
            request = route.request
            try:
                if request.url == document_url and request.is_navigation_request():
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                    return
                parsed = urlsplit(request.url)
                if parsed.scheme != "http" or parsed.netloc != urlsplit(ORIGIN).netloc or root is None:
                    raise AXTreeError(f"Resource is not in the local page bundle: {request.url}")
                path = (root / unquote(parsed.path).lstrip("/")).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise AXTreeError(f"Missing local page resource: {parsed.path}")
                route.fulfill(path=str(path))
            except Exception as exc:
                errors.append(str(exc))
                route.abort()

        try:
            context.route("**/*", serve)
            page = context.new_page()
            page.on("requestfailed", lambda request: errors.append(
                f"Resource load failed: {request.url}: {request.failure}"))
            page.goto(document_url, wait_until="load", timeout=30000)
            session = context.new_cdp_session(page)
            try:
                session.send("Accessibility.enable")
                native_tree = session.send("Accessibility.getFullAXTree")
                tree = json.dumps(native_tree, ensure_ascii=False)
            finally:
                session.detach()
            title = page.title()
            target_inspection = {}
            if inspect_targets:
                session = context.new_cdp_session(page)
                try:
                    for alias in dict.fromkeys(inspect_targets):
                        spec = str(alias)
                        spec_parts = spec.split("|", 3)
                        target_alias = spec_parts[0]
                        edit_op = spec_parts[1] if len(spec_parts) > 1 else ""
                        occurrence = int(spec_parts[2]) if len(spec_parts) > 2 else 0
                        expected_inserted_text = json.loads(spec_parts[3]) if len(spec_parts) > 3 and spec_parts[3] != "-" else None
                        field = target_alias.split(".")[-1]
                        js = """({field, operation, occurrence, text}) => {
                          const section = document.querySelector('section[data-ax-edit-contact="news"], section#contact');
                          if (!section) return null;
                          if (operation === 'group_note') {
                            return [...section.querySelectorAll('.ax-edit-group-text')]
                              .filter(node => node.textContent === text)[occurrence] || null;
                          }
                          if (field === 'contact') return section;
                          if (section.dataset.axEditContact === 'news') {
                            const value = section.querySelector(`[data-ax-edit-field="${field}"]`);
                            if (!value) return null;
                            if (operation === 'insert_text') {
                              return [...value.parentElement.querySelectorAll('.ax-edit-inserted-text')]
                                .filter(node => node.textContent === text)[occurrence] || null;
                            }
                            return value.hasAttribute('role') || value.hasAttribute('aria-label') ? value : value.parentElement;
                          }
                          const wanted = field === 'address' ? ['office','address'] : field === 'email' ? ['email','e-mail'] : ['phone','telephone'];
                          const dt = [...section.querySelectorAll('dt')].find(el => wanted.includes(el.textContent.trim().toLowerCase()));
                          const dd = dt && dt.nextElementSibling && dt.nextElementSibling.tagName.toLowerCase() === 'dd' ? dt.nextElementSibling : null;
                          if (!dd) return null;
                          if (operation === 'insert_text') {
                            return [...dd.querySelectorAll('.ax-edit-inserted-text')]
                              .filter(node => node.textContent === text)[occurrence] || null;
                          }
                          if (field === 'email') return [...dd.querySelectorAll('a[href]')].find(a => a.getAttribute('href').toLowerCase().startsWith('mailto:'));
                          if (field === 'phone') return [...dd.querySelectorAll('a[href]')].find(a => a.getAttribute('href').toLowerCase().startsWith('tel:'));
                          const value = dd.querySelector('.ax-edit-address-value');
                          // A generic span used to anchor inserted text is ignored in AX.
                          // Keep target metadata attached to the source definition unless
                          // the wrapper itself has an accessible semantic/name attribute.
                          return value && (value.hasAttribute('role') || value.hasAttribute('aria-label') || value.hasAttribute('aria-labelledby')) ? value : dd;
                        }"""
                        params = {"field": field, "operation": edit_op,
                                  "occurrence": occurrence, "text": expected_inserted_text}
                        item = _inspect_cdp_target(
                            session, native_tree, f"({js})({json.dumps(params)})")
                        if edit_op == "group_contacts":
                            params["operation"] = "group_note"
                            note = _inspect_cdp_target(
                                session, native_tree, f"({js})({json.dumps(params)})")
                            item["group_text_ax_nodes"] = note["ax_nodes"]
                        target_inspection[str(alias)] = item
                finally:
                    session.detach()
            if errors:
                raise AXTreeError("; ".join(dict.fromkeys(errors)))
            result = {
                "observation_format": "axtree",
                "observation_text": f"Page title: {json.dumps(title, ensure_ascii=False)}\nAXTree:\n{tree}",
                "observation_renderer": copy.deepcopy(self.settings),
            }
            if inspect_targets:
                result["target_inspection"] = target_inspection
            self._cache[key] = result
            return copy.deepcopy(result)
        except AXTreeError:
            raise
        except Exception as exc:
            raise AXTreeError(f"AXTree extraction failed: {exc}") from exc
        finally:
            context.close()
