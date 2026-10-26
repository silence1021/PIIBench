#!/usr/bin/env python3
"""Capture, build, validate, and locally serve real-page snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from defipi.snapshots.build import build_capture
from defipi.snapshots.capture import capture_manifest
from defipi.snapshots.manifest import load_manifest
from defipi.snapshots.server import make_server
from defipi.snapshots.validation import validate_capture
from defipi.configuration.generation import SNAPSHOT_VALIDATION_FIELDS, load_settings


def _print_report(report: dict[str, object]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _command_capture(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    report = capture_manifest(
        manifest,
        args.output_root,
        delay_seconds=args.delay_seconds,
        max_resource_bytes=args.max_resource_bytes,
        browser_launch_timeout_ms=args.browser_launch_timeout_ms,
    )
    _print_report(report)
    return 0 if report["status"] == "ok" else 1


def _command_build(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    settings = load_settings(args.validation_config, SNAPSHOT_VALIDATION_FIELDS)
    report = build_capture(args.capture, manifest, args.output_root, validation_settings=settings)
    _print_report(report)
    return 0 if report["status"] == "ok" else 1


def _command_validate(args: argparse.Namespace) -> int:
    settings = None
    if args.browser:
        if args.validation_config is None:
            raise ValueError("--browser requires --validation-config")
        settings = load_settings(args.validation_config, SNAPSHOT_VALIDATION_FIELDS)
    elif args.validation_config is not None:
        raise ValueError("--validation-config is only used with --browser")
    report = validate_capture(args.snapshot, browser=args.browser, validation_settings=settings)
    _print_report(report)
    return 0 if report["status"] == "ok" else 1


def _validated_page_dir(path: str | Path) -> Path:
    page_dir = Path(path).expanduser().resolve()
    marker_path = page_dir / ".piibench-syntheticized"
    validation_path = page_dir / "validation.json"
    if not marker_path.is_file() or not validation_path.is_file():
        raise ValueError("Refusing to serve a snapshot without a syntheticized validation marker")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if not validation.get("ok"):
        raise ValueError("Refusing to serve a snapshot whose validation status is not ok")
    return page_dir


def _command_serve(args: argparse.Namespace) -> int:
    page_dir = _validated_page_dir(args.snapshot)
    index_path = page_dir / args.variant / "index.html"
    if not index_path.is_file():
        raise FileNotFoundError(f"Snapshot variant does not exist: {index_path}")
    server = make_server(page_dir, host=args.host, port=args.port)
    host, port = server.server_address[:2]
    print(f"Serving validated {args.variant} snapshot at http://{host}:{port}/{args.variant}/index.html")
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
        description="Build privacy-safe, reproducible snapshots of public web pages"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="Capture raw HTML, rendered DOM, and response assets")
    capture.add_argument("--manifest", required=True, help="Private YAML/JSON URL and PII manifest")
    capture.add_argument("--output-root", required=True)
    capture.add_argument("--delay-seconds", type=float, required=True)
    capture.add_argument("--max-resource-bytes", type=int, required=True)
    capture.add_argument("--browser-launch-timeout-ms", type=int, required=True)
    capture.set_defaults(handler=_command_capture)

    build = subparsers.add_parser("build", help="Syntheticize and localize a completed raw capture")
    build.add_argument("--capture", required=True, help="Path to raw/<capture_id>")
    build.add_argument("--manifest", required=True, help="The same private manifest used for capture")
    build.add_argument("--output-root", required=True)
    build.add_argument("--validation-config", required=True)
    build.set_defaults(handler=_command_build)

    validate = subparsers.add_parser("validate", help="Re-run contact and offline replay checks")
    validate.add_argument("--snapshot", required=True, help="Synthetic capture directory or one page directory")
    validate.add_argument("--browser", action=argparse.BooleanOptionalAction, required=True)
    validate.add_argument("--validation-config", help="Browser validation YAML/JSON settings")
    validate.set_defaults(handler=_command_validate)

    serve = subparsers.add_parser("serve", help="Serve one validated page on localhost")
    serve.add_argument("--snapshot", required=True, help="Synthetic page directory containing snapshot.json")
    serve.add_argument("--variant", required=True, choices=["static", "scripted"])
    serve.add_argument("--host", required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.set_defaults(handler=_command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "port", 0) not in range(65536):
        parser.error("--port must be between 0 and 65535")
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"snapshot {args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
