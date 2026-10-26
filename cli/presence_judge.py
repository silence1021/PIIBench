"""Unified local presence judge. Only run/retry make model calls."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from defipi.evaluation.file_task_judges import write_json
from defipi.evaluation.presence_judges import prepare, extend, decisions, validate_store
from defipi.evaluation.presence_metrics import summarize
from defipi.evaluation.presence_runner import run_jobs, status
from defipi.evaluation.presence_sources import ROOT, iter_subjects, read, resolve


def local_output(value):
    p = resolve(value)
    return p


def prepare_command(args):
    unresolved = Path(args.output)
    if not unresolved.is_absolute():
        unresolved = ROOT / unresolved
    if unresolved.is_symlink():
        raise FileExistsError(f"Presence destination is a symbolic link: {unresolved}")
    spec = {"sources": [{"id": args.source_id, "kind": "runs", "path": str(Path.cwd()),
                         "runs": [str(Path(p).resolve()) for p in args.runs]}]}
    output = local_output(args.output)
    for value in args.runs:
        path = Path(value).resolve()
        if output == path or output.is_relative_to(path):
            raise ValueError("Judge output cannot be inside source evidence")
    registry = read(resolve(args.registry))
    function = extend if args.append else prepare
    return {"output": str(output), **function(iter_subjects(spec), output, registry, sources=spec)}


def run_command(args):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    if not args.allow_paid:
        raise ValueError("Paid judge execution requires --allow-paid and an explicit request budget")
    directory = local_output(args.jobs)
    validate_store(directory)
    concurrency = {}
    for item in args.concurrency or []:
        provider, count = item.split("=", 1)
        if provider not in ("openai", "deepseek", "openrouter", "anthropic"):
            raise ValueError("Unknown judge provider")
        concurrency[provider] = int(count)
    def progress(snapshot):
        print(json.dumps({"presence_progress": snapshot}, ensure_ascii=False), file=sys.stderr, flush=True)
    return run_jobs(directory, args.max_requests, concurrency=concurrency,
                    reclaim_started=args.reclaim_started, progress=progress,
                    retry_config_errors=getattr(args, "retry_config_errors", False))


def report_command(args):
    directory, output = local_output(args.jobs), local_output(args.output)
    if output == directory or output.is_relative_to(directory):
        raise ValueError("Write reports outside the frozen jobs directory")
    rows = list(decisions(directory))
    report = summarize(rows, bootstrap_samples=args.bootstrap_samples)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "labels.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_json(output / "summary.json", report)
    from cli.report_paper_tables import repeat_curves
    write_json(output / "repeat_curves.json", list(repeat_curves(rows)))
    return {"output": str(output), "texts": len(rows), "groups": len(report["groups"]),
            "unknown_texts": report["text_outcomes"]["unknown_texts"],
            "unknown_text_rate": report["text_outcomes"]["unknown_text_rate"],
            "human_validation": "pending", "model_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="OFFLINE: read sources and freeze deduplicated requests")
    p.add_argument("--runs", nargs="+", required=True, help="Explicit frozen run directories; read-only")
    p.add_argument("--source-id", default="public-runs")
    p.add_argument("--registry", default="configs/evaluation_models.json")
    p.add_argument("--output", required=True)
    p.add_argument("--append", action="store_true", help="Refresh in the SAME store, preserving receipts and send limits")
    p.set_defaults(function=prepare_command)
    for name in ("run", "retry"):
        p = sub.add_parser(name, help="PAID: run unfinished jobs within their persisted three-send cap")
        p.add_argument("--allow-paid", action="store_true", help="Authorize API execution for this invocation")
        p.add_argument("--jobs", required=True)
        p.add_argument("--max-requests", type=int, required=True, help="Maximum new sends INCLUDING retries in this invocation")
        p.add_argument("--concurrency", nargs="*", metavar="PROVIDER=N")
        p.add_argument("--reclaim-started", action="store_true")
        if name == "retry":
            p.add_argument("--retry-config-errors", action="store_true",
                           help="Explicitly resume configuration/authentication stops without resetting attempts")
        p.set_defaults(function=run_command)
    p = sub.add_parser("status", help="OFFLINE: receipts, sends, errors, actual reported usage")
    p.add_argument("--jobs", required=True)
    def status_command(args):
        directory = local_output(args.jobs)
        validate_store(directory)
        return status(directory)
    p.set_defaults(function=status_command)
    p = sub.add_parser("report", help="OFFLINE: labels, same-attempt matrix, @4/all4 and matched NoGuard RP")
    p.add_argument("--jobs", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--bootstrap-samples", type=int, default=1000)
    p.set_defaults(function=report_command)
    args = parser.parse_args(argv)
    if getattr(args, "bootstrap_samples", 0) < 0:
        parser.error("bootstrap-samples must be nonnegative")
    print(json.dumps(args.function(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
