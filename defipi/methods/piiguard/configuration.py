"""Closed PIIGuard configuration contracts and read-only input preflight."""
from defipi.execution.api.clients import CLIENT_SETTINGS_FIELDS, validate_scalar_client_settings
import json
import random
from pathlib import Path

from defipi.benchmark.files import load_tasks
from defipi.config import load_config_file
from defipi.configuration.schema import Field, Problem, ValidationError, validate_config
from defipi.execution.api.clients import REASONING_EFFORT_CHOICES, SUPPORTED_BACKENDS, validate_reasoning_effort
from defipi.methods.common.defense_fragments import FRAGMENT_SLOTS


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


TEXT = Field(str)
INTEGER = Field(int, minimum=1)
DOMAINS = ("academic", "news")
SLOTS = FRAGMENT_SLOTS
MODEL_FIELDS = {
    "client_settings": Field(dict, schema=CLIENT_SETTINGS_FIELDS),
    "model": TEXT,
    "provider": Field(str, choices=tuple(sorted(SUPPORTED_BACKENDS))),
    "request_parameters": Field(dict, schema={
        "temperature": Field((int, float), nullable=True, minimum=0, maximum=2),
        "max_tokens": Field(int, nullable=True, minimum=1),
        "reasoning_effort": Field(str, nullable=True, choices=REASONING_EFFORT_CHOICES),
    }),
}
SEARCH_FIELDS = {
    "rounds": INTEGER, "proposals_per_round": INTEGER, "mutation_batch": INTEGER,
    "positions": Field(list, items=Field(str, choices=SLOTS)),
    "rng_seed": Field(int), "fragment_budget_chars": Field(int, minimum=120),
    "exploration_epsilon": Field((int, float), minimum=0, maximum=1),
    "max_lineage_depth": Field(int, minimum=0), "eval_tasks_per_candidate": INTEGER,
    "child_eval_workers": INTEGER, "low_leak_threshold": Field((int, float), minimum=0, maximum=1),
}
BINDING = Field(dict, schema={"optimizer_data": TEXT, "final_tasks": TEXT})
PLAN_FIELDS = {
    "output_dir": TEXT, "prepare_only": Field(bool), "model": Field(dict, schema=MODEL_FIELDS),
    "training_prompts": TEXT, "training_base_request_id": TEXT,
    "search": Field(dict, schema=SEARCH_FIELDS),
    "execution": Field(dict, schema={"domain_order": Field(list, items=Field(str, choices=DOMAINS)),
        "api_concurrency_per_task_per_api": INTEGER, "logical_call_max_attempts": INTEGER,
        "transport_attempt_hard_cap": INTEGER}),
    "scenarios": Field(dict, values=BINDING),
}
OPTIMIZER_DATA_FIELDS = {"seeds_json": TEXT, "html_dir": TEXT}
DOMAIN_FIELDS = {
    "domain": Field(str, choices=DOMAINS), "model": Field(dict, schema=MODEL_FIELDS),
    "search": Field(dict, schema=SEARCH_FIELDS), "development_seeds": Field(list, items=Field(dict)),
    "search_ids": Field(list, items=TEXT), "supplement_ids": Field(list, items=TEXT),
    "initial_candidates": Field(list, items=Field(dict)), "request_cap": INTEGER,
    "concurrency": INTEGER, "max_attempts": INTEGER,
}
DATA_BINDING_FIELDS = {
    "source_seeds": TEXT, "saved_split": TEXT,
    "table_seed": Field(int), "html_dir": Field(str, required=False),
}
DATA_FIELDS = {
    "training_prompts": TEXT, "training_base_request_id": TEXT, "output_dir": TEXT,
    "scenarios": Field(dict, values=Field(dict, schema=DATA_BINDING_FIELDS)),
    "benchmark": Field(dict, values=TEXT),
}


def _nonempty_strings(value, path, problems):
    if type(value) is dict:
        for name, item in value.items():
            _nonempty_strings(item, f"{path}.{name}" if path else name, problems)
    elif type(value) is list:
        for index, item in enumerate(value):
            _nonempty_strings(item, f"{path}[{index}]", problems)
    elif type(value) is str and not value.strip():
        problems.append(Problem(path, "must be nonempty"))


def _common_rules(config, problems):
    _nonempty_strings(config, "", problems)
    model = config.get("model")
    if type(model) is dict and type(model.get("provider")) is str and type(model.get("request_parameters")) is dict:
        if type(model.get("client_settings")) is dict:
            try:
                validate_scalar_client_settings(model["client_settings"], model["provider"], temperature=model["request_parameters"].get("temperature"), reasoning_effort=model["request_parameters"].get("reasoning_effort"))
            except ValueError as exc:
                problems.extend(Problem(f"model.client_settings.{p.path}", p.message) for p in getattr(exc, "problems", ()))
        parameters = model["request_parameters"]
        if "reasoning_effort" in parameters and (parameters["reasoning_effort"] is None or type(parameters["reasoning_effort"]) is str):
            try:
                validate_reasoning_effort(model["provider"], parameters["reasoning_effort"])
            except ValueError as exc:
                problems.append(Problem("model.request_parameters.reasoning_effort", str(exc)))
    search = config.get("search")
    if type(search) is dict:
        positions = search.get("positions")
        if type(positions) is list and (not positions or len(set(map(str, positions))) != len(positions)):
            problems.append(Problem("search.positions", "must be nonempty and unique"))


