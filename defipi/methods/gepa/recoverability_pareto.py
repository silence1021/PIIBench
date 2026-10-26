"""Stage 1-2 recoverability optimizer.

This module is deliberately independent from the v7 greedy engine.  It owns
only the ranking vectors and the small active-pool state machine; providers and
page rendering stay in :mod:`run_recoverability_pareto`.
"""

import copy
import json
import math
import random
from fractions import Fraction

from defipi.evaluation.weighted_recoverability import (
    AGGREGATION_VERSION,
    PROTOCOL_VERSION,
    SCORING_VERSION,
    REWRITE_VERSION,
    objective_fraction,
    weight_fraction,
)
from defipi.methods.gepa.v8_mutation_directions import mutation_direction_contract
from defipi.methods.common.ax_edits import ax_edit_generated_length, ax_edit_identity, ax_edit_preview

STRATEGY = "gepa_recoverability_pareto"
POSITIONS = ("contact_block", "footer_notice", "bio_tail", "header_notice")


def candidate_payload(text, position, budget):
    """Build an AX-edit seed, with an initially empty edit list."""
    if position not in POSITIONS or not isinstance(text, str) or not text.strip():
        raise ValueError("A candidate requires text and a supported position")
    if budget is not None and len(text) > budget:
        raise ValueError(f"Initial/rewrite text exceeds fragment_budget_chars: {len(text)} > {budget}")
    fragment = {"format": "ax_edit_v1", "slot": position, "policy_text": text, "edits": []}
    return {"attack_instruction": text, "position": position, "defense_fragment": fragment,
            "generated_text_length": ax_edit_generated_length(fragment)}


def _vector(candidate):
    ranking = candidate.get("ranking") or candidate
    d_values = ranking.get("d_numerator_vector")
    j_values = ranking.get("j_numerator_vector")
    repeats = ranking.get("repeats")
    if (d_values is not None and j_values is not None and len(d_values) == len(j_values)
            and type(repeats) is int and repeats > 0):
        return tuple(value for pair in zip(
            (Fraction(int(value), repeats * 3) for value in d_values),
            (Fraction(int(value), repeats) for value in j_values)) for value in pair)
    return ()


def ranking_key(candidate):
    ranking = candidate.get("ranking") or candidate
    objective = Fraction(ranking["objective_numerator"], ranking["objective_denominator"])
    d = Fraction(ranking["contact_numerator"], ranking["contact_denominator"])
    joint = Fraction(ranking["joint_numerator"], ranking["joint_denominator"])
    return (objective, d, joint,
            int(candidate["generated_text_length"]), str(candidate["candidate_id"]))


def dominates(left, right):
    """Compare per-case/channel D and J without scalarizing the Pareto front."""
    lv, rv = _vector(left), _vector(right)
    if len(lv) != len(rv):
        return False
    left_ranking, right_ranking = left.get("ranking") or left, right.get("ranking") or right
    if not lv:
        return False
    return all(a <= b for a, b in zip(lv, rv)) and any(a < b for a, b in zip(lv, rv))


def non_dominated(candidates):
    items = list(candidates)
    return [candidate for candidate in items
            if not any(other is not candidate and dominates(other, candidate) for other in items)]


def _near_best(items, tau):
    if not items:
        return []
    best = min(ranking_key(c)[0] for c in items)
    threshold = best + Fraction(str(tau))
    return [c for c in items if ranking_key(c)[0] <= threshold]


def _coverage_gain(candidate, chosen, dimensions):
    vector = _vector(candidate)
    current = [min((_vector(item)[i] for item in chosen), default=Fraction(1)) for i in range(dimensions)]
    return (sum(max(Fraction(0), current[i] - vector[i]) for i in range(0, dimensions, 2)),
            sum(max(Fraction(0), current[i] - vector[i]) for i in range(1, dimensions, 2)))


