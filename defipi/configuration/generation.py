"""Explicit settings for offline page generation and browser validation."""
from pathlib import Path

from defipi.config import load_config_file
from .schema import Field, Problem, validate_config

VIEWPORT_FIELDS = {"width": Field(int, minimum=1), "height": Field(int, minimum=1)}
SNAPSHOT_VALIDATION_FIELDS = {
    "browser_launch_timeout_ms": Field(int, minimum=1),
    "action_timeout_ms": Field(int, minimum=1),
    "viewport": Field(dict, schema=VIEWPORT_FIELDS),
    "navigation_timeout_ms": Field(int, minimum=1),
    "wait_after_navigation_ms": Field(int, minimum=0),
}
ACADEMIC_VALIDATION_FIELDS = {
    "browser_launch_timeout_ms": Field(int, minimum=1),
    "action_timeout_ms": Field(int, minimum=1),
    "desktop_viewport": Field(dict, schema=VIEWPORT_FIELDS),
    "mobile_viewport": Field(dict, schema=VIEWPORT_FIELDS),
    "navigation_timeout_ms": Field(int, minimum=1),
    "theme_wait_ms": Field(int, minimum=0),
    "viewport_wait_ms": Field(int, minimum=0),
}
GENERATION_FIELDS = {
    "context_file": Field(str), "attack_file": Field(str),
    "count": Field(int, minimum=1), "attacks_per_context": Field(int, minimum=1),
    "variants_per_attack": Field(int, minimum=1), "positions": Field(str),
    "attack_mode": Field(str, choices=("cycle", "random")),
    "seed": Field(int), "table_seed": Field(int), "shuffle_contexts": Field(bool),
    "max_samples": Field(int, minimum=1, nullable=True),
    "output_json": Field(str), "output_html_dir": Field(str),
    "enable_injection": Field(bool),
}
ACADEMIC_FIELDS = {
    "personas": Field(str), "output_root": Field(str), "template_cache_root": Field(str),
    "jobs": Field(int, minimum=1), "incremental": Field(bool),
    "browser_validation": Field(bool),
    "validation_settings": Field(dict, required=False, schema=ACADEMIC_VALIDATION_FIELDS),
}


GENERATION_POSITIONS = frozenset({
    "before", "after", "inline", "meta", "header", "footer", "hidden", "data-attr", "comment", "body",
    "bio_tail", "contact_block", "footer_notice", "header_notice",
})


def generation_rules(data, problems):
    raw = data.get("positions")
    if type(raw) is str:
        positions = [part.strip() for part in raw.split(",") if part.strip()]
        if not positions:
            problems.append(Problem("positions", "must contain at least one position"))
        unknown = sorted(set(positions) - GENERATION_POSITIONS)
        if unknown:
            problems.append(Problem("positions", "unsupported positions: " + ", ".join(unknown)))


def browser_settings_rule(data, problems):
    if data.get("browser_validation") is True and "validation_settings" not in data:
        problems.append(Problem("validation_settings", "required when browser_validation is true"))
    if data.get("browser_validation") is False and "validation_settings" in data:
        problems.append(Problem("validation_settings", "inactive when browser_validation is false"))


def load_settings(path, fields, *, path_fields=(), rules=()):
    data = load_config_file(path)
    def path_rule(values, problems):
        for name in path_fields:
            if type(values.get(name)) is str and not values[name].strip():
                problems.append(Problem(name, "must be a non-empty path"))
    validate_config(data, fields, rules=(*rules, path_rule))
    root = Path(path).expanduser().resolve().parent
    result = dict(data)
    for name in path_fields:
        result[name] = str((root / data[name]).resolve())
    return result
