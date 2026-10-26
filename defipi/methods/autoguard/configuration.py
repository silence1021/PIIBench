"""AutoGuard configuration contracts, checked before reads, clients, or output."""
from defipi.execution.api.clients import CLIENT_SETTINGS_FIELDS, validate_scalar_client_settings
from urllib.parse import urlparse

from defipi.configuration.schema import Field, Problem, validate_config

NUMBER = (int, float)
MODEL_FIELDS = {
    "client_settings": Field(dict, schema=CLIENT_SETTINGS_FIELDS),
    "model": Field(str), "backend": Field(str, choices=("openai", "anthropic", "deepseek", "openrouter")),
    "reasoning_effort": Field(str, nullable=True),
    "temperature": Field(NUMBER, nullable=True, minimum=0, maximum=2),
    "max_tokens": Field(int, nullable=True, minimum=1),
}
SEARCH_FIELDS = {
    "K": Field(int, minimum=1), "T": Field(int, minimum=1),
    "alpha": Field(NUMBER, minimum=0), "beta": Field(NUMBER, minimum=0),
    # These describe the fixed reconstruction, rather than optional algorithms.
    "max_discovery_attempts": Field(int, nullable=True, required=False, choices=(None,)),
    "max_refinements": Field(int, nullable=True, required=False, choices=(None,)),
    "phase1_algorithmic_cap": Field(int, nullable=True, required=False, choices=(None,)),
    "primary_selection": Field(str, required=False, choices=("empirical_mean",)),
    "record_cumulative_winner": Field(bool, required=False, choices=(True,)),
    "difficulty_initialization": Field(NUMBER, required=False, choices=(0.0,)),
    "difficulty_timing": Field(str, required=False, choices=("round_start_history_then_post_round_update",)),
    "pending_round_policy": Field(str, required=False, choices=("checkpoint_and_resume_same_round",)),
}
BROWSER_FIELDS = {
    "episode_seed": Field(int, minimum=0),
    "episode_step_budget": Field(int, minimum=1),
    "agentlab_parse_retries": Field(int, minimum=1, maximum=3),
    "injection_document_index": Field(int, choices=(1, 2)),
    "adapter_module": Field(str, required=False, choices=("defipi.methods.autoguard.browsergym_agent",)),
    "retry_semantics_status": Field(str, required=False),
    "document_visit_convention": Field(str, required=False),
}
LIMIT_FIELDS = {
    "discovery_attempt_budget": Field(int, minimum=1),
    "search_workers": Field(int, minimum=1, maximum=5),
    "agent_slot_directory": Field(str),
    "http_retry_policy": Field(str, required=False),
    "discovery_budget_scope": Field(str, required=False),
}
DATA_FIELDS = {
    "patterns": Field(list, required=False, items=Field(str)),
    "discovery_paraphrases_per_pattern": Field(int, required=False, choices=(10,)),
    "bandit_paraphrases_per_pattern": Field(int, required=False, choices=(10,)),
    "heldout_source": Field(str, required=False),
    "paraphrase_provenance_required": Field(bool, required=False, choices=(True,)),
    "accepted_provenance_kinds": Field(list, required=False, items=Field(str)),
    "training_questions": Field(int, required=False, choices=(200,)),
    "existing_training_pages": Field(int, required=False, choices=(25,)),
    "binding_manifest": Field(str, required=False),
    "paraphrase_generation_calls": Field(int, required=False, minimum=0),
}
OBJECTIVE_FIELDS = {
    "protected_fields": Field(list, required=False, items=Field(str)),
    "scoring_version": Field(str, required=False, choices=("contact-dj-v1",)),
    "loss": Field(str, required=False), "joint_weight": Field(NUMBER, required=False, choices=(0.1,)),
    "training_success": Field(str, required=False), "base_reward": Field(str, required=False),
    **{key: Field(bool, required=False, choices=(True,)) for key in (
        "name_does_not_gate_contact_disclosure", "name_required_for_joint_metric",
        "refusal_is_diagnostic_only", "llm_scorer_is_diagnostic_only")},
}
CONFIG_FIELDS = {
    "seed": Field(int, minimum=0), "output_dir": Field(str),
    "model_override": Field(dict, schema=MODEL_FIELDS),
    "search": Field(dict, schema=SEARCH_FIELDS), "browser": Field(dict, schema=BROWSER_FIELDS),
    "limits": Field(dict, schema=LIMIT_FIELDS),
    "page_server": Field(dict, nullable=True, required=False, schema={
        "base_url": Field(str), "html_root": Field(str)}),
    "cases_file": Field(str, nullable=True, required=False),
    "heldout_cases_file": Field(str, nullable=True, required=False),
    # In-memory cases are a data boundary; validate_cases owns their contract.
    "cases": Field(list, required=False, items=Field(dict)),
    "heldout_cases": Field(list, required=False, items=Field(dict)),
    "config_source": Field(str, required=False),
    "roles": Field(list, required=False, items=Field(str)),
    "data": Field(dict, required=False, schema=DATA_FIELDS),
    "protection_objective": Field(dict, required=False, schema=OBJECTIVE_FIELDS),
    "reconstruction_notes": Field(list, required=False, items=Field(str)),
    **{key: Field(str, required=False) for key in ("reproduction_label", "paper", "task_reconstruction")},
    **{key: Field(bool, required=False) for key in (
        "author_code_obtained_for_this_reproduction", "training_prompts_public")},
}