def select_active_pool(candidates, *, tau, max_k):
    """Select a champion plus complementary componentwise Pareto candidates."""
    ranked = [c for c in candidates if isinstance(c.get("ranking"), dict)
              and all(key in c["ranking"] for key in (
                  "repeats", "channel_order", "component_order",
                  "d_numerator_vector", "d_vector", "j_numerator_vector", "j_vector", "case_count", "valid_count", "expected_count",
                  "contact_numerator", "contact_denominator", "joint_numerator", "joint_denominator",
                  "objective_numerator", "objective_denominator", "joint_weight", "objective_score", "contact_risk", "joint_risk"))]
    schemas = {(r.get("repeats"),
                tuple(r.get("channel_order") or ()), tuple(r.get("component_order") or ()),
                r.get("joint_weight"))
               for r in (c["ranking"] for c in ranked)}
    if len(schemas) > 1:
        raise ValueError("Ranking schema mismatch inside one active-pool update")
    common_schema = next(iter(schemas), None)
    if common_schema is None:
        raise ValueError("No complete comparable ranking schema")
    valid = [c for c in ranked if
             (c.get("ranking") or {}).get("repeats") == common_schema[0]
             and tuple((c.get("ranking") or {}).get("channel_order") or ()) == common_schema[1]
             and tuple((c.get("ranking") or {}).get("component_order") or ()) == common_schema[2]
             and (c.get("ranking") or {}).get("joint_weight") == common_schema[3]
             and (c.get("ranking") or {}).get("comparable") is True
             and (c.get("ranking") or {}).get("pending") is not True
             and type((c.get("ranking") or {}).get("joint_numerator")) is int
             and type((c.get("ranking") or {}).get("joint_denominator")) is int
             and type((c.get("ranking") or {}).get("contact_numerator")) is int
             and type((c.get("ranking") or {}).get("contact_denominator")) is int
             and (c.get("ranking") or {}).get("joint_denominator", 0) > 0
             and (c.get("ranking") or {}).get("contact_denominator", 0) > 0
             and math.isfinite(float((c.get("ranking") or {}).get("joint_risk")))
             and math.isfinite(float((c.get("ranking") or {}).get("contact_risk")))
             and _vector(c)]
    near = _near_best(valid, tau)
    front = non_dominated(near)
    if not front:
        return []
    champion = min(front, key=ranking_key)
    chosen = [champion]
    while len(chosen) < max_k and len(chosen) < len(front):
        remaining = [c for c in front if c not in chosen]
        dimensions = len(_vector(champion))
        chosen_lineages = {str(item["lineage_id"]) for item in chosen}
        def key(item):
            ranking = item.get("ranking") or item
            gain_d, gain_j = _coverage_gain(item, chosen, dimensions)
            return (-gain_d, -gain_j,
                    ranking_key(item)[0], ranking_key(item)[1], ranking_key(item)[2],
                    0 if str(item["lineage_id"]) not in chosen_lineages else 1,
                    int(item.get("expansion_count", 0)),
                    str(item.get("candidate_id", "")))
        chosen.append(sorted(remaining, key=key)[0])
    return chosen


def select_parent(pool, position, rng, epsilon):
    candidates = [c for c in pool if c.get("position") == position and "ranking" in c
                  and (c.get("ranking") or {}).get("comparable") is True]
    if not candidates:
        raise ValueError(f"No active ranked candidates for position {position}")
    champion = min(candidates, key=ranking_key)
    others = [c for c in candidates if c is not champion]
    if others and rng.random() < float(epsilon):
        return rng.choice(others)
    return champion


def _candidate_identity(candidate):
    return ax_edit_identity(candidate["defense_fragment"])


def validate_initial_diversity(initial):
    """Require two distinct nonempty initial texts at each v8 insertion position."""
    for position in POSITIONS:
        texts = {_candidate_identity(candidate)
                 for candidate in initial if candidate.get("position") == position}
        texts.discard("")
        if len(texts) < 2:
            raise ValueError(
                f"v8 requires at least two distinct initial texts for position {position}")


def _valid_peer(candidate, parent, dimensions):
    ranking = candidate.get("ranking")
    if (candidate.get("position") != parent.get("position")
            or candidate is parent
            or (parent.get("candidate_id") is not None
                and candidate.get("candidate_id") == parent.get("candidate_id"))
            or not isinstance(ranking, dict)
            or ranking.get("comparable") is not True
            or ranking.get("pending") is True
            or len(_vector(candidate)) != dimensions):
        return False
    return bool(str(candidate.get("attack_instruction") or "").strip())


