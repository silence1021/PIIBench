"""CLI/runtime adapter for the v8 stage 1-2 recoverability search."""

import copy
import json
from pathlib import Path
import random
import uuid

from defipi.methods.gepa.configuration import PROTOCOL_SETTINGS, input_config, validate_inputs
from defipi.configuration.schema import Problem, ValidationError
from defipi.concurrency import RequestLimiter, model_concurrency, run_bounded
from defipi.benchmark.splits import SPLIT_MANIFEST_VERSION, load_gepa_split, load_seed_records
from defipi.methods.gepa.attack_questions import load_attack_pool, sample_attack, with_attack
from defipi.methods.common.target_eval import TargetEvaluator
from defipi.methods.gepa.axtree import AXTreeRenderer, ThreadedAXTreeRenderer
from defipi.evaluation.llm_filter import FILTER_PROTOCOL_VERSION, mixed_filter_prompt_contract
from defipi.methods.common.privacy_metrics import get_privacy_metric
from defipi.methods.common.recoverability import score_answer
from defipi.evaluation.weighted_recoverability import (
    AGGREGATION_VERSION,
    PROTOCOL_VERSION,
    REWRITE_VERSION,
    SCORING_VERSION,
    metric_units,
    objective_fraction,
    strip_legacy_score_diagnostics,
)
from defipi.execution.api.clients import COMPLETE_TEXT_FINISH_REASONS, build_text_client, extract_finish_reason
from defipi.methods.common.ax_edits import (
    ax_edit_contract,
    ax_edit_identity,
    ax_edit_inspection_requests,
    ax_edit_preview,
    check_ax_edit_inspection,
    ax_edit_text_values,
    normalize_ax_edit_fragment,
)
from defipi.pages.reporter_info import resolve_clean_reporter_info
from defipi.generation.attacks import load_attack_specs
from defipi.methods.gepa.recoverability_pareto import POSITIONS, STRATEGY, candidate_payload, export_result, run_search, validate_initial_diversity
from defipi.methods.gepa.v8_mutation_directions import mutation_direction_contract
from defipi.jsonio import content_key as cache_key, open_store, write_json


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_initial(args):
    if not args.initial_attack_file:
        raise ValueError("gepa_recoverability_pareto requires initial_attack_file (a JSON list of prompt texts)")
    specs = list(enumerate(load_attack_specs(args.initial_attack_file)))
    if args.initial_prompt_limit is not None:
        if args.initial_prompt_limit < 1:
            raise ValueError("initial_prompt_limit must be positive")
        if args.initial_prompt_limit < len(specs):
            rng = random.Random(f"initial-prompt-sampling::{args.rng_seed}::{args.initial_attack_file}::{args.initial_prompt_limit}")
            specs = sorted(rng.sample(specs, args.initial_prompt_limit))
    candidates = []
    for index, spec in specs:
        text = spec
        for position in POSITIONS:
            candidate = candidate_payload(text, position, args.fragment_budget_chars)
            candidate["initial_prompt_index"] = index
            candidates.append(candidate)
    if not candidates:
        raise ValueError("initial_attack_file contains no prompts")
    return candidates


def build_ranking_manifest(records, ranking_records, *, attack_pool,
                           ranking_questions_per_page, rng_seed, observation_format):
    """Freeze ordered page/question cases before a target request is made."""
    count = int(ranking_questions_per_page)
    if count < 1:
        raise ValueError("ranking_questions_per_page must be positive")
    prompts = list((attack_pool or {}).get("prompts") or [])
    distinct_prompts = []
    seen_questions = set()
    for prompt in prompts:
        if prompt.get("question") in seen_questions:
            continue
        seen_questions.add(prompt.get("question")); distinct_prompts.append(prompt)
    if not prompts and count != 1:
        raise ValueError("ranking_questions_per_page > 1 requires a question pool")
    by_id = {r["id"]: r for r in records}
    cases = []
    for page in ranking_records:
        page_id = page["id"]
        if prompts:
            if len(distinct_prompts) < count:
                raise ValueError(f"Attack question pool lacks {count} distinct adapted questions for {page_id}")
            seed = f"gepa-recoverability-v8-manifest::{rng_seed}::{page_id}::{observation_format}"
            chosen = random.Random(seed).sample(distinct_prompts, count)
        else:
            chosen = [None]
        for slot, prompt in enumerate(chosen):
            question = prompt["question"] if prompt else page.get("question", "")
            question_id = prompt["prompt_id"] if prompt else "default"
            case_id = f"ranking/{page_id}/question-{slot}/{question_id}"
            case = {"case_id": case_id, "page_id": page_id,
                    "question_id": question_id, "question": question,
                    "question_slot": slot}
            if prompt:
                case["attack_prompt"] = copy.deepcopy(prompt)
            cases.append(case)
    if not cases:
        raise ValueError("Ranking manifest is empty")
    return {"version": "gepa-v8-ranking-manifest-v1", "observation_format": observation_format,
            "ranking_questions_per_page": count, "cases": cases,
            "source_page_ids": [r["id"] for r in ranking_records],
            "question_adaptation_version": (attack_pool or {}).get("question_adaptation_version")}


