#!/usr/bin/env python3
"""Prepare, validate, run, and report unified frozen-page final evaluations."""

import argparse
import json
from pathlib import Path

from defipi.config import load_local_env
from defipi.evaluation import final_eval_dispatch as dispatch
from defipi.evaluation.unified_final_eval import report_final_eval


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (("plan", "validate five-axis bindings and count calls offline"),
                               ("prepare", "freeze config, pages, resources, and tasks")):
        prepare = commands.add_parser(command, help=help_text)
        prepare.add_argument("--config", required=True)
    validate = commands.add_parser("validate", help="validate a frozen lock and show call plan")
    validate.add_argument("--lock", required=True)
    run = commands.add_parser("run", help="run or resume a frozen final evaluation")
    run.add_argument("--lock", required=True)
    run.add_argument("--resume", action="store_true")
    report = commands.add_parser("report", help="print the saved summary")
    report.add_argument("--results", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ("plan", "prepare"):
            if args.command == "plan":
                result = dispatch.call_plan(dispatch.build_lock(args.config))
            else:
                lock = dispatch.prepare(args.config)
                result = {"prepared": True, "lock_path": lock["artifacts"]["lock_path"],
                          "run_id": lock["run_id"]}
        elif args.command == "validate":
            result = dispatch.validate(args.lock)
        elif args.command == "run":
            lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))
            load_local_env()
            code = dispatch.run(args.lock, resume=args.resume)
            result = report_final_eval(lock["artifacts"]["results_path"])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return code
        else:
            result = report_final_eval(args.results)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
