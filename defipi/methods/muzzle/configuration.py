"""Closed, command-specific MUZZLE configuration contracts."""
from defipi.config import load_config_file
from defipi.configuration.schema import Field, Problem, validate_config
from defipi.configuration.execution import PI_BINDING_FIELDS, pi_target_from_role
from defipi.execution.api.clients import CLIENT_SETTINGS_FIELDS, validate_scalar_client_settings


EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
ROLE = {
    "client_settings": Field(dict, schema=CLIENT_SETTINGS_FIELDS),
    "backend": Field(str, choices=("openai", "openrouter", "deepseek")),
    "model": Field(str),
    "effort": Field(str, nullable=True, choices=EFFORTS),
    "max_output_tokens": Field(int, nullable=True, minimum=1),
    "temperature": Field((int, float), nullable=True, minimum=0, maximum=2),
    "top_p": Field((int, float), nullable=True, minimum=0, maximum=1),
}
PI_ROLE = {**ROLE, "pi_binding": Field(dict, schema=PI_BINDING_FIELDS)}
SEARCH = {
    "output": Field(str),
    "page_profile": Field(str, choices=("academic", "news")),
    "question": Field(str),
    "independent_runs": Field(int, minimum=1),
    "parallelism": Field(int, minimum=1, maximum=5),
    "max_requests": Field(int, minimum=1),
    "max_pi_requests": Field(int, minimum=1),
    "max_pi_tasks": Field(int, minimum=1),
    "browser_max_steps": Field(int, minimum=1),
    "pi_agent": Field(dict, schema={
        "container_image": Field(str),
        "max_model_requests": Field(int, minimum=1),
        "trajectory_timeout_seconds": Field(int, minimum=1),
        "agent_pool_dir": Field(str),
        "agent_pool_slots": Field(int, minimum=1, maximum=5),
        "trajectory_max_attempts": Field(int, minimum=1, maximum=3),
        "proxy_connect_timeout_seconds": Field((int, float), minimum=0.000001),
        "pi_http_idle_timeout_seconds": Field((int, float), minimum=0.000001),
        "mcp_startup_timeout_seconds": Field((int, float), minimum=0.000001),
        "mcp_tool_timeout_seconds": Field((int, float), minimum=0.000001),
        "tool_profile": Field(str, choices=("native",)),
    }),
    "safe_contact": Field(dict, schema={"label": Field(str), "email": Field(str)}),
    "data": Field(dict, schema={
        "seeds": Field(str), "saved_split": Field(str), "html_dir": Field(str),
        "developer_ids": Field(list, items=Field(str)),
    }),
    "roles": Field(dict, schema={
        "target": Field(dict, schema=PI_ROLE),
        **{name: Field(dict, schema=ROLE) for name in ("generator", "analyst", "judge")},
        "baseline": Field(dict, schema=PI_ROLE),
    }),
    "search": Field(dict, schema={
        "evaluation": Field(str, choices=("single_step", "full_task")),
        **{key: Field(int, minimum=1) for key in (
            "streams", "iterations", "keep_last_n", "max_surfaces", "validation_repeats")},
        "reflection_rounds": Field(int, minimum=0),
    }),
}
CONFIRMATION = {
    "search_output": Field(str), "output": Field(str),
    "repeats": Field(int, minimum=1),
    **{key: Field(int, minimum=1) for key in ("max_requests", "max_pi_requests", "max_pi_tasks")},
}
TRANSFER = {
    **{key: Field(str) for key in (
        "confirmation", "source_record", "pages", "output", "question")},
    "max_requests": Field(int, minimum=1),
    "roles": Field(dict, schema={"generator": Field(dict, schema=ROLE)}),
}


def read_config(source):
    return source if isinstance(source, dict) else load_config_file(source)


def _nonempty(value, problems, prefix=""):
    if type(value) is dict:
        for key, item in value.items():
            _nonempty(item, problems, f"{prefix}.{key}" if prefix else key)
    elif type(value) is list:
        for index, item in enumerate(value):
            _nonempty(item, problems, f"{prefix}[{index}]")
    elif type(value) is str and not value.strip():
        problems.append(Problem(prefix, "must be nonempty"))


def _roles(config, problems):
    roles = config.get("roles")
    if type(roles) is not dict:
        return
    for name, role in roles.items():
        if type(role) is not dict:
            continue
        if type(role.get("backend")) is str and type(role.get("client_settings")) is dict:
            try:
                validate_scalar_client_settings(role["client_settings"], role["backend"], temperature=role.get("temperature"), reasoning_effort=role.get("effort"))
            except ValueError as exc:
                problems.extend(Problem(f"roles.{name}.client_settings.{p.path}", p.message) for p in getattr(exc, "problems", ()))
        backend, model, effort = role.get("backend"), role.get("model"), role.get("effort")
        path = f"roles.{name}"
        if name in {"target", "baseline"}:
            if not all(key in role for key in ROLE) or type(model) is not str or type(backend) is not str:
                continue
            from defipi.execution.agents.pi import model_binding
            try:
                binding = model_binding(pi_target_from_role(role), container=True)
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(Problem(path, str(exc)))
                continue
            # Reasoning-capable targets must provide their own effort, rather
            # than adopting a registry default through the native adapter.
            if binding["reasoning_effort"] != "none" and effort is None:
                problems.append(Problem(f"{path}.effort", "must be explicit for this Pi model"))
            if name == "target" and type(config.get("search")) is dict:
                if config["search"].get("evaluation") == "single_step" and binding["api"] != "openai-responses":
                    problems.append(Problem(path, "single_step requires the Luna Responses binding"))
        elif backend == "openrouter" and name == "judge":
            for key in ("effort", "top_p"):
                if role.get(key) is not None:
                    problems.append(Problem(f"{path}.{key}", "must be null for the OpenRouter Judge"))
        elif backend != "openai":
            problems.append(Problem(f"{path}.backend", "requires OpenAI Responses, except the OpenRouter Judge"))
        elif effort not in EFFORTS:
            problems.append(Problem(f"{path}.effort", "must be an explicit reasoning effort"))
        if type(model) is str and model.startswith(("gpt-6-sol", "gpt-6-luna")) and effort != "none":
            for key in ("temperature", "top_p"):
                if role.get(key) is not None:
                    problems.append(Problem(f"{path}.{key}", "must be null when reasoning effort is not none"))


def _developer_ids(config, problems):
    data = config.get("data")
    ids = data.get("developer_ids") if type(data) is dict else None
    if type(ids) is list and (not ids or any(type(i) is not str for i in ids) or len(set(ids)) != len(ids)):
        problems.append(Problem("data.developer_ids", "select unique, nonempty developer IDs"))


def validate_search(config):
    return validate_config(config, SEARCH, rules=(_nonempty, _roles, _developer_ids))


def validate_confirmation(config):
    return validate_config(config, CONFIRMATION, rules=(_nonempty,))


def validate_transfer(config):
    return validate_config(config, TRANSFER, rules=(_nonempty, _roles))