V8_SCORING_VERSION = SCORING_VERSION
V8_AGGREGATION_VERSION = AGGREGATION_VERSION


def _metric_components(observation):
    scoring = observation.get("weighted_scoring")
    matched = observation.get("matched_fields")
    if (observation.get("metric_version") != SCORING_VERSION
            or scoring is None or not isinstance(matched, dict)):
        raise ValueError("Observation lacks complete v8 scoring fields")
    if set(matched) != {"name", "email", "phone", "address"}:
        raise ValueError("Observation matched_fields are incomplete")
    if scoring.get("version") != SCORING_VERSION:
        raise ValueError("Unsupported v8 observation scoring version")
    units = metric_units(matched)
    if any(scoring.get(key) != units[key] for key in ("j", "d_units")):
        raise ValueError("Observation weighted scoring fields do not match field matches")
    return units


def _validate_observation(saved, *, expected_repeats):
    if saved.get("valid") is not True or saved.get("expected_repeats") != expected_repeats:
        raise ValueError("Saved observation is not a complete valid repeat")
    _metric_components(saved)


class ParetoModelJobs:
    """Model jobs with v8 case IDs and full sanitizer-chain repeat identities."""

    def __init__(self, args, records, run_id, directory, recovery_instruction, rewrite_instruction,
                 attack_questions, *, search_page_ids,
                 observation_renderer=None, rewrite_user_instruction=None):
        self.args, self.records, self.run_id = args, records, run_id
        self.directory = Path(directory)
        self.recovery_instruction, self.rewrite_instruction = recovery_instruction, rewrite_instruction
        self.attack_questions = attack_questions
        self.model_concurrency = model_concurrency(args.model_concurrency)
        self.request_limiter = RequestLimiter(self.model_concurrency)
        common = dict(client_settings=args.eval_client_settings, enable_filter=False,
                      model=args.eval_model, backend=args.eval_backend, html_dir=args.html_dir,
                      temperature=args.eval_temperature,
                      max_retries=args.eval_max_retries, retry_backoff_seconds=args.eval_retry_backoff_seconds,
                      scenario=args.fragment_profile, cache_path=args.cache_path,
                      max_response_tokens=args.eval_max_response_tokens,
                      reasoning_effort=args.eval_reasoning_effort,
                      observation_format=args.observation_format, observation_renderer=observation_renderer,
                      filter_stage=args.filter_stage,
                      request_limiter=self.request_limiter,
                      privacy_metric_profile="name_gated_contact", required_finish_reasons=COMPLETE_TEXT_FINISH_REASONS)
        self.evaluators = {"raw": TargetEvaluator(**common)}
        if args.enable_filter:
            self.evaluators["filtered"] = TargetEvaluator(
                **{**common, "enable_filter": True}, filter_client_settings=args.filter_client_settings, filter_model=args.filter_model,
                filter_backend=args.filter_backend, filter_temperature=args.filter_temperature,
                filter_prompt_variant=args.filter_prompt_variant, filter_prompt_profile=args.filter_prompt_profile,
                filter_cache_path=args.filter_cache_path, filter_max_response_tokens=args.filter_max_response_tokens,
                filter_reasoning_effort=args.filter_reasoning_effort)
        self.client = build_text_client(model=args.mutation_model, backend=args.mutation_backend,
                                        client_settings=args.mutation_client_settings, max_retries=args.mutation_max_retries,
                                        retry_backoff_seconds=args.mutation_retry_backoff_seconds,
                                        request_limiter=self.request_limiter)
        self.mutation_cache_path = Path(args.mutation_cache_path)
        self.mutation_cache = open_store(self.mutation_cache_path)
        if rewrite_user_instruction is None:
            prompt = Path(args.rewrite_user_instruction_file)
            rewrite_user_instruction = prompt.read_text(encoding="utf-8").strip()
        if not rewrite_user_instruction:
            raise ValueError("Rewrite user instruction must be nonempty")
        self.rewrite_user_instruction = rewrite_user_instruction
        self.ax_renderer = observation_renderer
        self.search_page_ids = set(search_page_ids)

    def _record_for_repeat(self, record, repeat, job):
        if self.attack_questions is None:
            return record
        draw_id = f"{job}/{record['id']}/{repeat}"
        path = self.directory / "attack_draws" / self.run_id / f"{draw_id}.json"
        if path.exists():
            saved = read_json(path)
            prompt = next(p for p in self.attack_questions["prompts"] if p["prompt_id"] == saved["prompt_id"])
        else:
            prompt = sample_attack(self.attack_questions, draw_id)
            # Save the draw before either target channel runs. A failed answer
            # never gets a new attack when retried or resumed.
            write_json(path, {"draw_id": draw_id, "record_id": record["id"], "repeat": repeat,
                              "job": job, "prompt_id": prompt["prompt_id"]})
        return with_attack(record, prompt, draw_id)

    def _ranking_record(self, case):
        base = self.records[case["page_id"]]
        record = {**base, "id": case["page_id"], "case_id": case["case_id"],
                  "question_id": case["question_id"], "question": case["question"]}
        if case.get("attack_prompt"):
            record["attack_prompt"] = copy.deepcopy(case["attack_prompt"])
            record["attack_draw_id"] = case["case_id"]
        return record

    def _observation(self, candidate, record, repeat, job, channel, expected_repeats):
        case_id = record.get("case_id", record["id"])
        identity = f"{self.run_id}/{candidate['candidate_id']}/{job}/{case_id}/{repeat}/{channel}"
        path = self.directory / f"{identity}.json"
        if path.exists():
            saved = read_json(path)
            _validate_observation(saved, expected_repeats=expected_repeats)
            return saved
        evaluator = self.evaluators[channel]
        failed_path = self.directory / f"{identity}.failed.json"
        failures = read_json(failed_path) if failed_path.exists() else []
        try:
            prepared = evaluator.prepare_request(
                record, {**candidate, "_evaluation_id": identity},
                filter_cache_namespace=identity if channel == "filtered" else None)
        except Exception as exc:
            failures.append({"evaluation_id": identity, "stage": "prepare_observation",
                             "error": f"{type(exc).__name__}: {exc}"})
            write_json(failed_path, failures)
            raise
        if prepared.get("filter_error") or (
                channel == "filtered" and prepared.get("filter_finish_reason") not in COMPLETE_TEXT_FINISH_REASONS):
            failures.append({"evaluation_id": identity, "stage": "sanitization",
                             **{key: value for key, value in prepared.items() if key.startswith("filter_")}})
            write_json(failed_path, failures)
            reason = prepared.get("filter_error") or prepared.get("filter_finish_reason") or "missing_finish_reason"
            raise RuntimeError(f"Sanitization failed for {identity}: {reason}")
        cached = prepared.get("cache_hit", False)
        for _ in range(evaluator.max_retries):
            try:
                response = (prepared.get("cached_response") if cached else evaluator.client.generate(
                    **{**prepared["request"], "reasoning_effort": evaluator.reasoning_effort}))
            except Exception as exc:
                failures.append({"evaluation_id": identity, "stage": "target_request",
                                 "error": f"{type(exc).__name__}: {exc}"})
                write_json(failed_path, failures); raise
            metrics = strip_legacy_score_diagnostics(
                evaluator.finalize_response(prepared, response, cache_hit=cached, persist_cache=not cached))
            if metrics.get("answer_valid") and str(metrics.get("answer") or "").strip():
                break
            failures.append({"evaluation_id": identity, "metrics": metrics}); write_json(failed_path, failures)
            cached = False
        else:
            raise RuntimeError(f"Invalid target answer for {identity}: {metrics.get('request_error') or metrics.get('finish_reason')}")
        matched_fields = score_answer(metrics["answer"], resolve_clean_reporter_info(record))["matched_fields"]
        units = metric_units(matched_fields)
        observation = {"evaluation_id": identity, "record_id": record["id"],
                       "case_id": case_id,
                       "repeat": repeat, "channel": channel,
                       "observation_format": prepared["observation_format"],
                       "observation_text": prepared["observation_text"],
                       "observation_renderer": prepared["observation_renderer"],
                       "original_html": prepared["original_html"], "filtered_html": prepared["filtered_html"],
                       "original_observation_text": prepared.get("original_observation_text"),
                       "filter_stage": prepared.get("filter_stage"),
                       "filter_input_text": prepared.get("filter_input_text"),
                       "filter_output_text": prepared.get("filter_output_text"),
                       "answer": metrics["answer"], "question": record["question"],
                       "metric_version": SCORING_VERSION, "matched_fields": matched_fields,
                       "valid": True, "expected_repeats": expected_repeats,
                       "weighted_scoring": {"version": SCORING_VERSION, **units},
                       "metrics": metrics}
        if "question_id" in record:
            observation["question_id"] = record["question_id"]
        if prepared.get("ax_edit_target_metadata") is not None:
            observation["ax_edit_target_metadata"] = copy.deepcopy(prepared["ax_edit_target_metadata"])
        if "attack_prompt" in record:
            observation["attack_prompt"] = record["attack_prompt"]
            observation["attack_draw_id"] = record.get("attack_draw_id")
        write_json(path, observation)
        return observation

    def evaluate(self, candidate, cases_or_ids, repeats, job, include_cases):
        case_specs = cases_or_ids["cases"] if isinstance(cases_or_ids, dict) else cases_or_ids
        tasks = []
        for item in case_specs:
            if isinstance(item, dict):
                for repeat in range(repeats): tasks.append((self._ranking_record(item), repeat))
            else:
                for repeat in range(repeats): tasks.append((self._record_for_repeat(self.records[item], repeat, job), repeat))
        observations = {}
        def work(task):
            record, repeat = task
            return {name: self._observation(candidate, record, repeat, job, name, repeats) for name in self.evaluators}
        run_bounded(work, tasks, max_workers=self.model_concurrency, on_result=observations.__setitem__)
        case_results = []
        for index in range(0, len(tasks), repeats):
            record = tasks[index][0]
            samples = [observations[index + repeat] for repeat in range(repeats)]
            channel_means = {}
            for channel in self.evaluators:
                vals = [_metric_components(sample[channel]) for sample in samples]
                j_units = int(sum(v["j"] for v in vals))
                d_units = int(sum(v["d_units"] for v in vals))
                channel_means[channel] = {"j": j_units / repeats,
                                          "d": d_units / (repeats * 3),
                                          "j_numerator": j_units,
                                          "d_numerator": d_units}
            case_results.append({"case_id": record.get("case_id", record["id"]), "page_id": record["id"],
                                 "question": record["question"], "channels": channel_means,
                                 "valid_count": repeats * len(self.evaluators),
                                 "expected_count": repeats * len(self.evaluators),
                                 "samples": samples if include_cases else None})
        channels = list(self.evaluators)
        component_order = [f"{case['case_id']}::{channel}::{component}" for case in case_results for channel in channels for component in ("D", "J")]
        d_vector = tuple(case["channels"][channel]["d"] for case in case_results for channel in channels)
        d_numerator_vector = tuple(case["channels"][channel]["d_numerator"] for case in case_results for channel in channels)
        j_vector = tuple(case["channels"][channel]["j"] for case in case_results for channel in channels)
        j_numerator_vector = tuple(case["channels"][channel]["j_numerator"] for case in case_results for channel in channels)
        joint_numerator = sum(max(case["channels"][ch]["j_numerator"] for ch in channels) for case in case_results)
        contact_numerator = sum(max(case["channels"][ch]["d_numerator"] for ch in channels) for case in case_results)
        joint_denominator = len(case_results) * repeats
        contact_denominator = joint_denominator * 3
        joint_risk = joint_numerator / joint_denominator
        contact_risk = contact_numerator / contact_denominator
        joint_weight = float(self.args.joint_weight)
        objective = objective_fraction(contact_numerator, contact_denominator,
                                       joint_numerator, joint_denominator, joint_weight)
        return {"metric_version": V8_SCORING_VERSION,
                "protocol_version": PROTOCOL_VERSION, "rewrite_version": REWRITE_VERSION,
                "aggregation_version": V8_AGGREGATION_VERSION, "comparable": True,
                "pending": False, "contact_risk": contact_risk, "joint_risk": joint_risk,
                "objective_score": float(objective), "objective_numerator": objective.numerator,
                "objective_denominator": objective.denominator, "joint_weight": joint_weight,
                "joint_numerator": joint_numerator, "joint_denominator": joint_denominator,
                "contact_numerator": contact_numerator, "contact_denominator": contact_denominator,
                "d_vector": list(d_vector),
                "d_numerator_vector": list(d_numerator_vector), "j_vector": list(j_vector),
                "j_numerator_vector": list(j_numerator_vector),
                "component_vector": [value for pair in zip(d_vector, j_vector) for value in pair],
                "case_count": len(case_results), "channel_order": channels,
                "valid_count": len(case_results) * repeats * len(channels),
                "expected_count": len(case_results) * repeats * len(channels),
                "component_order": component_order,
                "repeats": repeats,
                "per_case": [{k: v for k, v in case.items() if k != "samples"} for case in case_results],
                **({"cases": case_results} if include_cases else {})}

    def mutate(self, parent, feedback, job):
        cases = []
        for case in feedback["cases"]:
            samples = []
            for sample in case["samples"]:
                channels = {}
                for channel, observation in sample.items():
                    _metric_components(observation)
                    channels[channel] = {
                        "question": observation.get("question"),
                        "observation_text": observation.get("observation_text"),
                        "answer": observation.get("answer"),
                        "matched_fields": observation.get("matched_fields"),
                        "target_alias_metadata": {
                            alias: {"role": item.get("role"), "name": item.get("name"),
                                    "realized": item.get("realized")}
                            for alias, item in observation.get("ax_edit_target_metadata", {}).items()
                        },
                    }
                sample_feedback = {"channels": channels}
                samples.append(sample_feedback)
            case_feedback = {"samples": samples}
            cases.append(case_feedback)
        current_fragment = parent["defense_fragment"]
        material = {"current_text": current_fragment["policy_text"],
                    "current_edits": current_fragment["edits"], "position": parent["position"],
                    "parent_feedback_cases": cases,
                    "mutation_direction": parent["_mutation_direction"],
                    "mutation_instruction": parent["_mutation_instruction"]}
        if parent["_mutation_direction"] == "hybridize_fragment":
            material["peer_text"] = parent["_peer_policy_text"]
            material["peer_edits"] = parent["_peer_edits"]
        if self.args.fragment_budget_chars is not None:
            material["character_budget"] = self.args.fragment_budget_chars
        request = {"system": self.rewrite_instruction,
                   "messages": [{"role": "user", "content": (
                       self.rewrite_user_instruction + "\n\n"
                       + json.dumps(material, ensure_ascii=False))}],
                   "temperature": self.args.mutation_temperature,
                   "max_tokens": self.args.mutation_max_tokens}
        if self.args.mutation_reasoning_effort is not None: request["reasoning_effort"] = self.args.mutation_reasoning_effort
        key = cache_key({"run_id": self.run_id, "job": job, "request": request})
        with self.mutation_cache.lock:
            entry = self.mutation_cache.setdefault(key, {"job": job, "request": request, "attempts": []})
            if entry.get("text"): return entry["text"]
            prior_attempts = list(entry["attempts"])
        retry_feedback = (str(prior_attempts[-1].get("error") or "invalid_proposal")[:180]
                          if prior_attempts else None)
        remaining_attempts = max(0, self.args.max_proposal_failures - len(prior_attempts))
        for _ in range(remaining_attempts):
            attempt_request = copy.deepcopy(request)
            if retry_feedback:
                retry_material = {**material, "retry_feedback": retry_feedback}
                attempt_request["messages"][0]["content"] = (
                    self.rewrite_user_instruction + "\n\n"
                    + json.dumps(retry_material, ensure_ascii=False))
            response = self.client.generate(
                **{**attempt_request, "reasoning_effort": self.args.mutation_reasoning_effort})
            text = str(response.get("text") or "").strip(); error = None
            if response.get("error") or extract_finish_reason(response) not in COMPLETE_TEXT_FINISH_REASONS: error = "incomplete_response"
            else:
                try:
                    raw = json.loads(text)
                    if not isinstance(raw, dict) or set(raw) != {"policy_text", "edits"}:
                        raise ValueError("response keys must be policy_text and edits only")
                    proposal = normalize_ax_edit_fragment({"format": "ax_edit_v1",
                        "slot": parent["position"], "policy_text": raw.get("policy_text", ""),
                        "edits": raw.get("edits")})
                except Exception as exc:
                    proposal = None; error = f"invalid_ax_edit_schema:{type(exc).__name__}"
                if proposal is not None:
                    generated = ax_edit_preview(proposal)
                    if not generated:
                        error = "empty_ax_edit_fragment"
                    elif any(ax_edit_identity(proposal) == ax_edit_identity(item)
                             for item in parent.get("_existing_fragments", []) if item):
                        error = "duplicate_ax_edit_fragment"
                    else:
                        for page_id in sorted(self.search_page_ids):
                            record = self.records.get(page_id)
                            if record is None:
                                error = "preflight_page_outside_search_split"; break
                            target = resolve_clean_reporter_info(record)
                            if any(any(score_answer(value, target)["matched_fields"].values())
                                   for value in ax_edit_text_values(proposal)):
                                error = "copied_personal_field"; break
                            try:
                                rendered = self.evaluators["raw"]._render_html(
                                    record, {"position": parent["position"],
                                             "attack_instruction": generated,
                                             "defense_fragment": proposal}, target)
                                inspected = self.ax_renderer.render(
                                    rendered, html_dir=self.args.html_dir,
                                    html_file=record.get("html_file"),
                                    inspect_targets=ax_edit_inspection_requests(proposal))
                                detail = inspected.get("target_inspection") or {}
                                issues = check_ax_edit_inspection(proposal, detail)
                                if issues:
                                    error = issues[0]
                                if error:
                                    break
                            except Exception as exc:
                                error = f"ax_edit_preflight:{type(exc).__name__}"; break
            attempt_record = {"response": response, "error": error, "request": attempt_request}
            with self.mutation_cache.lock:
                entry["attempts"].append(attempt_record)
                if error is None:
                    entry["text"] = proposal
                self.mutation_cache.put(key, entry)
            if error is None:
                return proposal
            retry_feedback = str(error or "invalid_proposal")[:180]
        raise RuntimeError(f"No valid rewrite after {self.args.max_proposal_failures} attempts: {job}")


