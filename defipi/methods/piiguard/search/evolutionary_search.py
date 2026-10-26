#!/usr/bin/env python3
import json
from concurrent.futures import ThreadPoolExecutor

from defipi.methods.common.defense_fragments import normalize_fragment_spec
from defipi.methods.common.utils import normalize_instruction
from defipi.configuration.schema import Field, validate_config


def _candidate_key(attack_instruction, position, defense_fragment=None):
    fragment_key = None
    if defense_fragment is not None:
        fragment_key = json.dumps(
            normalize_fragment_spec(defense_fragment),
            ensure_ascii=False,
            sort_keys=True,
        )
    return (
        normalize_instruction(attack_instruction),
        str(position or ""),
        fragment_key,
    )


def _coerce_mutation(record):
    if type(record) is not dict:
        raise ValueError("Mutation requires a canonical candidate object")
    if any(key in record for key in ("attack_instruction", "text")):
        raise ValueError("Mutation uses instruction, not a legacy text alias")
    if type(record.get("instruction")) is not str or not record["instruction"].strip():
        raise ValueError("Mutation instruction must be a nonempty string")
    if type(record.get("operator")) is not str or not record["operator"]:
        raise ValueError("Mutation operator must be a nonempty string")
    if type(record.get("position")) is not str or not record["position"]:
        raise ValueError("Mutation position must be a nonempty string")
    fragment = record.get("defense_fragment")
    if fragment is not None:
        fragment = normalize_fragment_spec(fragment)
        if fragment["slot"] != record["position"]:
            raise ValueError("Mutation fragment slot must equal position")
    return {"instruction": normalize_instruction(record["instruction"]),
            "operator": record["operator"], "position": record["position"],
            "defense_fragment": fragment}


def _new_candidate(
    candidate_id,
    attack_instruction,
    position,
    lineage_parent_id=None,
    lineage_depth=0,
    mutation_operator="seed",
    defense_fragment=None,
):
    return {
        "candidate_id": candidate_id,
        "attack_instruction": normalize_instruction(attack_instruction),
        "position": position,
        "defense_fragment": (
            normalize_fragment_spec(defense_fragment)
            if defense_fragment is not None
            else None
        ),
        "lineage_parent_id": lineage_parent_id,
        "lineage_depth": lineage_depth,
        "mutation_operator": mutation_operator,
        "visits": 0,
        "score": None,
        "reward": 0.0,
        "success_rate": 0.0,
        "avg_leakage_ratio": None,
        "low_leak_count": 0,
        "low_leak_coverage": 0.0,
        "task_count": 0,
        "last_feedback": None,
    }


def _set_candidate_feedback(candidate, scored, feedback):
    candidate["score"] = scored["score"]
    candidate["reward"] = scored["avg_reward"]
    candidate["success_rate"] = scored["success_rate"]
    candidate["avg_leakage_ratio"] = scored["avg_leakage_ratio"]
    candidate["low_leak_count"] = scored["low_leak_count"]
    candidate["low_leak_coverage"] = scored["low_leak_coverage"]
    candidate["task_count"] = scored["task_count"]
    candidate["last_feedback"] = feedback


def _increment_visits(pool, candidate_ids):
    for candidate_id in candidate_ids:
        pool[candidate_id]["visits"] += 1


def _score_formula():
    return "score=2*success_rate+1*(1-avg_leakage_ratio)+0.25*low_leak_coverage"


def _candidate_quality_key(candidate):
    score = candidate.get("score")
    score_value = float(score) if isinstance(score, (int, float)) else float("-inf")
    success_rate = float(candidate["success_rate"])
    avg_leakage_ratio = candidate.get("avg_leakage_ratio")
    leakage_reward = (
        1.0 - float(avg_leakage_ratio)
        if isinstance(avg_leakage_ratio, (int, float))
        else float("-inf")
    )
    low_leak_coverage = float(candidate["low_leak_coverage"])
    return (
        score_value,
        success_rate,
        leakage_reward,
        low_leak_coverage,
    )


