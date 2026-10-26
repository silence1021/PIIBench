#!/usr/bin/env python3
"""Validate page-agnostic synthetic persona manifests."""

from __future__ import annotations

import argparse
import json
import sys

from defipi.synthetic_personas import load_persona_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate synthetic persona data without generating pages"
    )
    parser.add_argument("--manifest", required=True, help="Synthetic persona YAML or JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_persona_manifest(args.manifest)
    except Exception as exc:
        print(f"synthetic persona validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "version": manifest.version,
                "persona_count": len(manifest.personas),
                "persona_ids": [persona.id for persona in manifest.personas],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