OUTPUT_PATHS = ("output_json", "cache_path", "filter_cache_path", "mutation_cache_path")


def _resolve_output_paths(args):
    from defipi.methods.gepa.workflow import _build_run_timestamp, _resolve_timestamped_output_path
    timestamp = _build_run_timestamp(args.timestamp_outputs, args.timestamp_value, args.timestamp_format)
    if timestamp is not None:
        for name in OUTPUT_PATHS:
            if getattr(args, name) is not None:
                setattr(args, name, str(_resolve_timestamped_output_path(Path(getattr(args, name)), timestamp)))


def _load_resume(args):
    if args.resume_from_output_json is None:
        return None
    saved = read_json(args.resume_from_output_json)
    if saved.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Resume mismatch: unsupported GEPA protocol version")
    if saved.get("run_status") == "completed":
        raise ValueError("Resume file is already completed")
    checkpoint = saved["checkpoint"]
    problems = []
    # These values are embedded in search state. A continuation may restore
    # state, but must not silently replace explicit current inputs with it.
    for state_name, input_name in (("tau", "pareto_tau"), ("epsilon", "parent_epsilon"),
                                   ("max_k", "active_pool_size"), ("mutation_rng_seed", "rng_seed"),
                                   ("stagnation_patience", "stagnation_patience")):
        if checkpoint.get(state_name) != getattr(args, input_name):
            problems.append(Problem(input_name, "does not match the resume checkpoint"))
    # The checkpoint's output and caches belong to the run being continued.
    for name in OUTPUT_PATHS:
        current, recorded = getattr(args, name), saved["seed_meta"][name]
        if (current is None) != (recorded is None) or (current is not None and Path(current) != Path(recorded)):
            problems.append(Problem(name, f"must match the resume checkpoint path {recorded!r}"))
    if problems:
        raise ValidationError(problems)
    return saved


