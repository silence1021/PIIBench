"""
Shared config-file loading for experiment entrypoints.

Supports:
- YAML (`.yaml`, `.yml`) through the required PyYAML dependency
- JSON (`.json`) without extra dependencies
- Local `.env` files for environment variables such as `OPENAI_API_KEY`

"""
import json
import os
from pathlib import Path

import yaml


def _unique_json_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"Duplicate config key: {key!r}")
        data[key] = value
    return data


def _reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant in config: {value}")


class _StrictSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError as exc:
                raise ValueError("Config object keys must be hashable") from exc
            if duplicate:
                raise ValueError(f"Duplicate config key: {key!r}")
        return super().construct_mapping(node, deep=deep)


def load_config_file(path):
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    suffix = config_path.suffix.lower()
    with config_path.open("r", encoding="utf-8") as f:
        if suffix in (".yaml", ".yml"):
            data = yaml.load(f, Loader=_StrictSafeLoader)
        elif suffix == ".json":
            data = json.load(f, object_pairs_hook=_unique_json_object,
                             parse_constant=_reject_json_constant)
        else:
            shown = suffix or "<no extension>"
            raise ValueError(
                f"Unsupported config file extension: {shown}. "
                "Use .yaml, .yml, or .json."
            )

    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a top-level mapping/object: {config_path}")
    return data


def _env_candidates():
    project_root = Path(__file__).resolve().parents[1]
    candidates = [Path.cwd() / ".env", project_root / ".env"]
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield candidate


def _strip_env_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_local_env():
    for env_path in _env_candidates():
        if not env_path.exists():
            continue
        with env_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = _strip_env_quotes(value.strip())
        return env_path
    return None
