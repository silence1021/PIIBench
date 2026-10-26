"""Offline export of explicitly mapped, already generated final WebCloak pages."""
import json
from pathlib import Path
from defipi.artifacts.contract import include_site_file, safe_relative
from .materialize import SCHEMA, ARMS

def add_arguments(parser):
    parser.add_argument("--source", required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)

def export(pages, args):
    source = Path(args.source).resolve()
    saved = json.loads(source.read_text())
    if saved.get("schema_version") != SCHEMA:
        raise ValueError("Expected final WebCloak pages; development prepared artifacts cannot substitute final pages")
    if saved.get("scenario") != pages["scenario"] or saved.get("benchmark_id") != pages["benchmark_id"]:
        raise ValueError("WebCloak final pages belong to a different benchmark/scenario")
    if saved.get("arm") != args.arm:
        raise ValueError("Requested WebCloak arm is absent from the final pages")
    indexed = {row["page_id"]: row for row in saved["pages"]}
    if len(indexed) != len(saved["pages"]):
        raise ValueError("Duplicate WebCloak page mapping")
    root = (source.parent / safe_relative(saved["site_root"], "site_root")).resolve()
    if not root.is_relative_to(source.parent):
        raise ValueError("WebCloak site escapes the method output")
    site_subtree = safe_relative(saved["site_subtree"], "site_subtree")
    selected = (root / site_subtree).resolve()
    if not selected.is_relative_to(root) or not selected.is_dir():
        raise ValueError("Missing selected WebCloak site subtree")
    rendered = []
    for page in pages["pages"]:
        row = indexed.get(page["page_id"])
        if row is None:
            raise ValueError(f"Missing final WebCloak page: {page['page_id']}")
        document = safe_relative(row["document"], "document")
        path = (root / document).resolve()
        if not path.is_relative_to(selected) or not path.is_file():
            raise ValueError(f"Missing final WebCloak HTML: {page['page_id']}")
        rendered.append({"page_id": page["page_id"], "document": document, "html": path.read_text(), **({"url_path": row["url_path"]} if row.get("url_path") else {})})
    documents = {p["document"] for p in rendered}
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in selected.rglob("*")
             if p.is_file() and p.relative_to(root).as_posix() not in documents
             and include_site_file(p.relative_to(root), documents)}
    return {"pages": rendered, "files": files, "complete_site": True, "defense_variant": args.arm,
            "provenance": {"source_artifact": str(source), **saved.get("provenance", {})}}
