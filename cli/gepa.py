#!/usr/bin/env python3
"""
Run the GEPA AX-edit recoverability search.

Run:
  python -m cli.gepa --help
"""

import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["select"]:
        from defipi.methods.gepa.selection import main as select
        return select(args[1:])
    from defipi.methods.gepa.workflow import main as search
    return search(args)


if __name__ == "__main__":
    result = main()
    if isinstance(result, int):
        raise SystemExit(result)