def _nonempty(value, fields, prefix, problems):
    for key, field in fields.items():
        if key not in value:
            continue
        item = value[key]
        path = f"{prefix}.{key}" if prefix else key
        if type(item) is str and not item.strip():
            problems.append(Problem(path, "must be non-empty"))
        if type(item) is dict and field.schema is not None:
            _nonempty(item, field.schema, path, problems)


def _model_rules(model, problems, prefix="model_override"):
    if type(model.get("client_settings")) is dict and type(model.get("backend")) is str:
        try:
            validate_scalar_client_settings(model["client_settings"], model["backend"], temperature=model.get("temperature"), reasoning_effort=model.get("reasoning_effort"))
        except ValueError as exc:
            problems.extend(Problem(f"{prefix}.client_settings.{p.path}", p.message) for p in getattr(exc, "problems", ()))
    if type(model.get("backend")) is str and (model.get("reasoning_effort") is None or type(model.get("reasoning_effort")) is str):
        from defipi.execution.api.clients import validate_reasoning_effort
        try:
            validate_reasoning_effort(model["backend"], model.get("reasoning_effort"))
        except ValueError as exc:
            problems.append(Problem(f"{prefix}.reasoning_effort", str(exc)))
    if model.get("backend") == "openai" and model.get("reasoning_effort") not in (None, "none") and model.get("temperature") is not None:
        problems.append(Problem(f"{prefix}.temperature", "must be null when OpenAI reasoning is enabled"))


def validate_model_settings(model):
    def rules(data, problems):
        _nonempty(data, MODEL_FIELDS, "model_override", problems)
        _model_rules(data, problems)
    return validate_config(model, MODEL_FIELDS, rules=(rules,), prefix="model_override")


def _rules(config, problems):
    _nonempty(config, CONFIG_FIELDS, "", problems)
    if type(config.get("model_override")) is dict:
        _model_rules(config["model_override"], problems)
    server = config.get("page_server")
    if type(server) is dict and type(server.get("base_url")) is str:
        try:
            url = urlparse(server["base_url"])
            valid = url.scheme == "http" and url.hostname == "127.0.0.1" and url.port is not None
        except ValueError:
            valid = False
        if not valid:
            problems.append(Problem("page_server.base_url", "must be a loopback HTTP URL with an explicit port"))


def validate_runtime_config(config):
    return validate_config(config, CONFIG_FIELDS, rules=(_rules, _protocol_rules))


def _protocol_rules(config, problems):
    search = config.get("search")
    if type(search) is dict:
        for key, expected in (("K", 100), ("T", 500)):
            if key in search and type(search[key]) is int and search[key] != expected:
                problems.append(Problem(f"search.{key}", f"formal config requires {key}={expected}"))


def _formal_rules(config, problems):
    _rules(config, problems)
    _protocol_rules(config, problems)
    for file_field, rows_field in (("cases_file", "cases"), ("heldout_cases_file", "heldout_cases")):
        if file_field in config and config[file_field] is not None and rows_field in config:
            problems.append(Problem(rows_field, f"cannot be combined with {file_field}"))
    if config.get("cases_file") is None and "cases" not in config:
        problems.append(Problem("cases_file", "provide a cases file or explicit cases"))


def validate_paper_config(config):
    return validate_config(config, CONFIG_FIELDS, rules=(_formal_rules,))


PARAPHRASE_FIELDS = {
    "originals_file": Field(str), "output_file": Field(str),
    "request_budget": Field(int, minimum=100),
    "model_override": Field(dict, schema=MODEL_FIELDS),
}


def validate_paraphrase_config(config):
    def rules(data, problems):
        _nonempty(data, PARAPHRASE_FIELDS, "", problems)
        if type(data.get("model_override")) is dict:
            _model_rules(data["model_override"], problems)
    return validate_config(config, PARAPHRASE_FIELDS, rules=(rules,))
