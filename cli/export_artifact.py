"""Export an existing method result into a portable materialized site."""
import argparse
import importlib
import json
from defipi.artifacts import write_artifact
from defipi.benchmark import load_pages
from defipi.paths import git_revision
METHODS = ("noguard", "gepa", "piiguard", "webcloak", "autoguard", "muzzle")
def main(argv=None):
    method_parser = argparse.ArgumentParser(add_help=False)
    method_parser.add_argument("--method", choices=METHODS)
    selected, _ = method_parser.parse_known_args(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=METHODS, required=True)
    parser.add_argument("--pages", required=True)
    parser.add_argument("--artifact-id")
    parser.add_argument("--base-url")
    parser.add_argument("--output", required=True)
    module = None
    if selected.method:
        module = importlib.import_module(f"defipi.methods.{selected.method}.export")
        module.add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        pages = load_pages(args.pages)
        result = module.export(pages, args)
        path = write_artifact(args.output, pages, result, method_id=args.method, artifact_id=args.artifact_id or f"{args.method}-{pages['scenario']}", base_url=args.base_url, code_revision=git_revision())
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps({"artifact": str(path)}, indent=2))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
