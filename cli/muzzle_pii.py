#!/usr/bin/env python3
"""Validate or run the bounded full MUZZLE-PII browser workflow."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from defipi.config import load_config_file, load_local_env
from defipi.methods.muzzle.core import confirm, run, validate_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "run", "confirm", "transfer"))
    parser.add_argument("--config", required=True, help="Entry config for the selected command")
    parser.add_argument("--retry-failed", action="store_true", help=(
        "Transfer only: retry saved failed pages without resetting cumulative usage; "
        "completed pages are skipped."))
    args = parser.parse_args()
    if args.retry_failed and args.command != "transfer":
        parser.error("--retry-failed is only relevant to transfer")
    if args.command == "transfer":
        from defipi.methods.muzzle.transfer import transfer
        load_local_env()
        result = transfer(args.config, retry_failed=args.retry_failed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2
    if args.command == "confirm":
        load_local_env()
        result = confirm(args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2
    config = load_config_file(args.config)
    if args.command == "validate":
        _, _, metadata = validate_config(config)
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return 0
    load_local_env()
    from defipi.methods.muzzle.configuration import validate_search
    validate_search(config)
    from defipi.methods.muzzle.core import _path
    output = _path(config["output"])
    result = run(config, output)
    print(json.dumps({"status": result["status"], "output": str(output.resolve()),
                      "usage": result["usage"],
                      **({"pi_usage": result["pi_usage"]} if "pi_usage" in result else {}),
                      "runs": [{"run_index": r["run_index"], "status": r["status"]} for r in result["runs"]],
                      "pages": [{"id": p["record_id"], "baseline": p["baseline"],
                                 "selected": (p["best"] or {}).get("metrics")} for p in result["pages"]]},
                     ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
