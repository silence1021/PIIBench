"""Publisher-side MUZZLE adaptation: observe, probe, PAIR, execute, reflect."""

from __future__ import annotations

from defipi.benchmark.method_splits import load_method_split

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT, git_revision

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import threading

from defipi.config import load_config_file
from defipi.evaluation.weighted_recoverability import SCORING_VERSION
from defipi.academic_pages.layout import ACADEMIC_LAYOUT_VERSION
from defipi.methods.muzzle.browser import (
    BudgetExceeded,
    FIELDS,
    JOINT_WEIGHT,
    PROMPTS,
    ResponseGateway,
    model_request,
    response_text,
    write_json,
)
from defipi.inputs.contact_pages import extract_page

ROOT = _PACKAGE_ROOT
# Evaluation/output protocol, separate from the author adapter's local implementation_version.
PROTOCOL_VERSION = "muzzle-pii-full-browser-v2"
POSITION_MECHANISM = "trajectory-grafter-dispatcher-v1"
INSERTION_ADAPTER = "publisher-html-insertion-v1"
PROBE = "MUZZLE_PII_INSTRUCTION_PLACEHOLDER_20260926"


def _path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


from defipi.methods.muzzle.injection import _v2_anchor


def validate_saved_surface(surface):
    if not isinstance(surface, dict) or not isinstance(surface.get("selector"), str) or not surface["selector"].strip():
        raise ValueError("Saved placement is missing its selector")
    if surface.get("adapter_version") != INSERTION_ADAPTER or surface.get("placement") not in {"before", "after", "prepend", "append"}:
        raise ValueError("Saved placement is missing its versioned insertion operation")
    return surface


def validate_config(config):
    from defipi.configuration.schema import Problem, ValidationError
    from defipi.methods.muzzle.configuration import validate_search
    validate_search(config)
    page_profile = config["page_profile"]
    baseline_role = config["roles"]["baseline"]
    data, search = config["data"], config["search"]
    problems, records, pages = [], [], {}
    seeds = split = None
    try:
        seeds = json.loads(_path(data["seeds"]).read_text())
        if not isinstance(seeds, list):
            raise ValueError("expected a list of seed records")
    except (OSError, ValueError) as exc:
        problems.append(Problem("data.seeds", str(exc)))
    try:
        split = load_method_split(_path(data["saved_split"]), "muzzle")
        if not isinstance(split["heldout_record_ids"], list) or not isinstance(split["eval_record_ids"], list):
            raise ValueError("expected saved heldout and developer ID lists")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        problems.append(Problem("data.saved_split", str(exc)))
        split = None
    ids = data["developer_ids"]
    if split is not None and (set(ids) & set(split["heldout_record_ids"]) or not set(ids) <= set(split["eval_record_ids"])):
        problems.append(Problem("data.developer_ids", "search pages must be from the saved developer split"))
    if seeds is not None:
        try:
            by_id = {r["id"]: r for r in seeds}
        except (KeyError, TypeError) as exc:
            problems.append(Problem("data.seeds", f"invalid seed record: {exc}"))
        else:
            for record_id in ids:
                if record_id not in by_id:
                    problems.append(Problem("data.developer_ids", f"unknown seed record {record_id!r}"))
                    continue
                record = by_id[record_id]
                try:
                    html = (_path(data["html_dir"]) / record["html_file"]).read_text()
                    extract_page(html, record, page_profile=page_profile)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    problems.append(Problem(f"data.html_dir[{record_id}]", str(exc)))
                    continue
                records.append(record)
                pages[record_id] = html
    if problems:
        raise ValidationError(problems)
    from defipi.methods.muzzle.author import (
        IMPLEMENTATION_VERSION as AUTHOR_IMPLEMENTATION_VERSION,
        SCAFFOLD_VERSION as AUTHOR_SCAFFOLD_VERSION,
    )
    metadata = {"protocol_version": PROTOCOL_VERSION, "scoring_version": SCORING_VERSION,
                "layout_version": ACADEMIC_LAYOUT_VERSION if page_profile == "academic" else "news-semantic-singlecol-v1",
                "page_profile": page_profile, "upstream": "third_party/muzzle",
                "developer_ids": ids, "stage": "training_search",
                "max_requests": config["max_requests"], "search": search,
                "independent_runs": config["independent_runs"],
                "parallelism": config["parallelism"],
                "target_runtime": "pi_agent", "orchestrator": "author_autogen",
                "position_mechanism": POSITION_MECHANISM, "insertion_adapter": INSERTION_ADAPTER,
                "browser_budget_unit": "tool_calls",
                "shared_pi_trajectory_and_single_step_slots": config["pi_agent"]["agent_pool_slots"],
                "max_pi_tasks": config["max_pi_tasks"], "max_pi_requests": config["max_pi_requests"],
                "implementation_version": AUTHOR_IMPLEMENTATION_VERSION,
                "scaffold_version": AUTHOR_SCAFFOLD_VERSION,
                "pair_selection": "author_score_10_only", "objective_trajectory": "separately_executed_pi_task",
                "analysis_input": "steps_plus_last_request"}
    if search["evaluation"] == "single_step":
        metadata["single_step_runtime"] = "author_text_template_with_task"
    metadata.update(baseline_role=baseline_role, baseline_role_scope="initial_original_page_only",
                    confirmation_target_role="target")
    return records, pages, metadata


