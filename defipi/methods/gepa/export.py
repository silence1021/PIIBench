"""Offline export of the selected GEPA candidate."""
import json
from pathlib import Path
from defipi.methods.common.render import render_fragment_page

def add_arguments(parser):
    parser.add_argument("--source", required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--candidate-id")
    choice.add_argument("--selection", help="selection.json produced by cli.gepa select")

def export(pages, args):
    source = Path(args.source)
    if source.is_dir():
        source = source / "output_json.json"
    saved = json.loads(source.read_text())
    if saved["seed_meta"]["fragment_profile"] != pages["scenario"]:
        raise ValueError("GEPA source scenario differs from benchmark")
    candidate_id = (json.loads(Path(args.selection).read_text())["candidate_id"]
                    if getattr(args, "selection", None) else args.candidate_id)
    selected = [c for c in saved["result"]["pool"] if str(c.get("candidate_id")) == str(candidate_id)]
    if len(selected) != 1:
        raise ValueError(f"Expected exactly one GEPA candidate: {candidate_id}")
    candidate = selected[0]
    fragment = candidate.get("defense_fragment") or {}
    if fragment.get("format") != "ax_edit_v1" or fragment.get("slot") != candidate.get("position"):
        raise ValueError("GEPA candidate requires ax_edit_v1 with slot matching position")
    best = saved["result"].get("best") or {}
    legacy_best = "candidate_id" in best
    position = candidate["position"]
    if legacy_best:
        # Historical outputs keep their original single best as evidence. Their
        # saved position diagnostics can describe this explicit export choice.
        position_best = ((saved.get("top_candidates_by_position") or {}).get(position) or {}).get("candidate") or {}
    else:
        position_best = best.get(position) or {}
    position_best_id = position_best.get("candidate_id")
    provenance = {"run_id": saved.get("seed_meta", {}).get("run_id"),
                  "run_status": saved.get("run_status"), "candidate_id": candidate.get("candidate_id"),
                  "search_best_scope": "legacy_global" if legacy_best else "per_position",
                  "position": position, "position_best_candidate_id": position_best_id,
                  "is_position_best": (str(candidate.get("candidate_id")) == str(position_best_id)
                                       if position_best_id is not None else None)}
    if legacy_best:
        provenance["legacy_reported_best_candidate_id"] = best["candidate_id"]
    return {"pages": [{"page_id": p["page_id"], "html": render_fragment_page(p, candidate, scenario=pages["scenario"])} for p in pages["pages"]],
            "files": {}, "defense_variant": "ax_edit_v1/" + candidate["position"], "provenance": {"source_artifact": str(source), **provenance}}
