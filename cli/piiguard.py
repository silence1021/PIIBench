"""Prepare data or run the PIIGuard optimization method."""
import argparse
import json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare-data", "optimize"):
        sub.add_parser(command).add_argument("--config", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare-data":
        from defipi.methods.piiguard.data import prepare_piiguard_data
        print(json.dumps(prepare_piiguard_data(args.config), ensure_ascii=False, indent=2))
        return 0
    from defipi.concurrency import FileSlots
    from defipi.config import load_local_env
    from defipi.methods.piiguard.configuration import load_plan_inputs
    from defipi.methods.piiguard.optimization import ROOT, run
    config, _ = load_plan_inputs(args.config, root=ROOT)
    if not config["prepare_only"]:
        load_local_env()
    output = ROOT / config["output_dir"]
    output.mkdir(parents=True, exist_ok=True)
    with FileSlots(output / ".run_lock", 1, busy=RuntimeError("a PIIGuard optimization is already running in this output directory")):
        print(json.dumps(run(args.config), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
