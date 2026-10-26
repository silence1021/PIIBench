"""Offline export of successful MUZZLE transfers."""
import json
from pathlib import Path
from .injection import inject_notice

def add_arguments(parser):
    parser.add_argument("--source", required=True)

def export(pages, args):
    source = Path(args.source)
    saved = json.loads(source.read_text())
    if saved.get("version") != "muzzle-v3-webcloak-imitation-v1" or saved.get("scenario") != pages["scenario"]:
        raise ValueError("Expected a matching MUZZLE transfer scenario")
    if saved.get("benchmark_id") != pages["benchmark_id"]:
        raise ValueError("MUZZLE transfer belongs to a different benchmark")
    expected = {p["page_id"]: {key: p[key] for key in ("source_record_id", "prepared_record_id")} for p in pages["pages"]}
    if saved.get("page_records") != expected:
        raise ValueError("MUZZLE transfer page record mapping differs from benchmark")
    indexed = {p["page_id"]: p for p in saved["pages"]}
    if len(indexed) != len(saved["pages"]):
        raise ValueError("Duplicate MUZZLE transfer page IDs")
    missing = [p["page_id"] for p in pages["pages"] if indexed.get(p["page_id"], {}).get("status") != "completed"]
    if missing:
        raise ValueError(f"Missing successful MUZZLE transfers: {missing}")
    return {"pages": [{"page_id": p["page_id"], "html": inject_notice(p["clean_html"], saved["surface"], indexed[p["page_id"]]["notice"],
                                page_profile=pages["scenario"])} for p in pages["pages"]],
            "files": {}, "defense_variant": saved["version"],
            "provenance": {"source_artifact": str(source), "source": saved["source"], "generator": saved["generator"]}}