def _plan_rules(config, problems):
    _common_rules(config, problems)
    execution, scenarios = config.get("execution"), config.get("scenarios")
    if type(execution) is dict and type(execution.get("domain_order")) is list:
        order = execution["domain_order"]
        if not order or len(set(map(str, order))) != len(order):
            problems.append(Problem("execution.domain_order", "must be nonempty and unique"))
        if type(scenarios) is dict:
            for domain in order:
                if type(domain) is str and domain not in scenarios:
                    problems.append(Problem(f"scenarios.{domain}", "required active domain is missing"))
    if type(scenarios) is dict:
        for domain in scenarios:
            if domain not in DOMAINS:
                problems.append(Problem(f"scenarios.{domain}", "unsupported domain"))


def validate_domain_config(config):
    return validate_config(config, DOMAIN_FIELDS, rules=(lambda data, problems: _common_rules(
        {key: value for key, value in data.items() if key not in {"development_seeds", "initial_candidates"}}, problems),))


def validate_data_config(config):
    def rules(data, problems):
        _common_rules(data, problems)
        scenarios, benchmark = data.get("scenarios"), data.get("benchmark")
        if type(scenarios) is dict:
            if not scenarios:
                problems.append(Problem("scenarios", "must be nonempty"))
            for domain, binding in scenarios.items():
                if domain not in DOMAINS:
                    problems.append(Problem(f"scenarios.{domain}", "unsupported domain"))
                if domain == "academic" and type(binding) is dict and "html_dir" not in binding:
                    problems.append(Problem("scenarios.academic.html_dir", "required field is missing"))
                if domain == "news" and type(binding) is dict and "html_dir" in binding:
                    problems.append(Problem("scenarios.news.html_dir", "not used for rendered news pages"))
                if type(benchmark) is dict and domain not in benchmark:
                    problems.append(Problem(f"benchmark.{domain}", "required active domain is missing"))
            if type(benchmark) is dict and set(benchmark) - set(scenarios):
                problems.append(Problem("benchmark", "must match configured scenarios"))
    return validate_config(config, DATA_FIELDS, rules=(rules,))


def partition_development_pages(seeds, search_count, rng_seed):
    """Cover training requests first, then sample extra search pages; keep the rest for reranking."""
    if not 0 < search_count < len(seeds):
        raise ValueError("eval_tasks_per_candidate must leave at least one supplement page")
    search, seen_prompts = [], set()
    for seed in seeds:
        if seed["prompt_id"] not in seen_prompts and len(search) < search_count:
            search.append(seed)
            seen_prompts.add(seed["prompt_id"])
    selected = {seed["id"] for seed in search}
    remaining = [seed for seed in seeds if seed["id"] not in selected]
    rng, extra_scenarios = random.Random(rng_seed), set()
    while len(search) < search_count:
        eligible = [seed for seed in remaining if seed["prompt_scenario_id"] not in extra_scenarios]
        if not eligible:
            eligible = remaining
        chosen = rng.choice(eligible)
        search.append(chosen)
        selected.add(chosen["id"])
        extra_scenarios.add(chosen["prompt_scenario_id"])
        remaining.remove(chosen)
    return search, [seed for seed in seeds if seed["id"] not in selected]


def load_plan_inputs(plan_path, *, root):
    """Read and validate all active files before locks, writes, or client creation."""
    root = Path(root)
    plan = validate_config(load_config_file(plan_path), PLAN_FIELDS, rules=(_plan_rules,))
    problems, prepared = [], {}
    try:
        training = _read_json(root / plan["training_prompts"])
        if type(training) is not list:
            raise ValueError("must contain a list of training prompts")
    except (OSError, ValueError, TypeError) as exc:
        problems.append(Problem("training_prompts", str(exc)))
        training = []
    for domain in plan["execution"]["domain_order"]:
        binding = plan["scenarios"][domain]
        prefix = f"scenarios.{domain}"
        try:
            data = validate_config(load_config_file(root / binding["optimizer_data"]), OPTIMIZER_DATA_FIELDS,
                                   rules=(_common_rules,), prefix=f"{prefix}.optimizer_data")
            seeds = _read_json(root / data["seeds_json"])
            if type(seeds) is not list or not seeds:
                raise ValueError("development seeds must be a nonempty list")
            from defipi.methods.piiguard.optimization import _validate_record_ids
            _validate_record_ids(seeds)
            indexed = {seed["id"]: seed for seed in seeds}
            if len(indexed) != len(seeds):
                raise ValueError("development record IDs must be unique")
            final = load_tasks(root / binding["final_tasks"])["tasks"]
            if set(indexed) & {item["metadata"]["source_record_id"] for item in final}:
                raise ValueError("Development pages overlap final pages")
            questions = [item for item in training if item["domain"] == domain
                         and item["base_request_id"] == plan["training_base_request_id"]]
            if not questions:
                raise ValueError("no configured training requests for active domain")
            by_prompt = {item["id"]: item for item in questions}
            for seed in seeds:
                if seed["question"] != by_prompt[seed["prompt_id"]]["prompt"]:
                    raise ValueError("Page/question binding changed")
            if {seed["question"] for seed in seeds} & {item["question"] for item in final}:
                raise ValueError("Development questions overlap final pages")
            search_pages, supplement_pages = partition_development_pages(
                seeds, plan["search"]["eval_tasks_per_candidate"], plan["search"]["rng_seed"])
            search_ids = [seed["id"] for seed in search_pages]
            supplement_ids = [seed["id"] for seed in supplement_pages]
            clean_html = {seed["id"]: (root / data["html_dir"] / seed["html_file"]).read_text(encoding="utf-8") for seed in seeds}
            prepared[domain] = {"seeds": seeds, "questions": questions, "search_ids": search_ids,
                                "supplement_ids": supplement_ids, "clean_html": clean_html}
        except ValidationError as exc:
            problems.extend(exc.problems)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            problems.append(Problem(prefix, str(exc)))
    if problems:
        raise ValidationError(problems)
    return plan, prepared
