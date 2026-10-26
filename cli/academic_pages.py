#!/usr/bin/env python3
"""Prepare, generate, validate, and locally preview synthetic academic pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from defipi.academic_pages.curated import validate_build
from defipi.academic_pages.generator import (
    generate_academic_pages,
    prepare_templates,
)
from defipi.academic_pages.server import make_preview_server
from defipi.configuration.generation import (ACADEMIC_FIELDS, ACADEMIC_VALIDATION_FIELDS,
    browser_settings_rule, load_settings)


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _prepare(args: argparse.Namespace) -> int:
    report = prepare_templates(args.cache_root, prepare_docker=args.docker)
    _print(report)
    return 0


def _generate(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, ACADEMIC_FIELDS,
        path_fields=("personas", "output_root", "template_cache_root"), rules=(browser_settings_rule,))
    report = generate_academic_pages(**settings)
    _print(report)
    return 0


def _validate(args: argparse.Namespace) -> int:
    settings = None
    if args.browser:
        if args.validation_config is None:
            raise ValueError("--browser requires --validation-config")
        settings = load_settings(args.validation_config, ACADEMIC_VALIDATION_FIELDS)
    elif args.validation_config is not None:
        raise ValueError("--validation-config is only used with --browser")
    report = validate_build(args.build, browser=args.browser, validation_settings=settings, write_report=True)
    _print(report)
    return 0 if report["ok"] else 1


def _preview(args: argparse.Namespace) -> int:
    report = validate_build(args.build, browser=False, write_report=False)
    if not report["ok"]:
        raise ValueError("Refusing to preview an invalid academic page build")
    build_dir = Path(args.build).expanduser().resolve()
    manifest = json.loads((build_dir / "generation_manifest.json").read_text(encoding="utf-8"))
    server = make_preview_server(build_dir, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"Serving academic page build at http://{host}:{port}/")
    for page in manifest.get("pages") or []:
        print(f"- {page['template_backend']} / {page['persona_id']}: http://{host}:{port}/{page['html_path']}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate paired, defense-free academic homepages from synthetic personas"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Fetch pinned templates and prepare build images")
    prepare.add_argument("--cache-root", required=True)
    prepare.add_argument("--docker", action=argparse.BooleanOptionalAction, required=True,
                         help="Whether to build or pull Docker images")
    prepare.set_defaults(handler=_prepare)

    generate = subparsers.add_parser("generate", help="Build paired Curated Scripted base pages")
    generate.add_argument("--config", required=True)
    generate.set_defaults(handler=_generate)

    validate = subparsers.add_parser("validate", help="Re-run parity and offline checks")
    validate.add_argument("--build", required=True)
    validate.add_argument("--browser", action=argparse.BooleanOptionalAction, required=True)
    validate.add_argument("--validation-config", help="Browser validation YAML/JSON settings")
    validate.set_defaults(handler=_validate)

    preview = subparsers.add_parser("preview", help="Serve a validated build on localhost only")
    preview.add_argument("--build", required=True)
    preview.add_argument("--host", required=True)
    preview.add_argument("--port", type=int, required=True)
    preview.set_defaults(handler=_preview)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "port", 1) < 0 or getattr(args, "port", 1) > 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"academic pages {args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
