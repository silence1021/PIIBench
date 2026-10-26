"""The current initial-prompt input is a nonempty JSON list of strings."""
import json

from defipi.config import _reject_json_constant


def load_attack_specs(path):
    with open(path, "r", encoding="utf-8") as stream:
        raw = json.load(stream, parse_constant=_reject_json_constant)
    if type(raw) is not list or not raw:
        raise ValueError("Attack file requires a nonempty list of prompt strings")
    for index, instruction in enumerate(raw):
        if type(instruction) is not str or not instruction.strip():
            raise ValueError(f"attack[{index}] must be a nonempty prompt string")
    return raw