def select_mutation_direction(active, parent, *, rng, profile, history):
    """Choose uniformly from all directions, then select a hybrid peer if needed."""
    contract = mutation_direction_contract(profile)
    instruction_by_name = {item["name"]: item["instruction"] for item in contract}
    direction = rng.choice([item["name"] for item in contract])
    peer = None
    if direction != "hybridize_fragment":
        return direction, instruction_by_name[direction], peer

    dimensions = len(_vector(parent))
    parent_identity = _candidate_identity(parent)
    peers = [candidate for candidate in active
             if _valid_peer(candidate, parent, dimensions)
             and _candidate_identity(candidate) != parent_identity]
    gains = []
    for candidate in peers:
        gain_d, gain_j = _coverage_gain(candidate, [parent], dimensions)
        if gain_d > 0 or gain_j > 0:
            gains.append((gain_d, gain_j, candidate))
    if gains:
        best_d = max(item[0] for item in gains)
        gains = [item for item in gains if item[0] == best_d]
        best_j = max(item[1] for item in gains)
        gains = [item for item in gains if item[1] == best_j]
        peer = rng.choice([item[2] for item in gains])
    elif peers:
        peer = rng.choice(peers)
    else:
        history_peers = [candidate for candidate in history
                         if _valid_peer(candidate, parent, dimensions)
                         and _candidate_identity(candidate) != parent_identity]
        if history_peers:
            peer = min(history_peers, key=ranking_key)
    if peer is None:
        raise ValueError("hybridize_fragment selected but no evaluated different-text peer exists")
    return direction, instruction_by_name[direction], peer


def validate_ranking(ranking, manifest, repeats, expected_schema):
    """Reject incomplete/mismatched results before they enter checkpoint state."""
    if not isinstance(ranking, dict) or ranking.get("comparable") is not True or ranking.get("pending") is True:
        raise ValueError("Ranking result is pending or not comparable")
    cases = list(manifest.get("cases") or []) if isinstance(manifest, dict) else list(manifest or [])
    channels = tuple(ranking.get("channel_order") or ())
    component_order = tuple(ranking.get("component_order") or ())
    expected_order = tuple(f"{case['case_id']}::{channel}::{component}" for case in cases for channel in channels for component in ("D", "J"))
    if ranking.get("repeats") != repeats or not channels or component_order != expected_order:
        raise ValueError("Ranking repeat/channel/component order mismatch")
    j_numerators = ranking.get("j_numerator_vector")
    j_values = ranking.get("j_vector")
    joint_denominator = len(cases) * repeats
    contact_denominator = joint_denominator * 3
    d_numerators = ranking.get("d_numerator_vector")
    d_values = ranking.get("d_vector")
    if (ranking.get("protocol_version") != PROTOCOL_VERSION
            or ranking.get("rewrite_version") != REWRITE_VERSION
            or ranking.get("aggregation_version") != AGGREGATION_VERSION
            or ranking.get("metric_version") != SCORING_VERSION
            or ranking.get("case_count") != len(cases)
            or len(j_numerators or ()) != len(expected_order) // 2
            or len(j_values or ()) != len(expected_order) // 2
            or len(d_numerators or ()) != len(expected_order) // 2
            or len(d_values or ()) != len(expected_order) // 2
            or any(type(value) is not int or value < 0 or value > repeats * 3 for value in d_numerators)
            or any(type(value) is not int or value < 0 or value > repeats for value in j_numerators)
            or any(not math.isfinite(float(value)) or not 0 <= float(value) <= 1 for value in d_values)
            or any(not math.isfinite(float(value)) or not 0 <= float(value) <= 1 for value in j_values)):
            raise ValueError("Ranking case/component count mismatch")
    if (type(ranking.get("joint_numerator")) is not int
            or type(ranking.get("contact_numerator")) is not int
            or ranking.get("joint_denominator") != joint_denominator
            or ranking.get("contact_denominator") != contact_denominator
            or not 0 <= ranking["joint_numerator"] <= joint_denominator
            or not 0 <= ranking["contact_numerator"] <= contact_denominator
            or type(ranking.get("objective_numerator")) is not int
            or type(ranking.get("objective_denominator")) is not int
            or ranking.get("objective_denominator") <= 0
            or weight_fraction(ranking.get("joint_weight")) < 0
            or not math.isfinite(float(ranking.get("joint_risk")))
            or not math.isfinite(float(ranking.get("contact_risk")))
            or not math.isfinite(float(ranking.get("objective_score")))):
        raise ValueError("Ranking scalar numerator/denominator contract is incomplete")
    if abs(float(Fraction(ranking["joint_numerator"], joint_denominator)) - float(ranking["joint_risk"])) > 1e-12:
        raise ValueError("Ranking joint risk disagrees with exact numerator")
    if abs(float(Fraction(ranking["contact_numerator"], contact_denominator)) - float(ranking["contact_risk"])) > 1e-12:
        raise ValueError("Ranking contact risk disagrees with exact numerator")
    if any(abs(float(value) - d_numerators[index] / (repeats * 3)) > 1e-12
           for index, value in enumerate(d_values)):
        raise ValueError("Ranking D vector disagrees with exact numerators")
    if any(abs(float(value) - j_numerators[index] / repeats) > 1e-12
           for index, value in enumerate(j_values)):
        raise ValueError("Ranking J vector disagrees with exact numerators")
    component_vector = ranking.get("component_vector")
    expected_components = [value for pair in zip(d_values, j_values) for value in pair]
    if (len(component_vector or ()) != len(expected_components)
            or any(abs(float(value) - expected_components[index]) > 1e-12
                   for index, value in enumerate(component_vector or ()))):
        raise ValueError("Ranking interleaved component vector disagrees with D/J vectors")
    channel_count = len(channels)
    expected_d = sum(max(d_numerators[offset:offset + channel_count])
                     for offset in range(0, len(d_numerators), channel_count))
    expected_j = sum(max(j_numerators[offset:offset + channel_count])
                     for offset in range(0, len(j_numerators), channel_count))
    if ranking["contact_numerator"] != expected_d or ranking["joint_numerator"] != expected_j:
        raise ValueError("Ranking aggregate numerator disagrees with per-case/channel vectors")
    objective = objective_fraction(ranking["contact_numerator"], contact_denominator,
                                   ranking["joint_numerator"], joint_denominator,
                                   ranking["joint_weight"])
    if objective != Fraction(ranking["objective_numerator"], ranking["objective_denominator"]):
        raise ValueError("Ranking objective disagrees with exact numerator")
    if abs(float(objective) - float(ranking["objective_score"])) > 1e-12:
        raise ValueError("Ranking objective display disagrees with exact numerator")
    expected_count = len(cases) * repeats * len(channels)
    if ranking.get("expected_count") != expected_count or ranking.get("valid_count") != expected_count:
        raise ValueError("Ranking valid/expected observation count mismatch")
    schema = (ranking.get("repeats"), channels, component_order,
              ranking.get("joint_weight"))
    if expected_schema is not None:
        expected_schema = (expected_schema[0], tuple(expected_schema[1]), tuple(expected_schema[2]), expected_schema[3])
    if expected_schema is not None and schema != expected_schema:
        raise ValueError("Ranking schema differs from the frozen manifest schema")
    return schema