def _candidate_selection_key(candidate):
    return _candidate_quality_key(candidate) + (
        -int(candidate["visits"]),
        -int(candidate["lineage_depth"]),
    )


def _select_candidate(pool, rng, exploration_epsilon, max_lineage_depth):
    if not pool:
        raise RuntimeError("No candidate entries available for selection")

    unscored = [
        candidate for candidate in pool.values()
        if candidate.get("score") is None and candidate.get("last_feedback") is None
    ]
    if unscored:
        return rng.choice(unscored)["candidate_id"]

    expandable = [
        candidate
        for candidate in pool.values()
        if candidate["lineage_depth"] < max_lineage_depth
        and candidate.get("score") is not None
    ]
    if not expandable:
        return None

    if rng.random() < exploration_epsilon:
        return rng.choice(expandable)["candidate_id"]

    chosen = max(
        expandable,
        key=_candidate_selection_key,
    )
    return chosen["candidate_id"]


def _sample_peer_instruction(pool, current_id, rng):
    choices = [
        candidate["attack_instruction"]
        for candidate_id, candidate in pool.items()
        if candidate_id != current_id and candidate.get("attack_instruction")
    ]
    if not choices:
        return None
    return rng.choice(choices)


def _propose_mutations(
    pool,
    dedupe,
    current_id,
    mutate_fn,
    mutation_batch,
    positions,
    rng,
):
    current = pool[current_id]
    peer_instruction = _sample_peer_instruction(pool, current_id, rng)
    allowed_positions = [str(position or "").strip() for position in (positions or []) if str(position or "").strip()]

    raw_variants = mutate_fn(
        current["attack_instruction"], n=mutation_batch, rng=rng,
        peer_instruction=peer_instruction, current_candidate=current,
        allowed_positions=allowed_positions,
    )
    if type(raw_variants) is not list:
        raise ValueError("mutate_fn must return a list of canonical candidates")

    candidates = []
    seen = set()
    for raw in raw_variants or []:
        item = _coerce_mutation(raw)
        if item is None:
            continue
        position = item["position"]
        if allowed_positions and position not in allowed_positions:
            raise ValueError("Mutation position is outside allowed_positions")
        if len(allowed_positions) > 1 and rng.random() < 0.25:
            position = rng.choice(allowed_positions)
        defense_fragment = item.get("defense_fragment")
        if defense_fragment is not None:
            defense_fragment = normalize_fragment_spec(defense_fragment)
            if allowed_positions and defense_fragment["slot"] not in allowed_positions:
                locked_slot = (
                    current["position"]
                    if current.get("position") in allowed_positions
                    else allowed_positions[0]
                )
                defense_fragment = normalize_fragment_spec(
                    {
                        **defense_fragment,
                        "slot": locked_slot,
                        **({"presentation": "notice", "row_text": ""} if locked_slot != "contact_block" else {}),
                    },
                )
            position = defense_fragment["slot"]
        if allowed_positions and position not in allowed_positions:
            continue
        key = _candidate_key(item["instruction"], position, defense_fragment)
        if key in dedupe or key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "instruction": item["instruction"],
                "position": position,
                "operator": item["operator"],
                "defense_fragment": defense_fragment,
            }
        )
    return candidates


