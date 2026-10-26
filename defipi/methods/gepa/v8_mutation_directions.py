"""Load the fixed, shared v8 mutation direction contract."""

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import json
from pathlib import Path


_DIRECTIONS_PATH = (
    _PACKAGE_ROOT / "prompts" / "gepa" / "v8_mutation_directions.json"
)


def _load_direction_contract():
    with _DIRECTIONS_PATH.open(encoding="utf-8") as source:
        raw_directions = json.load(source)
    if not isinstance(raw_directions, list) or not raw_directions:
        raise ValueError("v8 mutation directions must be a non-empty list")

    directions = []
    names = set()
    for direction in raw_directions:
        if not isinstance(direction, dict):
            raise ValueError("each v8 mutation direction must be an object")
        name = direction.get("name")
        instruction = direction.get("instruction")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each v8 mutation direction needs a non-empty string name")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("each v8 mutation direction needs a non-empty string instruction")
        if name in names:
            raise ValueError(f"duplicate v8 mutation direction name: {name}")
        names.add(name)
        directions.append((name, instruction))
    return tuple(directions)


_DIRECTION_CONTRACT = _load_direction_contract()
MUTATION_DIRECTION_NAMES = tuple(name for name, _ in _DIRECTION_CONTRACT)


def mutation_direction_contract(profile):
    """Return an independent copy of the shared v8 direction contract."""
    normalized = str(profile or "").strip().lower()
    if normalized not in {"academic", "news"}:
        raise ValueError(f"Unsupported v8 mutation direction profile: {profile}")
    return [
        {"name": name, "instruction": instruction}
        for name, instruction in _DIRECTION_CONTRACT
    ]