def run_search(initial, feedback_ids, ranking_manifest, *, evaluate, mutate, save,
               rng_seed, max_extensions_per_position, stagnation_patience, tau,
               epsilon, max_k, ranking_repeats, parent_repeats,
               minibatch_size, budget, joint_weight, checkpoint, fragment_profile):
    joint_weight = weight_fraction(joint_weight)
    if type(stagnation_patience) is not int or stagnation_patience < 1:
        raise ValueError("stagnation_patience must be a positive integer")
    direction_contract = mutation_direction_contract(fragment_profile)
    if checkpoint is not None:
        if checkpoint.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("Old GEPA checkpoints cannot be resumed by gepa_recoverability_pareto")
        if checkpoint.get("joint_weight") != str(joint_weight):
            raise ValueError("Checkpoint joint_weight does not match the requested weighted objective")
        if (checkpoint.get("rewrite_version") != REWRITE_VERSION
                or checkpoint.get("mutation_direction_contract") != direction_contract
                or checkpoint.get("mutation_rng_seed") is None):
            raise ValueError("Checkpoint rewrite/direction contract does not match the requested run")
        if checkpoint.get("stagnation_patience") != stagnation_patience:
            raise ValueError("Checkpoint stagnation_patience does not match the requested run")
        stagnation = checkpoint.get("stagnation_per_position")
        if (not isinstance(stagnation, dict) or set(stagnation) != set(POSITIONS)
                or any(type(value) is not int or value < 0 for value in stagnation.values())):
            raise ValueError("Checkpoint stagnation state is missing or invalid")
        stop_reasons = checkpoint.get("stop_reasons_by_position")
        if (not isinstance(stop_reasons, dict) or set(stop_reasons) != set(POSITIONS)
                or any(value not in (None, "max_extensions_per_position", "stagnation_patience")
                       for value in stop_reasons.values())):
            raise ValueError("Checkpoint stop-reason state is missing or invalid")
        state = copy.deepcopy(checkpoint)
    else:
        validate_initial_diversity(initial)
        pool = [{**copy.deepcopy(c), "candidate_id": i} for i, c in enumerate(initial)]
        for item in pool:
            item["lineage_id"] = item["candidate_id"]
        state = {"protocol_version": PROTOCOL_VERSION, "phase": "initialization", "pool": pool,
                 "initial_count": len(pool), "initial_index": 0, "round": 0,
                 "extensions_per_position": {p: 0 for p in POSITIONS},
                 "children_generated_per_position": {p: 0 for p in POSITIONS},
                 "active_pool_by_position": {p: [] for p in POSITIONS},
                 "history_by_position": {p: [] for p in POSITIONS}, "pending": None,
                 "position_cursor": 0,
                 "stop_reasons_by_position": {p: None for p in POSITIONS},
                 "stagnation_patience": stagnation_patience,
                 "stagnation_per_position": {p: 0 for p in POSITIONS},
                 "rng_state": random.Random(rng_seed).getstate(),
                 "tau": tau, "epsilon": epsilon, "max_k": max_k,
                 "joint_weight": str(joint_weight), "rewrite_version": REWRITE_VERSION,
                 "mutation_direction_contract": copy.deepcopy(direction_contract),
                 "mutation_rng_seed": rng_seed,
                 "scoring_version": SCORING_VERSION, "aggregation_version": AGGREGATION_VERSION}
    if state.get("ranking_schema") is not None:
        schema = state["ranking_schema"]
        state["ranking_schema"] = (schema[0], tuple(schema[1]), tuple(schema[2]), schema[3])
    rng = random.Random(); rng.setstate(_tuples(state["rng_state"]))

    def persist(status="running", error=None):
        state["rng_state"] = rng.getstate()
        save(copy.deepcopy(state), status, error)

    try:
        while state["initial_index"] < state["initial_count"]:
            index = state["initial_index"]
            candidate = state["pool"][index]
            state["pending"] = {"stage": "initial_ranking", "candidate_id": index}
            persist()
            if not isinstance(candidate.get("ranking"), dict):
                ranking = evaluate(candidate, ranking_manifest, ranking_repeats,
                                   f"initial/{index}/ranking", False)
                schema = validate_ranking(
                    ranking, ranking_manifest, ranking_repeats, state.get("ranking_schema"))
                if weight_fraction(ranking.get("joint_weight")) != joint_weight:
                    raise ValueError("Initial ranking joint_weight differs from the requested objective")
                state["ranking_schema"] = schema
                candidate["ranking"] = ranking
            candidate["history_index"] = len(state["history_by_position"][candidate["position"]])
            state["history_by_position"][candidate["position"]].append(index)
            state["initial_index"] += 1; state["pending"] = None
            persist()
        if state["phase"] == "initialization":
            for position in POSITIONS:
                ids = state["history_by_position"][position]
                active = select_active_pool([state["pool"][i] for i in ids], tau=tau, max_k=max_k)
                state["active_pool_by_position"][position] = [c["candidate_id"] for c in active]
        state["phase"] = "search"; persist()
        while any(state["stop_reasons_by_position"][p] is None
                  and state["extensions_per_position"][p] < max_extensions_per_position
                  for p in POSITIONS):
            pending = state["pending"]
            if pending is None:
                candidates = [p for p in POSITIONS
                              if state["stop_reasons_by_position"][p] is None
                              and state["extensions_per_position"][p] < max_extensions_per_position]
                candidates = [p for p in candidates if state["active_pool_by_position"][p]]
                if not candidates:
                    raise RuntimeError("No active comparable candidate remains; refusing to fabricate completion")
                position = next((POSITIONS[(state["position_cursor"] + i) % len(POSITIONS)]
                                 for i in range(len(POSITIONS)) if POSITIONS[(state["position_cursor"] + i) % len(POSITIONS)] in candidates), candidates[0])
                active_ids = list(state["active_pool_by_position"][position])
                active = [state["pool"][i] for i in active_ids]
                parent = select_parent(active, position, rng, epsilon=epsilon)
                if minibatch_size > len(feedback_ids):
                    raise ValueError("feedback minibatch exceeds feedback pool")
                page_ids = rng.sample(list(feedback_ids), minibatch_size)
                direction_seed = f"v8-direction-v1::{state['mutation_rng_seed']}::{position}::{state['extensions_per_position'][position]}"
                direction_rng = random.Random(direction_seed)
                mutation_direction, mutation_instruction, peer = select_mutation_direction(
                    active, parent, rng=direction_rng, profile=fragment_profile,
                    history=[state["pool"][i]
                             for i in state["history_by_position"][position]])
                pending = {"stage": "parent_feedback", "position": position,
                           "parent_id": parent["candidate_id"], "page_ids": page_ids,
                           "child_index": 0, "child_ids": [], "feedback": None,
                           "mutation_direction": mutation_direction,
                           "mutation_instruction": mutation_instruction,
                           "peer_candidate_id": peer.get("candidate_id") if peer else None,
                           "peer_text": peer.get("attack_instruction") if peer else None}
                pending["peer_edits"] = copy.deepcopy(
                    (peer.get("defense_fragment") or {}).get("edits") or []) if peer else []
                state["pending"] = pending; persist()
            position = pending["position"]
            parent = state["pool"][pending["parent_id"]]
            job = f"extension/{position}/{state['extensions_per_position'][position]}"
            if pending["stage"] == "parent_feedback":
                pending["feedback"] = evaluate(parent, pending["page_ids"], parent_repeats,
                                                job + "/parent_feedback", True)
                pending["stage"] = "child_mutation"; persist()
            if pending["stage"] == "child_mutation":
                direction = "proposal-0"
                existing = [state["pool"][i]["attack_instruction"]
                            for i in state["history_by_position"][position]]
                peer = next((item for item in state["pool"]
                             if item["candidate_id"] == pending["peer_candidate_id"]), {})
                mutation_parent = {**parent, "_existing_texts": existing,
                                   "_mutation_direction": pending["mutation_direction"],
                                   "_mutation_instruction": pending["mutation_instruction"],
                                   "_peer_candidate_id": pending["peer_candidate_id"],
                                   "_peer_text": pending["peer_text"]}
                mutation_parent["_existing_fragments"] = [
                    state["pool"][i]["defense_fragment"]
                    for i in state["history_by_position"][position]]
                mutation_parent["_peer_policy_text"] = (
                    peer["defense_fragment"]["policy_text"]
                    if peer else "")
                mutation_parent["_peer_edits"] = copy.deepcopy(
                    peer["defense_fragment"]["edits"] if peer else [])
                child_text = mutate(mutation_parent, pending["feedback"],
                                    job + "/" + direction)
                if not isinstance(child_text, dict) or child_text.get("format") != "ax_edit_v1":
                    raise ValueError("Mutation must return an ax_edit_v1 fragment")
                from defipi.methods.common.ax_edits import normalize_ax_edit_fragment
                fragment = normalize_ax_edit_fragment({**child_text, "slot": position})
                child = candidate_payload(ax_edit_preview(fragment), position, budget)
                child["defense_fragment"] = fragment
                child["generated_text_length"] = ax_edit_generated_length(fragment)
                if pending["child_index"] == 0:
                    parent["expansion_count"] = parent.get("expansion_count", 0) + 1
                child.update(candidate_id=len(state["pool"]), parent_id=parent["candidate_id"],
                             rewrite_round=state["extensions_per_position"][position] + 1,
                             direction=direction, parent_expansion_count=parent.get("expansion_count", 0),
                             mutation_direction=pending["mutation_direction"],
                             peer_candidate_id=pending["peer_candidate_id"],
                             lineage_id=parent["lineage_id"],
                             expansion_count=0)
                state["pool"].append(child); state["history_by_position"][position].append(child["candidate_id"])
                pending["child_ids"].append(child["candidate_id"]); pending["current_child_id"] = child["candidate_id"]
                pending["stage"] = "child_ranking"
                state["children_generated_per_position"][position] += 1; persist()
            if pending["stage"] == "child_ranking":
                child = state["pool"][pending["current_child_id"]]
                if not isinstance(child.get("ranking"), dict):
                    ranking = evaluate(child, ranking_manifest, ranking_repeats,
                                       job + "/" + child["direction"] + "/ranking", False)
                    validate_ranking(ranking, ranking_manifest, ranking_repeats, state.get("ranking_schema"))
                    child["ranking"] = ranking
                pending["child_index"] += 1; pending.pop("current_child_id", None)
                pending["stage"] = "update_pool"
                persist()
            if pending["stage"] == "update_pool":
                previous_active = [state["pool"][i]
                                   for i in state["active_pool_by_position"][position]]
                previous_best_l = min(ranking_key(candidate)[0] for candidate in previous_active)
                candidates = list(previous_active)
                candidates += [state["pool"][i] for i in pending["child_ids"]]
                selected = select_active_pool(candidates, tau=tau, max_k=max_k)
                state["active_pool_by_position"][position] = [c["candidate_id"] for c in selected]
                current_best_l = min(ranking_key(candidate)[0] for candidate in selected)
                if current_best_l < previous_best_l:
                    state["stagnation_per_position"][position] = 0
                else:
                    state["stagnation_per_position"][position] += 1
                state["extensions_per_position"][position] += 1
                if state["extensions_per_position"][position] >= max_extensions_per_position:
                    state["stop_reasons_by_position"][position] = "max_extensions_per_position"
                elif state["stagnation_per_position"][position] >= stagnation_patience:
                    state["stop_reasons_by_position"][position] = "stagnation_patience"
                state["round"] += 1
                state["position_cursor"] = (POSITIONS.index(position) + 1) % len(POSITIONS)
                state["pending"] = None; persist()
        state["phase"] = "completed"; persist("completed")
    except BaseException as exc:
        persist("interrupted", f"{type(exc).__name__}: {exc}"); raise
    return state