def _score_feedback(
    feedback,
    low_leak_threshold,
):
    seed_results = feedback["seed_results"]
    task_count = feedback["task_count"]
    success_rate = feedback["success_rate"]
    scored_count = feedback["scored_count"]
    reward_value = feedback["reward"]
    avg_reward = float(reward_value) if isinstance(reward_value, (int, float)) else 0.0
    avg_leakage_ratio = feedback["avg_leakage_ratio"]
    if avg_leakage_ratio is None:
        avg_leakage_ratio = 1.0 if scored_count == 0 else (1.0 - avg_reward)
    avg_leakage_ratio = float(avg_leakage_ratio)

    low_leak_count = 0
    for item in seed_results:
        leakage_ratio = item["leakage_ratio"]
        if leakage_ratio is None:
            continue
        if float(leakage_ratio) <= low_leak_threshold:
            low_leak_count += 1

    low_leak_coverage = (low_leak_count / task_count) if task_count else 0.0
    score = (
        2.0 * success_rate
        + 1.0 * (1.0 - avg_leakage_ratio)
        + 0.25 * low_leak_coverage
    )
    if feedback["eligible"] is False:
        score = None
    return {
        "score": score,
        "avg_reward": avg_reward,
        "success_rate": success_rate,
        "avg_leakage_ratio": avg_leakage_ratio,
        "low_leak_count": low_leak_count,
        "low_leak_coverage": low_leak_coverage,
        "task_count": task_count,
    }


def _public_candidate(candidate):
    return {
        "candidate_id": candidate["candidate_id"],
        "attack_instruction": candidate["attack_instruction"],
        "position": candidate["position"],
        "defense_fragment": candidate.get("defense_fragment"),
        "lineage_parent_id": candidate["lineage_parent_id"],
        "lineage_depth": candidate["lineage_depth"],
        "mutation_operator": candidate["mutation_operator"],
        "visits": candidate["visits"],
        "score": candidate.get("score"),
        "reward": candidate["reward"],
        "success_rate": candidate["success_rate"],
        "avg_leakage_ratio": candidate.get("avg_leakage_ratio"),
        "low_leak_count": candidate["low_leak_count"],
        "low_leak_coverage": candidate["low_leak_coverage"],
        "task_count": candidate["task_count"],
        "last_feedback": candidate["last_feedback"],
    }


def _candidate_payload(candidate):
    payload = {
        "attack_instruction": candidate["attack_instruction"],
        "position": candidate["position"],
    }
    if candidate.get("defense_fragment") is not None:
        payload["defense_fragment"] = candidate.get("defense_fragment")
    return payload


def _empty_result(score_formula):
    return {
        "best": None,
        "history": [],
        "pool": [],
        "total_evals": 0,
        "search_strategy": "evolutionary_pool_epsilon_greedy",
        "score_formula": score_formula,
        "pool_stats": {
            "candidate_count": 0,
            "max_lineage_depth": 0,
            "seed_candidate_count": 0,
        },
    }


def _build_search_result(pool, history, best, total_evals, seed_candidate_count, score_formula):
    pool_out = [_public_candidate(candidate) for candidate in pool.values()]
    pool_out.sort(
        key=lambda candidate: (
            candidate["score"] is not None,
            *_candidate_quality_key(candidate),
            -int(candidate["visits"]),
        ),
        reverse=True,
    )

    pool_stats = {
        "candidate_count": len(pool_out),
        "max_lineage_depth": max([candidate["lineage_depth"] for candidate in pool_out] or [0]),
        "seed_candidate_count": seed_candidate_count,
    }

    return {
        "best": best,
        "history": history,
        "pool": pool_out,
        "total_evals": total_evals,
        "search_strategy": "evolutionary_pool_epsilon_greedy",
        "score_formula": score_formula,
        "pool_stats": pool_stats,
    }


def _build_search_resume_state(
    pool,
    rng,
    next_candidate_id,
    completed_steps,
    seed_candidate_count,
):
    return {
        "pool_order": [candidate["candidate_id"] for candidate in pool.values()],
        "rng_state": rng.getstate(),
        "next_candidate_id": int(next_candidate_id),
        "completed_steps": int(completed_steps),
        "seed_candidate_count": int(seed_candidate_count),
    }


