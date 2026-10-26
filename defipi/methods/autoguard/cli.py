"""Explicit planning, data preparation, paid search, and heldout evaluation CLI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from defipi.config import load_config_file, load_local_env

from defipi.methods.autoguard.configuration import validate_paper_config, validate_paraphrase_config
from defipi.methods.autoguard.data import import_seed_rows, read_rows, validate_cases
from defipi.methods.autoguard.paraphrase import prepare_paraphrases
from defipi.methods.autoguard.search import AutoGuardSearch
from defipi.methods.autoguard.scoring import TRAINING_OBJECTIVE
from defipi.methods.autoguard.runtime import (
    model_client_factory,
    runtime_contract,
    search_workers,
)
from defipi.methods.autoguard.pages import serve_bound_pages


def load_config(path):
    path = Path(path).resolve()
    config = load_config_file(path)
    validate_paper_config(config)
    base = path.parent
    config["config_source"] = str(path)
    for field in ("cases_file", "heldout_cases_file"):
        if field in config and config[field] is not None:
            resolved = (base / config[field]).resolve()
            config[field] = str(resolved)
            config[field.removesuffix("_file")] = read_rows(resolved)
    if "cases" not in config:
        config["cases"] = []
    if "heldout_cases" in config:
        config["cases"].extend(config["heldout_cases"])
    config["output_dir"] = str((base / config["output_dir"]).resolve())
    config["limits"]["agent_slot_directory"] = str((base / config["limits"]["agent_slot_directory"]).resolve())
    if "page_server" in config and config["page_server"] is not None:
        config["page_server"]["html_root"] = str((base / config["page_server"]["html_root"]).resolve())
    return config


def _search(config):
    validate_cases(config["cases"], require_paper_cardinality=True)
    return AutoGuardSearch(config, cases=config["cases"], model_client_factory=model_client_factory(config))


def command_plan(args):
    cfg = load_config(args.config)
    search_cfg = cfg["search"]
    step_budget = cfg["browser"]["episode_step_budget"]
    attempt_budget = cfg["limits"]["discovery_attempt_budget"]
    conditional = None
    if attempt_budget is not None and step_budget is not None:
        conditional = {"nominal_discovery_action_step_bound": attempt_budget * step_budget,
                       "nominal_discovery_scorer_judgments": attempt_budget,
                       "defender_or_refiner_top_level_calls": attempt_budget,
                       "max_target_http_requests": attempt_budget * step_budget * 3 * 3,
                       "max_generation_http_requests": attempt_budget * 3,
                       "max_scorer_http_requests": attempt_budget * 3,
                       "episode_max_attempts": 3,
                       "request_max_attempts_including_parse_retries": 3}
    plan = {
        "phase1": {"success_library_size_K": search_cfg["K"],
                   "proposal_cap": None,
                   "explicit_operational_attempt_budget": attempt_budget,
                   "discovery_demand": "unknown; paper specifies no proposal/refinement cap",
                   "conditional_upper_bound": conditional},
        "phase2": {"rounds_T": search_cfg["T"], "instances_per_round": 10,
                   "target_trajectories": 5000, "training_scorer_judgments": 5000,
                   "max_target_http_requests": 5000 * step_budget * 3 * 3 if step_budget else None,
                   "max_training_scorer_http_requests": 5000 * 3,
                   "per_episode_step_budget": step_budget,
                   "final_judges": "separate A.4/A.5 judgments; up to trajectory-step count, currently unbounded"},
        "output_token_budget": cfg["model_override"]["max_tokens"],
        "full_paid_execution_authorized": False,
        "provider_clients_constructed": False,
        "paid_calls_made": 0,
        "runtime_contract": runtime_contract(),
        "search_workers": search_workers(cfg),
    }
    try:
        validate_cases(cfg["cases"], require_paper_cardinality=True)
        data_ready, data_error = True, None
    except Exception as exc:
        data_ready, data_error = False, f"{type(exc).__name__}: {exc}"
    plan.update({"command": "plan", "roles": ["defender", "refiner", "target", "training_scorer", "final_refusal_judge", "attribution_judge", "paraphraser"], "training_objective": TRAINING_OBJECTIVE,
                 "training_cases": {split: sum(1 for x in cfg["cases"] if x.get("split") == split)
                                    for split in ("discovery", "bandit", "heldout")},
                 "formal_data_ready": data_ready, "data_validation_error": data_error,
                 "data_note": "Training requires 100+100 saved pattern expansions. Held-out questions are separate and required only for evaluation.",
                 "unique_training_pages": len({row.get("page_id") for row in cfg["cases"] if row.get("split") in {"discovery", "bandit"}})})
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def command_prepare(args):
    seeds = read_rows(args.seeds)
    mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    rows = import_seed_rows(seeds, mapping)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"written": len(rows), "output": str(out),
                      "questions_preserved_verbatim": True, "model_calls": 0}, ensure_ascii=False))
    return 0


def command_paraphrase(args):
    path = Path(args.config).resolve()
    cfg = validate_paraphrase_config(load_config_file(path))
    originals = read_rows((path.parent / cfg["originals_file"]).resolve())
    result = prepare_paraphrases(originals, output=(path.parent / cfg["output_file"]).resolve(),
                                 request_budget=cfg["request_budget"], paid_authorized=args.authorize_paid_calls,
                                 client_factory=model_client_factory(cfg), model=cfg["model_override"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("complete") else 2


def command_discover(args):
    cfg = load_config(args.config)
    runner = _search(cfg)
    with serve_bound_pages(cfg):
        result = runner.discover(attempt_budget=_budget(cfg, "discovery_attempt_budget"),
                                 step_budget=_budget(cfg, "episode_step_budget"), paid_authorized=args.authorize_paid_calls)
    print(json.dumps({"complete": result.get("complete"), "library_size": len(result.get("library", [])),
                      "attempts": result.get("attempts"), "paused": result.get("paused"),
                      "pause_reason": result.get("pause_reason")}, ensure_ascii=False, indent=2))
    return 0


def command_train(args):
    cfg = load_config(args.config)
    runner = _search(cfg)
    with serve_bound_pages(cfg):
        result = runner.train(step_budget=_budget(cfg, "episode_step_budget"), paid_authorized=args.authorize_paid_calls)
    print(json.dumps({"complete": result.get("complete"), "paused": result.get("paused"),
                      "pause_reason": result.get("pause_reason"), "selected": result.get("selected"),
                      "winners": result.get("winners")}, ensure_ascii=False, indent=2, default=str))
    return 0


def command_search(args):
    cfg = load_config(args.config)
    runner = _search(cfg)
    with serve_bound_pages(cfg):
        return _run_search(args, cfg, runner)


def _run_search(args, cfg, runner):
    discovery = runner.discover(attempt_budget=_budget(cfg, "discovery_attempt_budget"),
                                step_budget=_budget(cfg, "episode_step_budget"),
                                paid_authorized=args.authorize_paid_calls)
    if not discovery.get("complete"):
        print(json.dumps({"complete": False, "stage": "discovery", "library_size": len(discovery.get("library", [])),
                          "pause_reason": discovery.get("pause_reason")}, ensure_ascii=False, indent=2))
        return 0
    result = runner.train(step_budget=_budget(cfg, "episode_step_budget"), paid_authorized=args.authorize_paid_calls)
    print(json.dumps({"complete": result.get("complete"), "stage": "bandit",
                      "selected": result.get("selected"), "winners": result.get("winners")},
                     ensure_ascii=False, indent=2, default=str))
    return 0


def command_evaluate(args):
    cfg = load_config(args.config)
    heldout = [case for case in cfg["cases"] if case["split"] == "heldout"]
    if not heldout:
        raise ValueError("evaluation requires a separate heldout_cases_file of original questions")
    runner = _search(cfg)
    with serve_bound_pages(cfg):
        result = runner.evaluate(heldout_cases=heldout, step_budget=_budget(cfg, "episode_step_budget"),
                                 paid_authorized=args.authorize_paid_calls, baseline=args.baseline)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _budget(config, name):
    section = "browser" if name == "episode_step_budget" else "limits"
    return config[section][name]


def command_bind(args):
    from defipi.methods.autoguard.binding import bind_training_cases
    result = bind_training_cases(prompt_dir=args.prompts, source_config=args.source_config,
                                 final_tasks=args.final_tasks, base_url=args.base_url, output_dir=args.output,
                                 page_profile=args.page_profile)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    load_local_env()
    parser = argparse.ArgumentParser(prog="autoguard-pii-reproduction")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="inspect bounds without constructing a provider client")
    plan.add_argument("--config", required=True)
    plan.set_defaults(handler=command_plan)
    prepare = commands.add_parser("prepare-seeds", help="explicitly map local seed records; never generates paraphrases")
    prepare.add_argument("--seeds", required=True)
    prepare.add_argument("--mapping", required=True)
    prepare.add_argument("--output", required=True)
    prepare.set_defaults(handler=command_prepare)
    bind = commands.add_parser("bind-training", help="offline binding of saved prompts to existing development pages")
    bind.add_argument("--prompts", required=True)
    bind.add_argument("--source-config", required=True)
    bind.add_argument("--final-tasks", required=True)
    bind.add_argument("--base-url", required=True)
    bind.add_argument("--output", required=True)
    bind.add_argument("--page-profile", choices=("academic", "news"), required=True)
    bind.set_defaults(handler=command_bind)
    paraphrase = commands.add_parser("prepare-paraphrases", help="explicitly generate and freeze 100 disjoint training paraphrases per split")
    paraphrase.add_argument("--config", required=True)
    paraphrase.add_argument("--authorize-paid-calls", action="store_true")
    paraphrase.set_defaults(handler=command_paraphrase)
    discover = commands.add_parser("discover")
    discover.add_argument("--config", required=True)
    discover.add_argument("--authorize-paid-calls", action="store_true")
    discover.set_defaults(handler=command_discover)
    train = commands.add_parser("train")
    train.add_argument("--config", required=True)
    train.add_argument("--authorize-paid-calls", action="store_true")
    train.set_defaults(handler=command_train)
    search = commands.add_parser("search")
    search.add_argument("--config", required=True)
    search.add_argument("--authorize-paid-calls", action="store_true")
    search.set_defaults(handler=command_search)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--config", required=True)
    evaluate.add_argument("--authorize-paid-calls", action="store_true")
    evaluate.add_argument("--baseline", action="store_true", help="run clean no-payload baseline on the same originals")
    evaluate.set_defaults(handler=command_evaluate)
    args = parser.parse_args(argv)
    try:
        if args.command not in {"plan", "prepare-seeds", "bind-training"} and not args.authorize_paid_calls:
            raise PermissionError("paid execution requires --authorize-paid-calls")
        return args.handler(args)
    except Exception as exc:
        print(f"autoguard-pii-reproduction: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