def _tuples(value):
    return tuple(_tuples(v) for v in value) if isinstance(value, list) else value


def export_result(state):
    ranked = sorted(
        (state["pool"][i] for ids in state["active_pool_by_position"].values()
         for i in ids if i < len(state["pool"])), key=ranking_key)
    top = [{"search_rank": i + 1, "candidate": copy.deepcopy(c),
            "search_stats": {"candidate_id": c.get("candidate_id"),
                             "contact_risk": (c.get("ranking") or {}).get("contact_risk"),
                             "contact_numerator": (c.get("ranking") or {}).get("contact_numerator"),
                             "contact_denominator": (c.get("ranking") or {}).get("contact_denominator"),
                             "joint_risk": (c.get("ranking") or {}).get("joint_risk"),
                             "joint_numerator": (c.get("ranking") or {}).get("joint_numerator"),
                             "joint_denominator": (c.get("ranking") or {}).get("joint_denominator"),
                             "objective_score": (c.get("ranking") or {}).get("objective_score"),
                             "objective_numerator": (c.get("ranking") or {}).get("objective_numerator"),
                             "objective_denominator": (c.get("ranking") or {}).get("objective_denominator"),
                             "joint_weight": (c.get("ranking") or {}).get("joint_weight"),
                             "j_vector": (c.get("ranking") or {}).get("j_vector"),
                             "direction": c.get("direction"),
                             "score_direction": "minimize"}}
           for i, c in enumerate(ranked)]
    by_position = {p: next((x for x in top if x["candidate"].get("position") == p), {}) for p in POSITIONS}
    best = {position: copy.deepcopy(by_position[position].get("candidate", {}))
            for position in POSITIONS}
    return {"search_strategy": STRATEGY, "protocol_version": PROTOCOL_VERSION,
            "score_direction": "minimize", "best": best,
            "active_pool_by_position": copy.deepcopy(state["active_pool_by_position"]),
            "history_by_position": copy.deepcopy(state["history_by_position"]),
            "pool": copy.deepcopy(state["pool"]),
            "extensions_per_position": copy.deepcopy(state["extensions_per_position"]),
            "children_generated_per_position": copy.deepcopy(state["children_generated_per_position"]),
            "stagnation_patience": state["stagnation_patience"],
            "stagnation_per_position": copy.deepcopy(state["stagnation_per_position"]),
            "stop_reasons_by_position": copy.deepcopy(state["stop_reasons_by_position"])}, top, by_position