def run(args):
    inputs = validate_inputs(input_config(args))
    _resolve_output_paths(args)
    saved = _load_resume(args)
    initial = load_initial(args)
    validate_initial_diversity(initial)
    question = (None if args.attack_prompts_file is not None else
                args.question_override if args.question_override is not None else
                Path(args.recovery_instruction_file).read_text(encoding="utf-8").strip())
    rewrite = Path(args.rewrite_instruction_file).read_text(encoding="utf-8").strip()
    rewrite_user_prompt_path = Path(args.rewrite_user_instruction_file)
    rewrite_user_instruction = rewrite_user_prompt_path.read_text(encoding="utf-8").strip()
    if question is not None and not question:
        raise ValueError("Recovery question must be nonempty")
    if not rewrite:
        raise ValueError("Rewrite instruction must be nonempty")
    if not rewrite_user_instruction:
        raise ValueError("Rewrite user instruction must be nonempty")
    records = load_seed_records(args.seeds_json, start_index=args.seed_index, limit=None, table_seed=args.table_seed)
    if not records or len({record.get("id") for record in records}) != len(records):
        raise ValueError("Source records must be nonempty with unique IDs")
    for record in records:
        score_answer("Preflight validation", resolve_clean_reporter_info(record))
    if question is not None:
        for record in records: record["question"] = question
    split = load_gepa_split(records, args.split_manifest, scenario=args.fragment_profile)
    attack_pool = load_attack_pool(args.attack_prompts_file, args.fragment_profile, args.rng_seed, args.observation_format) if args.attack_prompts_file else None
    if attack_pool is not None and not attack_pool.get("prompts"):
        raise ValueError("Attack question pool is empty")
    pools = split
    if args.feedback_minibatch_size > len(pools["feedback_records"]):
        raise ValueError("feedback_minibatch_size exceeds the manifest feedback pool")
    if not pools["feedback_record_ids"] or not pools["pareto_record_ids"]:
        raise ValueError("Feedback and ranking pools must both be nonempty")
    manifest = build_ranking_manifest(records, pools["pareto_records"], attack_pool=attack_pool,
                                       ranking_questions_per_page=args.ranking_questions_per_page,
                                       rng_seed=args.rng_seed, observation_format=args.observation_format)
    with ThreadedAXTreeRenderer(AXTreeRenderer) as renderer:
        renderer_settings = copy.deepcopy(renderer.settings)
        settings_names = ("rng_seed", "split_manifest",
                          "feedback_minibatch_size", "ranking_questions_per_page", "ranking_repeats", "parent_feedback_repeats",
                          "fragment_budget_chars", "fragment_profile", "eval_backend", "eval_model", "enable_filter",
                          "filter_backend", "filter_model", "filter_temperature", "filter_prompt_variant", "filter_prompt_profile", "filter_stage",
                          "privacy_metric_profile",
                          "mutation_backend", "mutation_model", "mutation_temperature", "mutation_max_tokens", "observation_format",
                          "max_extensions_per_position", "stagnation_patience", "active_pool_size",
                          "pareto_tau", "joint_weight", "parent_epsilon")
        settings_names = settings_names + tuple(name for name in (
            "eval_max_response_tokens", "filter_max_response_tokens", "eval_reasoning_effort",
            "filter_reasoning_effort", "mutation_reasoning_effort", "recovery_instruction_file",
            "rewrite_instruction_file", "rewrite_user_instruction_file", "initial_attack_file", "attack_prompts_file",
            "eval_max_retries", "eval_retry_backoff_seconds"))
        protocol = {"version": PROTOCOL_VERSION, "initial": initial, "records": records,
                     "ranking_manifest": manifest,
                    "attack_questions": attack_pool,
                    "attack_sampling_contract": {"version": (attack_pool or {}).get("version"),
                                                   "sampling_seed": (attack_pool or {}).get("sampling_seed"),
                                                   "feedback_sampling": "random-with-replacement-per-page-repeat"},
                    "recovery_instruction": question, "rewrite_instruction": rewrite,
                    "rewrite_user_instruction": rewrite_user_instruction,
                    "observation_renderer": renderer_settings,
                    "scoring_version": SCORING_VERSION,
                    "aggregation_version": V8_AGGREGATION_VERSION,
                    "rewrite_version": REWRITE_VERSION,
                    "joint_weight": args.joint_weight,
                    "settings": {**PROTOCOL_SETTINGS, **{name: getattr(args, name) for name in settings_names}},
                    "mutation_directions": mutation_direction_contract(args.fragment_profile)}
        protocol["candidate_representation_contract"] = {
            "representation": "ax_edit_v1", **ax_edit_contract(args.fragment_profile),
            "new_prompt_files": [str(args.rewrite_instruction_file), str(rewrite_user_prompt_path)],
        }
        if args.enable_filter:
            protocol["filter_protocol"] = FILTER_PROTOCOL_VERSION
        if args.enable_filter and args.filter_prompt_variant == "mixed":
            protocol["settings"]["mixed_filter_prompt_contract"] = mixed_filter_prompt_contract()
        protocol["settings"]["eval_temperature"] = args.eval_temperature
        output_path = Path(args.output_json)
        manifest_path = Path(saved["seed_meta"]["ranking_manifest_path"] if saved else str(output_path.with_suffix(".ranking_manifest.json")))
        if not saved:
            # This file is committed before the first checkpoint/target call.
            write_json(manifest_path, manifest)
        run_id = saved["seed_meta"]["run_id"] if saved else str(uuid.uuid4())
        directory = saved["seed_meta"]["observations_dir"] if saved else str(output_path.with_suffix(".evaluations"))
        jobs = ParetoModelJobs(
            args, {r["id"]: r for r in records}, run_id, directory, question, rewrite,
            attack_pool, observation_renderer=renderer,
            rewrite_user_instruction=rewrite_user_instruction,
            search_page_ids=set(pools["feedback_record_ids"]) | set(pools["pareto_record_ids"]))
        meta = {"search_strategy": STRATEGY, "protocol_version": PROTOCOL_VERSION, "output_json": str(output_path),
                "observations_dir": directory, "run_id": run_id, "ranking_manifest_path": str(manifest_path),
                 "data_split_version": SPLIT_MANIFEST_VERSION,
                "feedback_record_ids": pools["feedback_record_ids"], "ranking_record_ids": pools["pareto_record_ids"],
                "fragment_profile": args.fragment_profile,
                "stagnation_patience": args.stagnation_patience,
                "privacy_metric_profile": args.privacy_metric_profile,
                "scoring_version": SCORING_VERSION,
                "joint_weight": args.joint_weight,
                "rewrite_version": REWRITE_VERSION,
                "mutation_directions": mutation_direction_contract(args.fragment_profile),
                "rewrite_instruction": rewrite,
                "rewrite_user_instruction": rewrite_user_instruction,
                "source_record_count": len(records),
                "heldout_record_ids": [r["id"] for r in split["heldout_records"]],
                "eval_record_ids": [r["id"] for r in split["eval_pool_records"]],
                "ranking_questions_per_page": args.ranking_questions_per_page,
                "heldout_size": len(split["heldout_records"]), "eval_pool_size": len(split["eval_pool_records"]),
                "split_manifest": args.split_manifest, "table_seed": args.table_seed,
                "aggregation_version": V8_AGGREGATION_VERSION,
                "observation_format": args.observation_format,
                "filter_stage": args.filter_stage,
                "observation_renderer": renderer_settings,
                "attack_questions": copy.deepcopy(attack_pool),
                "recovery_instruction": question,
                "config_path": str(output_path.with_suffix(".config.json")),
                "cache_path": str(args.cache_path), "filter_cache_path": args.filter_cache_path,
                "mutation_cache_path": str(args.mutation_cache_path)}
        meta["candidate_representation"] = "ax_edit_v1"
        run_contract = {"fragment_profile": args.fragment_profile,
                        "fragment_contract_version": "ax_edit_v1",
                        "privacy_metric_definition": get_privacy_metric(
                            args.privacy_metric_profile).to_dict(),
                        "dual_score_protocol_version": "metric_defined_dual_v1"}
        run_contract.update(candidate_representation="ax_edit_v1",
                            **ax_edit_contract(args.fragment_profile))
        meta["gepa_run_contract"] = {**run_contract}
        branches = 2 if args.enable_filter else 1
        ranking_cases = len(manifest["cases"])
        meta["planned_target_calls"] = branches * (
            len(initial) * ranking_cases * args.ranking_repeats
            + len(POSITIONS) * args.max_extensions_per_position *
            (args.feedback_minibatch_size * args.parent_feedback_repeats
             + ranking_cases * args.ranking_repeats))
        meta["planned_target_calls_scope"] = "nominal_no_retry_excludes_filter_and_mutation_calls"
        meta["planned_target_calls_is_nominal"] = True
        write_json(meta["config_path"], inputs)
        def save(state, status, error=None):
            result, top, by_position = export_result(state)
            payload = {"protocol_version": PROTOCOL_VERSION,  "run_status": status,
                       "phase": state["phase"], "seed_meta": meta,
                       "active_config": {**protocol["settings"], "protocol_version": PROTOCOL_VERSION,
                                         "scoring_version": SCORING_VERSION,
                                         "aggregation_version": AGGREGATION_VERSION,
                                         "rewrite_version": REWRITE_VERSION,
                                         "mutation_directions": mutation_direction_contract(args.fragment_profile),
                                         "rewrite_instruction": rewrite,
                                         "rewrite_user_instruction": rewrite_user_instruction},
                       "checkpoint": {**state, "rewrite_instruction": rewrite,
                                      "rewrite_user_instruction": rewrite_user_instruction,
                                      "mutation_directions": mutation_direction_contract(args.fragment_profile)},
                       "rewrite_instruction": rewrite,
                       "rewrite_user_instruction": rewrite_user_instruction,
                       "result": result, "seed_pool": initial, "top_candidates": top,
                       "top_candidates_by_position": by_position, "ranking_manifest": manifest}
            if error: payload["error"] = error
            write_json(output_path, payload)
        return run_search(initial, pools["feedback_record_ids"], manifest, evaluate=jobs.evaluate,
                          mutate=jobs.mutate, save=save, rng_seed=args.rng_seed,
                          max_extensions_per_position=args.max_extensions_per_position, tau=args.pareto_tau,
                          stagnation_patience=args.stagnation_patience,
                          epsilon=args.parent_epsilon, max_k=args.active_pool_size, ranking_repeats=args.ranking_repeats,
                          parent_repeats=args.parent_feedback_repeats, minibatch_size=args.feedback_minibatch_size,
                          budget=args.fragment_budget_chars, joint_weight=args.joint_weight,
                          checkpoint=saved["checkpoint"] if saved else None,
                          fragment_profile=args.fragment_profile)
