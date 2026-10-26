"""Offline export of the selected PIIGuard candidate."""
import json
from pathlib import Path
from defipi.methods.common.render import render_fragment_page

def add_arguments(parser):
    parser.add_argument("--source", required=True)

def export(pages, args):
    source = Path(args.source)
    if source.is_dir():
        source = source / "selected_candidate.json"
    saved = json.loads(source.read_text())
    if saved["domain"] != pages["scenario"]:
        raise ValueError("PIIGuard source scenario differs from benchmark")
    candidate = saved["candidate"]
    provenance = {"candidate_id": candidate.get("candidate_id"), "selection_split": saved.get("selection_split")}
    if not candidate or not candidate.get("defense_fragment"):
        raise ValueError("Selected candidate has no defense fragment")
    return {"pages": [{"page_id": p["page_id"], "html": render_fragment_page(p, candidate, scenario=pages["scenario"])} for p in pages["pages"]],
            "files": {}, "defense_variant": "compact_fragment_rewrite_v1/" + candidate["position"], "provenance": {"source_artifact": str(source), **provenance}}
