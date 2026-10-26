"""Explicit inputs for the current GEPA AX-edit search."""
from defipi.execution.api.clients import CLIENT_SETTINGS_FIELDS, validate_scalar_client_settings
from argparse import Namespace

from defipi.config import load_config_file
from defipi.configuration.schema import Field, Problem, validate_config
from defipi.execution.api.clients import (
    REASONING_EFFORT_CHOICES, SUPPORTED_BACKENDS, validate_reasoning_effort,
)
from defipi.evaluation.llm_filter import FILTER_PROMPT_PROFILE_CHOICES, FILTER_PROMPT_VARIANTS
from defipi.methods.gepa.recoverability_pareto import POSITIONS, STRATEGY

# These are the fixed v8 protocol, not experiment options.
PROTOCOL_SETTINGS = {
    "search_strategy": STRATEGY,
    "positions": ",".join(POSITIONS),
    "candidate_mode": "fragment",
    "observation_format": "axtree",
    "filter_stage": "ax_after_render",
    "privacy_metric_profile": "name_gated_contact",
    "fragment_budget_chars": None,
    "eval_max_response_tokens": None,
    "filter_max_response_tokens": None,
    "mutation_max_tokens": None,
}

FIELDS = {
    **{f"{role}_client_settings": Field(dict, required=role != "filter", schema=CLIENT_SETTINGS_FIELDS) for role in ("eval", "filter", "mutation")},
    "mutation_max_retries": Field(int, minimum=1),
    "mutation_retry_backoff_seconds": Field((int, float), minimum=0),
    **{name: Field(str) for name in (
        "seeds_json", "split_manifest", "initial_attack_file", "html_dir",
        "rewrite_instruction_file", "rewrite_user_instruction_file", "output_json",
        "cache_path", "mutation_cache_path",
    )},
    **{name: Field(str, required=False) for name in (
        "recovery_instruction_file", "filter_cache_path", "timestamp_format",
    )},
    **{name: Field(str, nullable=True, required=False) for name in (
        "attack_prompts_file", "question_override", "timestamp_value",
    )},
    "resume_from_output_json": Field(str, nullable=True),
    "initial_prompt_limit": Field(int, nullable=True, minimum=1),
    "seed_index": Field(int, minimum=0),
    **{name: Field(int) for name in ("table_seed", "rng_seed")},
    **{name: Field(int, minimum=1) for name in (
        "feedback_minibatch_size", "ranking_repeats", "ranking_questions_per_page",
        "max_extensions_per_position", "active_pool_size", "parent_feedback_repeats",
        "max_proposal_failures", "stagnation_patience", "model_concurrency", "eval_max_retries",
    )},
    "fragment_profile": Field(str, choices=("academic", "news")),
    "pareto_tau": Field((int, float), minimum=0),
    "joint_weight": Field((int, float), minimum=0),
    "parent_epsilon": Field((int, float), minimum=0, maximum=1),
    "eval_retry_backoff_seconds": Field((int, float), minimum=0),
    "enable_filter": Field(bool),
    "timestamp_outputs": Field(bool),
    "filter_prompt_variant": Field(str, required=False, choices=FILTER_PROMPT_VARIANTS),
    "filter_prompt_profile": Field(str, required=False, choices=FILTER_PROMPT_PROFILE_CHOICES),
    **{f"{role}_backend": Field(str, required=role != "filter", choices=tuple(sorted(SUPPORTED_BACKENDS)))
       for role in ("eval", "filter", "mutation")},
    **{f"{role}_model": Field(str, required=role != "filter") for role in ("eval", "filter", "mutation")},
    **{f"{role}_temperature": Field((int, float), required=role != "filter", nullable=True, minimum=0)
       for role in ("eval", "filter", "mutation")},
    **{f"{role}_reasoning_effort": Field(str, required=role != "filter", nullable=True, choices=REASONING_EFFORT_CHOICES)
       for role in ("eval", "filter", "mutation")},
}


FILTER_FIELDS = tuple(name for name in FIELDS if name.startswith("filter_"))


def _rules(config, problems):
    for name, value in config.items():
        if name in FIELDS and type(value) is str and not value.strip():
            problems.append(Problem(name, "must be nonempty"))
    if config.get("enable_filter") is True:
        for name in FILTER_FIELDS:
            if name not in config:
                problems.append(Problem(name, "required when enable_filter=true"))
    sources = [name for name in ("attack_prompts_file", "question_override", "recovery_instruction_file")
               if config.get(name) is not None]
    if len(sources) > 1:
        problems.append(Problem(sources[-1], "question sources are mutually exclusive: " + ", ".join(sources)))
    if config.get("attack_prompts_file") is None and config.get("question_override") is None and "recovery_instruction_file" not in config:
        problems.append(Problem("recovery_instruction_file", "required when neither attack_prompts_file nor question_override selects a question source"))
    if config.get("timestamp_outputs") is True and config.get("timestamp_value") is None and "timestamp_format" not in config:
        problems.append(Problem("timestamp_format", "required when timestamp_outputs=true and timestamp_value is null or absent"))
    if config.get("attack_prompts_file") is None and type(config.get("ranking_questions_per_page")) is int and config["ranking_questions_per_page"] != 1:
        problems.append(Problem("ranking_questions_per_page", "requires attack_prompts_file when greater than one"))
    for role in ("eval", "filter", "mutation"):
        if type(config.get(f"{role}_client_settings")) is dict and type(config.get(f"{role}_backend")) is str:
            try:
                validate_scalar_client_settings(config[f"{role}_client_settings"], config[f"{role}_backend"], temperature=config.get(f"{role}_temperature"), reasoning_effort=config.get(f"{role}_reasoning_effort"))
            except ValueError as exc:
                problems.extend(Problem(f"{role}_client_settings.{p.path}", p.message) for p in getattr(exc, "problems", ()))
        backend, effort = config.get(f"{role}_backend"), config.get(f"{role}_reasoning_effort")
        if type(backend) is str and backend in SUPPORTED_BACKENDS and (effort is None or type(effort) is str):
            try:
                validate_reasoning_effort(backend, effort, role=role)
            except ValueError as exc:
                problems.append(Problem(f"{role}_reasoning_effort", str(exc)))


def validate_inputs(config):
    return validate_config(config, FIELDS, rules=(_rules,))


def input_config(args):
    """Select user inputs without mixing in derived or protocol settings."""
    return {name: getattr(args, name) for name in getattr(args, "_input_keys", FIELDS) if hasattr(args, name)}


def load_config(path):
    config = validate_inputs(load_config_file(path))
    # Missing inactive fields support internal branching, but are not promoted
    # to user input or emitted as invented configuration values.
    inactive = {name: None for name, field in FIELDS.items() if not field.required and name not in config}
    return Namespace(**config, **inactive, **PROTOCOL_SETTINGS,
                     config=str(path), _input_keys=tuple(config))