def validate_grafter(selection):
    if not isinstance(selection, dict) or set(selection) != {"reasoning", "candidates"}:
        raise ValueError("Grafter must return reasoning and candidates")
    if not isinstance(selection["reasoning"], str) or not selection["reasoning"].strip():
        raise ValueError("Grafter reasoning must be nonempty")
    candidates = selection["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 3:
        raise ValueError("Grafter candidates must contain zero to three proposals")
    ranked = []
    for index, proposal in enumerate(candidates):
        if not isinstance(proposal, dict) or set(proposal) != {"element", "why", "how", "confidence"}:
            raise ValueError("Grafter proposal schema is invalid")
        if any(not isinstance(proposal[k], str) or not proposal[k].strip() for k in ("element", "why", "how")):
            raise ValueError("Grafter proposal text must be nonempty")
        confidence = proposal["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Grafter confidence must be finite and between zero and one")
        ranked.append({**proposal, "proposal_id": f"position-{index+1:03d}", "model_order": index})
    ranked.sort(key=lambda item: -item["confidence"])
    return ranked


def resolve_dispatch(soup, proposal, result, seen):
    if not isinstance(result, dict) or set(result) != {"status", "selector", "placement", "reason"}:
        raise ValueError("Dispatcher result schema is invalid")
    status, selector, placement = result["status"], result["selector"], result["placement"]
    if not isinstance(result["reason"], str) or not result["reason"].strip():
        raise ValueError("Dispatcher reason must be nonempty")
    if status == "unavailable":
        if selector is not None or placement is not None:
            raise ValueError("Unavailable Dispatcher result must not include a placement")
        return {"id": proposal["proposal_id"], **{k: proposal[k] for k in ("element", "why", "how", "confidence")},
                "selector": None, "placement": None, "adapter_version": INSERTION_ADAPTER,
                "dispatcher": copy.deepcopy(result), "status": "unavailable"}
    if status != "ready" or not isinstance(selector, str) or not selector.strip() or placement not in {"before", "after", "prepend", "append"}:
        raise ValueError("Ready Dispatcher result has an invalid selector or placement")
    nodes = soup.select(selector)
    if len(nodes) != 1:
        raise ValueError("Dispatcher selector is missing or ambiguous")
    surface = {"id": proposal["proposal_id"], **{k: proposal[k] for k in ("element", "why", "how", "confidence")},
               "selector": selector, "placement": placement, "adapter_version": INSERTION_ADAPTER,
               "dispatcher": copy.deepcopy(result), "status": "ready"}
    _v2_anchor(soup, surface)
    key = (selector, placement)
    if key in seen:
        raise ValueError("Dispatcher repeated an existing selector and placement")
    seen.add(key)
    return surface


def aggregate(traces):
    valid = [t for t in traces if t["status"] == "valid"]
    n = len(valid)
    return {"planned": len(traces), "valid": n, "invalid": len(traces)-n,
            "field_denominator": 3*n,
            "field_hits": {f: sum(t["score"]["matched_fields"][f] for t in valid) for f in FIELDS},
            "name_hits": sum(t["score"]["matched_fields"]["name"] for t in valid),
            "D": sum(t["score"]["D"] for t in valid)/n if n else None,
            "J": sum(t["score"]["J"] for t in valid)/n if n else None,
            "L": sum(t["score"]["L"] for t in valid)/n if n else None}


class MuzzlePII:
    def __init__(self, config, output_dir, gateway, *, browser_runner, browser_slots=None,
                 replay_runner=None):
        self.config = config
        self.output_dir = Path(output_dir)
        self.gateway = gateway
        self.browser_slots = browser_slots or threading.BoundedSemaphore(config["parallelism"])
        self.browser_runner = browser_runner
        self.replay_runner = replay_runner

    def _json(self, prompt_name, inputs, *, purpose, role=None, history=None):
        if role is None:
            role = "judge" if prompt_name in {"proxy_judge", "judge"} else "analyst"
        conversation = list(history or []) + [{
            "role": "user", "content": "Return one JSON object.\n" + json.dumps(inputs, ensure_ascii=False)}]
        request = model_request(self.config["roles"][role], conversation,
                                (PROMPTS / f"{prompt_name}.txt").read_text(),
                                text={"format": {"type": "json_object"}})
        response = self.gateway.call(request, purpose=f"{self.output_dir.name}:{purpose}")
        if response.get("status") != "completed":
            raise ValueError(f"Incomplete {purpose} response")
        value = json.loads(response_text(response))
        if not isinstance(value, dict):
            raise ValueError(f"{purpose} must return a JSON object")
        return value


def run(config_path, output_dir):
    config = copy.deepcopy(config_path) if isinstance(config_path, dict) else load_config_file(config_path)
    records, pages, metadata = validate_config(config)
    from defipi.methods.muzzle.author import AuthorMuzzlePII
    output_dir = Path(output_dir).resolve()
    if output_dir.exists():
        raise ValueError("Use a new output directory for this bounded run")
    output_dir.mkdir(parents=True)
    write_json(output_dir / "config.json", config)
    write_json(output_dir / "protocol.json", {**metadata, "code_revision": git_revision(),
                                            "roles": config["roles"],
                                            "question": config["question"],
                                            "tools": "Pi builtin:mcp Playwright tools and browser-artifact read"})
    parallelism = config["parallelism"]
    run_count = config["independent_runs"]
    gateway = ResponseGateway(output_dir, config["max_requests"], roles=config["roles"], parallelism=parallelism)
    browser_slots = threading.BoundedSemaphore(parallelism)
    from defipi.methods.muzzle.pi import PiBrowserRunner
    desktop = PiBrowserRunner(output_dir, config)
    result = {**metadata, "status": "running", "pages": [], "runs": []}
    write_json(output_dir / "result.json", result)

    def run_one(index, native):
        directory = output_dir if run_count == 1 else output_dir / f"run-{index+1:02d}"
        child = {**metadata, "run_index": index+1, "status": "running", "pages": [],
                 "shared_usage_file": str(output_dir / "usage.json")}
        child_config = copy.deepcopy(config)
        child_config["independent_runs"] = 1
        if run_count > 1:
            directory.mkdir()
            write_json(directory / "config.json", child_config)
        engine = AuthorMuzzlePII(child_config, directory, gateway, browser_slots=browser_slots,
                                 browser_runner=native.run_browser, replay_runner=native.replay)
        error = None
        for record in records:
            try:
                print(f"run-{index+1:02d} {record['slug']}: reconnaissance", flush=True)
                child["pages"].append(engine.run_page(record, pages[record["id"]]))
            except Exception as exc:
                error = exc
                child.update(status="budget_exhausted" if isinstance(exc, BudgetExceeded) else "error",
                             error=f"{type(exc).__name__}: {exc}")
                page_checkpoint = directory / record["slug"] / "result.json"
                if page_checkpoint.is_file():
                    partial = json.loads(page_checkpoint.read_text())
                    partial.update(status=child["status"], error=child["error"])
                    write_json(page_checkpoint, partial)
                    child["pages"].append(partial)
                break
            finally:
                write_json(directory / "result.json", child)
        if not error:
            child["status"] = ("completed" if all(p["status"] in {"completed", "baseline_already_zero"}
                                                   for p in child["pages"]) else "completed_with_invalid_pages")
        write_json(directory / "result.json", child)
        return child, error

    errors = []
    try:
        with desktop as native, ThreadPoolExecutor(max_workers=run_count) as pool:
            futures = [pool.submit(run_one, index, native) for index in range(run_count)]
            try:
                for future in as_completed(futures):
                    child, error = future.result()
                    result["runs"].append(child)
                    result["runs"].sort(key=lambda r: r["run_index"])
                    if error:
                        errors.append(error)
                    # A one-run result keeps the existing page/confirmation interface.
                    if run_count == 1:
                        result["pages"] = child["pages"]
                    if run_count > 1:
                        write_json(output_dir / "result.json", result)
            except BaseException as exc:
                if hasattr(gateway, "stopped"):
                    gateway.stopped.set()
                native.stopped.set()
                result.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                              error=f"{type(exc).__name__}: {exc}", usage=gateway.usage)
                write_json(output_dir / "result.json", result)
                raise
            result["usage"] = gateway.usage
            result["pi_usage"] = copy.deepcopy(native.usage)
            result["status"] = ("budget_exhausted" if any(isinstance(e, BudgetExceeded) for e in errors)
                                else "error" if errors else "completed" if all(r["status"] == "completed" for r in result["runs"])
                                else "completed_with_invalid_pages")
            write_json(output_dir / "result.json", result)
    except BaseException as exc:
        if result["status"] == "running":
            result.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                          error=f"{type(exc).__name__}: {exc}", usage=gateway.usage)
            write_json(output_dir / "result.json", result)
        raise
    if errors:
        raise errors[0]
    return result


