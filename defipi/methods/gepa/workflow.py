"""Config-only CLI for current GEPA AX-edit recoverability search."""
import argparse
from datetime import datetime
from pathlib import Path

from defipi.config import load_local_env
from defipi.methods.gepa.configuration import load_config


def _build_run_timestamp(enabled, timestamp_value, timestamp_format):
    if not enabled:
        return None
    if timestamp_value is not None:
        return timestamp_value
    return datetime.now().strftime(timestamp_format)


def _append_timestamp_to_path(path, timestamp):
    path_obj = Path(path)
    if timestamp is None:
        return path_obj
    suffix = "".join(path_obj.suffixes)
    stem = path_obj.name[: -len(suffix)] if suffix else path_obj.name
    return path_obj.with_name(f"{stem}_{timestamp}{suffix}")


def _resolve_timestamped_output_path(path, timestamp):
    return _append_timestamp_to_path(path, timestamp)


def main(argv=None):
    parser = argparse.ArgumentParser(description="GEPA AX-edit recoverability search", allow_abbrev=False,
        epilog="After search, resolve tied position winners with: python -m cli.gepa select prepare --config configs/methods/gepa-news-selection.yaml")
    parser.add_argument("--config", required=True, help="Complete GEPA YAML/JSON configuration file.")
    cli = parser.parse_args(argv)
    args = load_config(cli.config)
    load_local_env()
    from defipi.methods.gepa.run import run
    return run(args)
