"""Offline equivalent of first-document AutoGuard injection."""
import json
from pathlib import Path
from .injection import inject_static

def add_arguments(parser):
    parser.add_argument("--source", required=True)

def export(pages, args):
    source = Path(args.source)
    manifest = json.loads((source / "run_manifest.json").read_text())
    if manifest["config"]["browser"]["injection_document_index"] != 1:
        raise ValueError("Static AutoGuard export requires injection_document_index == 1")
    selected = json.loads((source / "selected_candidate.json").read_text())
    if not isinstance(selected.get("text"), str) or not selected["text"]:
        raise ValueError("Selected AutoGuard candidate has no text")
    provenance = {"source_artifact": str(source)}
    if "id" in selected:
        provenance["candidate_id"] = selected["id"]
    diagnostics = source / "selection_diagnostics.json"
    if diagnostics.is_file():
        provenance["selection_diagnostics"] = json.loads(diagnostics.read_text())
    return {"pages": [{"page_id": p["page_id"], "html": inject_static(p["clean_html"], selected["text"])} for p in pages["pages"]],
            "files": {}, "defense_variant": "hidden-container-static-v1", "provenance": provenance}
