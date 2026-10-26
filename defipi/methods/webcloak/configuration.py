"""WebCloak configuration contracts, with requirements selected by workflow mode."""
from defipi.execution.api.clients import CLIENT_SETTINGS_FIELDS, validate_scalar_client_settings
from defipi.configuration.schema import Field, Problem, validate_config
from defipi.evaluation.llm_filter import FILTER_PROMPT_VARIANTS
from defipi.execution.api.clients import SUPPORTED_BACKENDS, REASONING_EFFORT_CHOICES, validate_reasoning_effort

ROLE_FIELDS = {
    "max_retries": Field(int, minimum=1), "retry_backoff_seconds": Field((int, float), minimum=0),
    "client_settings": Field(dict, schema=CLIENT_SETTINGS_FIELDS),
    "backend": Field(str, choices=tuple(sorted(SUPPORTED_BACKENDS))), "model": Field(str),
    "effort": Field(str, nullable=True, choices=REASONING_EFFORT_CHOICES),
    "temperature": Field((int, float), nullable=True, minimum=0),
    "max_tokens": Field(int, nullable=True, minimum=1),
}
SEARCH_CHANNELS = ("raw", "mixed", "canonical")
EVALUATION_CHANNELS = ("raw",) + tuple(v for v in FILTER_PROMPT_VARIANTS if v != "mixed")
CONDITIONS = ("ordinary", "refined", "knowledgeable")


def validate_role(role):
    return validate_config(role, ROLE_FIELDS, rules=(_nonempty, _reasoning))


def _nonempty(data, problems):
    if "client_settings" in data and type(data.get("backend")) is str:
        try:
            validate_scalar_client_settings(data["client_settings"], data["backend"], temperature=data.get("temperature"), reasoning_effort=data.get("effort"))
        except ValueError as exc:
            problems.extend(getattr(exc, "problems", ()))
    for key, value in data.items():
        if type(value) is str and not value.strip():
            problems.append(Problem(key, "must be non-empty"))


def _reasoning(role, problems):
    backend, effort = role.get("backend"), role.get("effort")
    if type(backend) is str and backend in SUPPORTED_BACKENDS and (effort is None or type(effort) is str):
        try:
            validate_reasoning_effort(backend, effort)
        except ValueError as exc:
            problems.append(Problem("effort", str(exc)))


def _rules(data, problems):
    _nonempty(data, problems)
    for section in ("data", "search", "evaluation", "roles", "artifacts"):
        value = data.get(section)
        if type(value) is not dict:
            continue
        for key, item in value.items():
            path = f"{section}.{key}"
            if type(item) is str and not item.strip():
                problems.append(Problem(path, "must be non-empty"))
            if type(item) is list:
                if not item:
                    problems.append(Problem(path, "must be non-empty"))
                for index, entry in enumerate(item):
                    if type(entry) is str and not entry.strip():
                        problems.append(Problem(f"{path}[{index}]", "must be non-empty"))
                if all(type(x) is str for x in item) and len(set(item)) != len(item):
                    problems.append(Problem(path, "must contain unique values"))
            roles = item if key == "search_evaluators" and type(item) is list else ([item] if section == "roles" else [])
            for index, role in enumerate(roles):
                if type(role) is dict:
                    suffix = f"[{index}]" if key == "search_evaluators" else ""
                    role_problems = []
                    _nonempty(role, role_problems)
                    _reasoning(role, role_problems)
                    problems.extend(Problem(f"{path}{suffix}.{p.path}", p.message) for p in role_problems)
                    for name in ("backend", "model", "effort"):
                        if type(role.get(name)) is str and not role[name].strip():
                            suffix = f"[{index}]" if key == "search_evaluators" else ""
                            problems.append(Problem(f"{path}{suffix}.{name}", "must be non-empty"))
    roles = data.get("roles")
    evaluators = roles.get("search_evaluators") if type(roles) is dict else None
    if type(evaluators) is list:
        if len(evaluators) != 2:
            problems.append(Problem("roles.search_evaluators", "must contain exactly two models"))
        elif all(type(r) is dict and type(r.get("backend")) is str and type(r.get("model")) is str for r in evaluators):
            if len({(r["backend"], r["model"]) for r in evaluators}) != 2:
                problems.append(Problem("roles.search_evaluators", "must use two distinct models"))


def validate_method_config(config, *, mode):
    if mode not in ("validate", "optimize", "prepare", "evaluate"):
        raise ValueError(f"unsupported WebCloak mode: {mode}")
    optimize = mode in ("validate", "optimize")
    prepare = mode == "prepare"
    evaluate = mode in ("validate", "evaluate")
    evaluation = mode in ("validate", "prepare", "evaluate")
    search = config.get("search") if type(config) is dict else None
    eval_config = config.get("evaluation") if type(config) is dict else None
    filtered_search = optimize and type(search) is dict and search.get("channel") in ("mixed", "canonical")
    filtered_eval = evaluate and type(eval_config) is dict and type(eval_config.get("channels")) is list and any(c != "raw" for c in eval_config["channels"])
    fields = {
        "page_profile": Field(str, choices=("academic", "news")), "question": Field(str),
        "data": Field(dict, schema={
            "seeds": Field(str, required=optimize or prepare),
            "saved_split": Field(str, required=optimize or prepare),
            "html_dir": Field(str),
            "developer_ids": Field(list, required=optimize, items=Field(str)),
            "prepare_ids": Field(list, required=prepare, items=Field(str)),
        }),
        "roles": Field(dict, required=optimize or evaluate, schema={
            "generator": Field(dict, required=optimize, schema=ROLE_FIELDS),
            "search_evaluators": Field(list, required=optimize, items=Field(dict, schema=ROLE_FIELDS)),
            "evaluator": Field(dict, required=evaluate, schema=ROLE_FIELDS),
            "filter": Field(dict, required=filtered_search or filtered_eval, schema=ROLE_FIELDS),
        }),
        "search": Field(dict, required=optimize or prepare, schema={
            "channel": Field(str, required=optimize, choices=SEARCH_CHANNELS),
            **{name: Field(int, required=optimize, minimum=minimum) for name, minimum in (
                ("depth", 1), ("branch", 1), ("initial_word_count", 0), ("increase", 0), ("batch_size", 1), ("concurrency", 1))},
            "rng_seed": Field(int, required=optimize or prepare),
        }),
        "artifacts": Field(dict, schema={
            "search": Field(str, required=optimize or prepare),
            "prepared": Field(str, required=mode in ("validate", "prepare", "evaluate")),
            "evaluation": Field(str, required=evaluate),
        }),
        "evaluation": Field(dict, required=evaluation, schema={
            "channels": Field(list, items=Field(str, choices=EVALUATION_CHANNELS)),
            "attacker_conditions": Field(list, items=Field(str, choices=CONDITIONS)),
        }),
    }
    return validate_config(config, fields, rules=(_rules,))