def confirm(config_path):
    """Freeze the search winner using an explicit confirmation entry config."""
    from defipi.methods.muzzle.configuration import read_config, validate_confirmation
    entry = validate_confirmation(read_config(config_path))
    repeats, max_requests = entry["repeats"], entry["max_requests"]
    source, output_dir = _path(entry["search_output"]).resolve(), _path(entry["output"]).resolve()
    search_result = json.loads((source / "result.json").read_text())
    if search_result["status"] not in {"completed", "budget_exhausted"}:
        raise ValueError("Confirmation requires a completed or budget-exhausted search with a validated winner")
    if not search_result["pages"]:
        raise ValueError("Select one run-NN directory for confirmation of an independent search")
    config = load_config_file(source / "config.json")
    config.update({key: entry[key] for key in ("max_requests", "max_pi_requests", "max_pi_tasks", "output")})
    from defipi.methods.muzzle.pi import PiBrowserRunner
    records, _, metadata = validate_config(config)
    by_id = {r["id"]: r for r in records}
    if output_dir.exists():
        raise ValueError("Confirmation requires a new output directory")
    frozen = []
    for page in search_result["pages"]:
        best = page.get("best")
        if best is None:
            raise ValueError(f"No validated candidate for {page['record_id']}")
        record = by_id[page["record_id"]]
        base_html = (source / record["slug"] / "base.html").read_text()
        candidate_html = Path(best["html_path"]).read_text()
        validate_saved_surface(best["surface"])
        frozen.append((record, base_html, candidate_html, best["candidate"]["prompt"]))
    output_dir.mkdir(parents=True)
    gateway = ResponseGateway(output_dir, max_requests, roles=config["roles"], parallelism=config["parallelism"])
    result = {**metadata, "stage": "frozen_candidate_same_page_confirmation",
              "source_search": str(source), "status": "running", "repeats": repeats,
              "source_search_status": search_result["status"],
              "max_requests": max_requests, "roles": config["roles"],
              "question": config["question"], "pages": []}
    write_json(output_dir / "entry_config.json", entry)
    write_json(output_dir / "config.json", config)
    write_json(output_dir / "protocol.json", {**metadata, "code_revision": git_revision(),
                                            "roles": config["roles"], "question": config["question"],
                                            "stage": "frozen_candidate_same_page_confirmation",
                                            "repeats": repeats, "max_requests": max_requests})
    native = PiBrowserRunner(output_dir, config)
    native.__enter__()
    browser_runner = native.run_browser
    try:
        for record, base_html, candidate_html, payload in frozen:
            page_dir = output_dir / record["slug"]
            page_dir.mkdir()
            (page_dir / "base.html").write_text(base_html, encoding="utf-8")
            (page_dir / "selected.html").write_text(candidate_html, encoding="utf-8")
            page = {"record_id": record["id"], "runs": [], "arms": {}}
            result["pages"].append(page)
            arms = {"baseline": [], "defended": []}
            for repeat in range(repeats):
                order = ("baseline", "defended") if repeat % 2 == 0 else ("defended", "baseline")
                for arm in order:
                    print(f"confirmation {record['slug']} repeat={repeat+1} arm={arm}", flush=True)
                    trace = browser_runner(
                        gateway, config["roles"]["target"], html_root=_path(config["data"]["html_dir"]),
                        record=record, html=base_html if arm == "baseline" else candidate_html,
                        question=config["question"], output_dir=page_dir / f"{arm}-{repeat+1}",
                        max_steps=config["browser_max_steps"], joint_weight=JOINT_WEIGHT,
                        payload=payload if arm == "defended" else None)
                    arms[arm].append(trace)
                    page["runs"].append({"repeat": repeat+1, "arm": arm, "trace_path": trace["trace_path"]})
                    page["arms"] = {name: aggregate(items) for name, items in arms.items()}
                    write_json(output_dir / "result.json", result)
            for stats in page["arms"].values():
                stats["planned"] = repeats
            write_json(output_dir / "result.json", result)
        result["status"] = ("completed" if all(stats["invalid"] == 0 for page in result["pages"]
                                                for stats in page["arms"].values()) else "completed_with_invalid_runs")
    except Exception as exc:
        result.update(status="budget_exhausted" if isinstance(exc, BudgetExceeded) else "error",
                      error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        native.__exit__(None, None, None)
        result["pi_usage"] = copy.deepcopy(native.usage)
        result["usage"] = gateway.usage
        for page in result["pages"]:
            for stats in page["arms"].values():
                stats["not_run"] = repeats - stats["planned"]
                stats["planned"] = repeats
        write_json(output_dir / "result.json", result)
    return result