def run_blackbox_search(
    *,
    seed_instructions,
    positions,
    rng,
    mutate_fn,
    evaluate_fn,
    rounds,
    proposals_per_round,
    mutation_batch,
    exploration_epsilon,
    low_leak_threshold,
    max_lineage_depth,
    child_eval_workers,
    progress_callback=None,
    checkpoint_callback=None,
    resume_state=None,
):
    validate_config({
        "seed_instructions": seed_instructions, "positions": positions,
        "rounds": rounds, "proposals_per_round": proposals_per_round,
        "mutation_batch": mutation_batch, "exploration_epsilon": exploration_epsilon,
        "low_leak_threshold": low_leak_threshold, "max_lineage_depth": max_lineage_depth,
        "child_eval_workers": child_eval_workers,
    }, {
        "seed_instructions": Field(list, items=Field(dict)),
        "positions": Field(list, items=Field(str)),
        **{key: Field(int, minimum=1) for key in
           ("rounds", "proposals_per_round", "mutation_batch", "child_eval_workers")},
        **{key: Field((int, float), minimum=0, maximum=1) for key in
           ("exploration_epsilon", "low_leak_threshold")},
        "max_lineage_depth": Field(int, minimum=0),
    }, prefix="search")
    score_formula = _score_formula()
    if not positions:
        return _empty_result(score_formula=score_formula)
    if rng is None:
        raise ValueError("rng is required")
    if mutate_fn is None or evaluate_fn is None:
        raise ValueError("mutate_fn and evaluate_fn are required")

    base_pool = []
    for item in seed_instructions or []:
        coerced = _coerce_mutation(item)
        if coerced is not None:
            base_pool.append(coerced)

    if not base_pool:
        return _empty_result(score_formula=score_formula)

    seen_seed = set()
    dedup_pool = []
    for item in base_pool:
        key = _candidate_key(
            item.get("instruction"),
            item.get("position"),
            item.get("defense_fragment"),
        )
        if key not in seen_seed:
            dedup_pool.append(item)
            seen_seed.add(key)
    base_pool = dedup_pool

    pool = {}
    dedupe = {}
    next_candidate_id = 1
    seed_candidate_count = 0
    history = []
    total_evals = 0
    best = None
    completed_steps = 0

    if resume_state is not None:
        ordered_candidates = list(resume_state["pool_candidates"])
        for candidate_record in ordered_candidates:
            candidate = dict(candidate_record)
            candidate_id = candidate["candidate_id"]
            pool[candidate_id] = candidate
            dedupe[
                _candidate_key(
                    candidate.get("attack_instruction"),
                    candidate.get("position"),
                    candidate.get("defense_fragment"),
                )
            ] = candidate_id
        next_candidate_id = resume_state["next_candidate_id"]
        seed_candidate_count = resume_state["seed_candidate_count"]
        history = list(resume_state["history"])
        total_evals = resume_state["total_evals"]
        best = resume_state["best"]
        completed_steps = resume_state["completed_steps"]
        rng.setstate(resume_state["rng_state"])
    else:
        for item in base_pool:
            seed_position = item["position"]
            if seed_position not in positions:
                raise ValueError("Seed position is outside configured positions")
            position_choices = [seed_position]
            for position in position_choices:
                defense_fragment = item.get("defense_fragment")
                if defense_fragment is not None:
                    defense_fragment = normalize_fragment_spec(defense_fragment)
                    position = defense_fragment["slot"]
                key = _candidate_key(item.get("instruction"), position, defense_fragment)
                if key in dedupe:
                    continue
                candidate = _new_candidate(
                    next_candidate_id,
                    item.get("instruction"),
                    position,
                    lineage_parent_id=None,
                    lineage_depth=0,
                    mutation_operator="seed",
                    defense_fragment=defense_fragment,
                )
                pool[next_candidate_id] = candidate
                dedupe[key] = next_candidate_id
                next_candidate_id += 1
        seed_candidate_count = len(pool)

    def _emit_checkpoint(step_event):
        if checkpoint_callback is None:
            return
        checkpoint_callback(
            _build_search_result(
                pool=pool,
                history=history,
                best=best,
                total_evals=total_evals,
                seed_candidate_count=seed_candidate_count,
                score_formula=score_formula,
            ),
            checkpoint=dict(step_event or {}),
            resume_state=_build_search_resume_state(
                pool=pool,
                rng=rng,
                next_candidate_id=next_candidate_id,
                completed_steps=completed_steps,
                seed_candidate_count=seed_candidate_count,
            ),
        )

    search_exhausted = False
    for round_idx in range(rounds):
        for proposal_idx in range(proposals_per_round):
            step_index = round_idx * proposals_per_round + proposal_idx
            if step_index < completed_steps:
                continue
            selected_id = _select_candidate(
                pool,
                rng,
                exploration_epsilon,
                max_lineage_depth,
            )
            if selected_id is None:
                event = {
                    "round": round_idx + 1,
                    "proposal": proposal_idx + 1,
                    "event_type": "search_exhausted",
                    "reason": "no_expandable_candidate",
                }
                history.append(event)
                if progress_callback is not None:
                    progress_callback(
                        event,
                        total_evals=total_evals,
                        pool_count=len(pool),
                    )
                _emit_checkpoint(
                    {
                        "round": round_idx + 1,
                        "proposal": proposal_idx + 1,
                        "event_type": "search_exhausted",
                        "reason": "no_expandable_candidate",
                        "total_evals": total_evals,
                        "pool_count": len(pool),
                    }
                )
                search_exhausted = True
                break
            selected = pool[selected_id]

            if progress_callback is not None:
                progress_callback(
                    {
                        "round": round_idx + 1,
                        "proposal": proposal_idx + 1,
                        "event_type": "step_start",
                        "candidate_id": selected_id,
                        "lineage_parent_id": selected["lineage_parent_id"],
                        "lineage_depth": selected["lineage_depth"],
                        "mutation_operator": selected["mutation_operator"],
                        "position": selected["position"],
                        "score": selected.get("score"),
                        "visits": selected["visits"],
                    },
                    total_evals=total_evals,
                    pool_count=len(pool),
                )

            if selected.get("score") is None:
                candidate = _candidate_payload(selected)
                feedback = evaluate_fn(candidate)
                scored = _score_feedback(feedback, low_leak_threshold=low_leak_threshold)
                _set_candidate_feedback(selected, scored, feedback)
                _increment_visits(pool, [selected_id])
                total_evals += 1

                event = {
                    "round": round_idx + 1,
                    "proposal": proposal_idx + 1,
                    "event_type": "score_candidate",
                    "candidate_id": selected_id,
                    "lineage_parent_id": selected["lineage_parent_id"],
                    "lineage_depth": selected["lineage_depth"],
                    "mutation_operator": selected["mutation_operator"],
                    "attack_instruction": selected["attack_instruction"],
                    "position": selected["position"],
                    "score": scored["score"],
                    "reward": scored["avg_reward"],
                    "success_rate": scored["success_rate"],
                    "avg_leakage_ratio": scored["avg_leakage_ratio"],
                    "low_leak_count": scored["low_leak_count"],
                    "low_leak_coverage": scored["low_leak_coverage"],
                    "task_count": scored["task_count"],
                    "visits": selected["visits"],
                    "feedback": feedback,
                }
                history.append(event)
                if progress_callback is not None:
                    progress_callback(
                        event,
                        total_evals=total_evals,
                        pool_count=len(pool),
                    )

                if scored["score"] is not None and (
                    best is None or _candidate_quality_key(scored) > _candidate_quality_key(best)
                ):
                    best = {
                        "score": scored["score"],
                        "reward": scored["avg_reward"],
                        "success_rate": scored["success_rate"],
                        "avg_leakage_ratio": scored["avg_leakage_ratio"],
                        "low_leak_count": scored["low_leak_count"],
                        "low_leak_coverage": scored["low_leak_coverage"],
                        "round": round_idx + 1,
                        "proposal": proposal_idx + 1,
                        "candidate_id": selected_id,
                        "mutation_operator": selected["mutation_operator"],
                        "candidate": candidate,
                        "feedback": feedback,
                    }
                completed_steps = step_index + 1
                _emit_checkpoint(
                    {
                        "round": round_idx + 1,
                        "proposal": proposal_idx + 1,
                        "event_type": "step_complete",
                        "step_result": "score_candidate",
                        "candidate_id": selected_id,
                        "total_evals": total_evals,
                        "pool_count": len(pool),
                    }
                )
                continue

            if selected["lineage_depth"] >= max_lineage_depth:
                _increment_visits(pool, [selected_id])
                event = {
                    "round": round_idx + 1,
                    "proposal": proposal_idx + 1,
                    "event_type": "no_new_candidate",
                    "reason": "max_lineage_depth",
                    "candidate_id": selected_id,
                    "lineage_parent_id": selected["lineage_parent_id"],
                    "lineage_depth": selected["lineage_depth"],
                    "mutation_operator": selected["mutation_operator"],
                    "attack_instruction": selected["attack_instruction"],
                    "position": selected["position"],
                    "score": 0.0,
                    "reward": 0.0,
                    "success_rate": 0.0,
                    "avg_leakage_ratio": None,
                    "low_leak_count": 0,
                    "low_leak_coverage": 0.0,
                    "task_count": 0,
                    "visits": selected["visits"],
                    "feedback": None,
                }
                history.append(event)
                if progress_callback is not None:
                    progress_callback(
                        event,
                        total_evals=total_evals,
                        pool_count=len(pool),
                    )
                completed_steps = step_index + 1
                _emit_checkpoint(
                    {
                        "round": round_idx + 1,
                        "proposal": proposal_idx + 1,
                        "event_type": "step_complete",
                        "step_result": "no_new_candidate",
                        "reason": "max_lineage_depth",
                        "candidate_id": selected_id,
                        "total_evals": total_evals,
                        "pool_count": len(pool),
                    }
                )
                continue

            candidates = _propose_mutations(
                pool,
                dedupe,
                selected_id,
                mutate_fn,
                mutation_batch,
                positions,
                rng,
            )
            if not candidates:
                _increment_visits(pool, [selected_id])
                event = {
                    "round": round_idx + 1,
                    "proposal": proposal_idx + 1,
                    "event_type": "no_new_candidate",
                    "reason": "no_valid_mutation",
                    "candidate_id": selected_id,
                    "lineage_parent_id": selected["lineage_parent_id"],
                    "lineage_depth": selected["lineage_depth"],
                    "mutation_operator": selected["mutation_operator"],
                    "attack_instruction": selected["attack_instruction"],
                    "position": selected["position"],
                    "score": 0.0,
                    "reward": 0.0,
                    "success_rate": 0.0,
                    "avg_leakage_ratio": None,
                    "low_leak_count": 0,
                    "low_leak_coverage": 0.0,
                    "task_count": 0,
                    "visits": selected["visits"],
                    "feedback": None,
                }
                history.append(event)
                if progress_callback is not None:
                    progress_callback(
                        event,
                        total_evals=total_evals,
                        pool_count=len(pool),
                    )
                completed_steps = step_index + 1
                _emit_checkpoint(
                    {
                        "round": round_idx + 1,
                        "proposal": proposal_idx + 1,
                        "event_type": "step_complete",
                        "step_result": "no_new_candidate",
                        "reason": "no_valid_mutation",
                        "candidate_id": selected_id,
                        "total_evals": total_evals,
                        "pool_count": len(pool),
                    }
                )
                continue

            evaluated_children = []
            for proposed in candidates:
                child_id = next_candidate_id
                next_candidate_id += 1
                child = _new_candidate(
                    child_id,
                    proposed["instruction"],
                    proposed["position"],
                    lineage_parent_id=selected_id,
                    lineage_depth=selected["lineage_depth"] + 1,
                    mutation_operator=proposed["operator"],
                    defense_fragment=proposed.get("defense_fragment"),
                )
                pool[child_id] = child
                dedupe[
                    _candidate_key(
                        child["attack_instruction"],
                        child["position"],
                        child.get("defense_fragment"),
                    )
                ] = child_id
                evaluated_children.append(child)

            _increment_visits(
                pool,
                [selected_id] + [child["candidate_id"] for child in evaluated_children],
            )

            child_feedback_by_id = {}
            if child_eval_workers > 1 and len(evaluated_children) > 1:
                max_workers = min(int(child_eval_workers), len(evaluated_children))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_by_id = {
                        child["candidate_id"]: executor.submit(
                            evaluate_fn,
                            _candidate_payload(child),
                        )
                        for child in evaluated_children
                    }
                    for child in evaluated_children:
                        child_feedback_by_id[child["candidate_id"]] = (
                            future_by_id[child["candidate_id"]].result()
                        )
            else:
                for child in evaluated_children:
                    child_feedback_by_id[child["candidate_id"]] = (
                        evaluate_fn(_candidate_payload(child))
                    )

            for child in evaluated_children:
                feedback = child_feedback_by_id[child["candidate_id"]]
                scored = _score_feedback(feedback, low_leak_threshold=low_leak_threshold)
                _set_candidate_feedback(child, scored, feedback)
                total_evals += 1

                event = {
                    "round": round_idx + 1,
                    "proposal": proposal_idx + 1,
                    "event_type": "add_candidate",
                    "candidate_id": child["candidate_id"],
                    "lineage_parent_id": child["lineage_parent_id"],
                    "lineage_depth": child["lineage_depth"],
                    "mutation_operator": child["mutation_operator"],
                    "attack_instruction": child["attack_instruction"],
                    "position": child["position"],
                    "score": scored["score"],
                    "reward": scored["avg_reward"],
                    "success_rate": scored["success_rate"],
                    "avg_leakage_ratio": scored["avg_leakage_ratio"],
                    "low_leak_count": scored["low_leak_count"],
                    "low_leak_coverage": scored["low_leak_coverage"],
                    "task_count": scored["task_count"],
                    "visits": child["visits"],
                    "feedback": feedback,
                }
                history.append(event)
                if progress_callback is not None:
                    progress_callback(
                        event,
                        total_evals=total_evals,
                        pool_count=len(pool),
                    )

                if scored["score"] is not None and (
                    best is None or _candidate_quality_key(scored) > _candidate_quality_key(best)
                ):
                    best = {
                        "score": scored["score"],
                        "reward": scored["avg_reward"],
                        "success_rate": scored["success_rate"],
                        "avg_leakage_ratio": scored["avg_leakage_ratio"],
                        "low_leak_count": scored["low_leak_count"],
                        "low_leak_coverage": scored["low_leak_coverage"],
                        "round": round_idx + 1,
                        "proposal": proposal_idx + 1,
                        "candidate_id": child["candidate_id"],
                        "mutation_operator": child["mutation_operator"],
                        "candidate": _candidate_payload(child),
                        "feedback": feedback,
                    }
            completed_steps = step_index + 1
            _emit_checkpoint(
                {
                    "round": round_idx + 1,
                    "proposal": proposal_idx + 1,
                    "event_type": "step_complete",
                    "step_result": "add_candidate",
                    "candidate_id": selected_id,
                    "children_added": len(evaluated_children),
                    "total_evals": total_evals,
                    "pool_count": len(pool),
                }
            )

        if search_exhausted:
            break

    return _build_search_result(
        pool=pool,
        history=history,
        best=best,
        total_evals=total_evals,
        seed_candidate_count=seed_candidate_count,
        score_formula=score_formula,
    )
