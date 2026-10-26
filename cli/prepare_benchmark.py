"""Prepare benchmark pages and tasks without model calls."""
import argparse
import json
from defipi.benchmark.prepare import prepare_records, prepare_suite

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--suite")
    source.add_argument("--records")
    parser.add_argument("--root", help="Required with --suite; root of its referenced data")
    parser.add_argument("--benchmark-id")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.suite and not args.root:
            raise ValueError("--suite requires --root")
        if args.records and args.root:
            raise ValueError("--root is only used with --suite")
        if args.records and not args.benchmark_id:
            raise ValueError("--records requires --benchmark-id")
        result = prepare_suite(args.suite, args.output, root=args.root) if args.suite else prepare_records(args.records, args.output, benchmark_id=args.benchmark_id)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
