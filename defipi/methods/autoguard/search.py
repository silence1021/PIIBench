"""Two-phase AutoGuard search with shared contact-D/J loss and EXP3-IX."""
from __future__ import annotations

from defipi.jsonio import content_key
import json
import math
import multiprocessing
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from defipi.methods.autoguard.data import PATTERNS, validate_cases
from defipi.methods.autoguard.scoring import (
    LLMTrainingScorer,
    contact_diagnostics,
    evaluate_final_trajectory,
    outgoing_user_messages,
    score_episode,
    TRAINING_OBJECTIVE,
    episode_feedback,
    FEEDBACK_VERSION,
)
from defipi.methods.autoguard.runtime import (
    MAX_ATTEMPTS,
    agent_slots,
    call_model,
    request_attempt_scope,
    runtime_contract,
    search_workers,
    exclusive_search_run,
)
from functools import partial
from defipi.jsonio import write_json


_json_write = partial(write_json, default=str)


def _jsonl_append(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def _tuple_tree(value):
    return tuple(_tuple_tree(v) for v in value) if isinstance(value, list) else value


def _response_text(response):
    if isinstance(response, str):
        return response
    return str((response or {}).get("text") or "")


class EXP3IX:
    """EXP3-IX with the paper's raw, potentially >1 reward and no clipping."""
    def __init__(self, arms, *, T, alpha, beta):
        if not arms:
            raise ValueError("EXP3-IX needs a non-empty frozen candidate library")
        self.arms = list(arms)
        self.K = len(self.arms)
        from defipi.configuration.schema import Field, validate_config
        validate_config({"T": T, "alpha": alpha, "beta": beta}, {
            "T": Field(int, minimum=1), "alpha": Field((int, float), minimum=0),
            "beta": Field((int, float), minimum=0)})
        self.T = T
        self.alpha = alpha
        self.beta = beta
        self.eta = math.sqrt(2.0 * math.log(self.K) / (self.T * self.K)) if self.K > 1 else 0.0
        self.gamma = self.eta / 2.0
        self.log_weights = [0.0] * self.K
        self.counts = [0] * self.K
        self.reward_sums = [0.0] * self.K

    @property
    def probabilities(self):
        if self.K == 1:
            return [1.0]
        peak = max(self.log_weights)
        weights = [math.exp(x - peak) for x in self.log_weights]
        total = sum(weights)
        return [x / total for x in weights]

    def select(self, rng):
        ps = self.probabilities
        draw, cumulative = rng.random(), 0.0
        for i, p in enumerate(ps):
            cumulative += p
            if draw <= cumulative or i == self.K - 1:
                return i, p

    def update(self, selected, probability, reward):
        reward = float(reward)
        loss = 1.0 - reward
        estimated_loss = loss / (float(probability) + self.gamma)
        self.log_weights[selected] -= self.eta * estimated_loss
        self.counts[selected] += 1
        self.reward_sums[selected] += reward
        return {"raw_reward": reward, "loss": loss, "estimated_loss": estimated_loss,
                "eta": self.eta, "gamma": self.gamma}

    def winners(self):
        sampled = [i for i, n in enumerate(self.counts) if n > 0]
        if not sampled:
            return {"mean_winner_id": None, "cumulative_winner_id": None}
        mean = lambda i: self.reward_sums[i] / self.counts[i]
        mean_winner = max(sampled, key=lambda i: (mean(i), -i))
        cumulative_winner = max(sampled, key=lambda i: (self.reward_sums[i], -i))
        return {
            "mean_winner_id": self.arms[mean_winner]["id"],
            "mean_winner_count": self.counts[mean_winner],
            "mean_winner_reward_sum": self.reward_sums[mean_winner],
            "mean_winner_empirical_mean": mean(mean_winner),
            "cumulative_winner_id": self.arms[cumulative_winner]["id"],
            "cumulative_winner_count": self.counts[cumulative_winner],
            "cumulative_winner_reward_sum": self.reward_sums[cumulative_winner],
            "cumulative_winner_empirical_mean": mean(cumulative_winner),
            "winners_disagree": mean_winner != cumulative_winner,
        }


class AutoGuardSearch:
    def __init__(self, config, *, cases, model_client_factory, episode_runner=None, scorer_factory=None,
                 executor_factory=None):
        from defipi.methods.autoguard.configuration import validate_runtime_config
        validate_runtime_config(config)
        self.config = dict(config)
        self.paid_authorized = False
        self.cases = list(cases)
        self.splits = validate_cases(self.cases)
        self.model = dict(config["model_override"])
        self.model_client_factory = model_client_factory
        self.episode_runner = episode_runner
        self.scorer_factory = scorer_factory
        self.workers = search_workers(config)
        self.executor_factory = executor_factory
        self._record_ids = {}
        self.output_dir = Path(config["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_dir = Path(__file__).parent / "prompts"
        self.rng = random.Random(config["seed"])
        self.library_path = self.output_dir / "frozen_library.jsonl"
        self.state_path = self.output_dir / "search_checkpoint.json"
        self.scorer = None
        self.request_log = self.output_dir / "role_requests_responses.jsonl"


    def _default_episode_runner(self):
        from defipi.methods.autoguard.browsergym_agent import run_episode
        return run_episode

    @contextmanager
    def _job_pool(self):
        if self.workers == 1:
            yield None
            return
        pool = (self.executor_factory(max_workers=self.workers) if self.executor_factory else
                ProcessPoolExecutor(max_workers=self.workers, mp_context=multiprocessing.get_context("spawn")))
        with pool:
            yield pool

    def _run_jobs(self, pool, jobs):
        from defipi.methods.autoguard.parallel import execute_job
        def arguments(job):
            return (self.config, job, self.model_client_factory, self.episode_runner, self.scorer_factory)
        if pool is None:
            for job in jobs:
                path = execute_job(*arguments(job))
                yield job, json.loads(Path(path).read_text())
            return
        futures = {pool.submit(execute_job, *arguments(job)): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                result = json.loads(Path(future.result()).read_text())
            except Exception as exc:
                result = {"valid": False, "error_stage": "worker", "error": f"{type(exc).__name__}:{exc}"}
            yield job, result

    def _append_record(self, path, row, key="job_id"):
        """Coordinator-only journal append, idempotent after checkpoint interruption."""
        path = Path(path)
        cache_key = (str(path), key)
        if cache_key not in self._record_ids:
            existing = set()
            if path.exists():
                with path.open() as stream:
                    existing = {json.loads(line).get(key) for line in stream if line.strip()}
            self._record_ids[cache_key] = existing
        if row[key] not in self._record_ids[cache_key]:
            _jsonl_append(path, row)
            self._record_ids[cache_key].add(row[key])

    def _load_scorer(self):
        if self.scorer is not None:
            return self.scorer
        if self.scorer_factory:
            self.scorer = self.scorer_factory(self.model, self.output_dir / "scorer")
        else:
            self.scorer = LLMTrainingScorer(self.model, self.model_client_factory,
                                            artifact_dir=self.output_dir / "scorer")
        return self.scorer

    def _new_client(self, role):
        return self.model_client_factory(self.model, role)

    def _generate(self, role, system, user):
        client = self._new_client(role)
        request = {"role": role, "model": self.model["model"], "backend": self.model["backend"],
                   "reasoning_effort": self.model["reasoning_effort"], "temperature": self.model["temperature"], "max_tokens": self.model["max_tokens"],
                   "system": system, "messages": [{"role": "user", "content": user}],
                   "api_payload": {"model": self.model["model"], "messages": [
                       {"role": "system", "content": system}, {"role": "user", "content": user}],
                       "reasoning_effort": self.model["reasoning_effort"]}, "http_attempts": 0}
        def before_request_attempt():
            request["http_attempts"] += 1
        try:
            response = call_model(client, messages=request["messages"], system=system, temperature=self.model["temperature"],
                                       max_tokens=self.model["max_tokens"], reasoning_effort=self.model["reasoning_effort"],
                                       before_request_attempt=before_request_attempt)
        except Exception as exc:
            request["error"] = f"{type(exc).__name__}:{exc}"
            _jsonl_append(self.request_log, request)
            raise
        if isinstance(response, dict) and response.get("error"):
            raise RuntimeError(f"{role}_provider_error:{response['error']}")
        request["response"] = _response_text(response)
        request["usage"] = response.get("usage") if isinstance(response, dict) else None
        _jsonl_append(self.request_log, request)
        return request["response"]

    def _episode(self, text, case, phase, case_index):
        runner = self.episode_runner
        if runner is None:
            runner = self._default_episode_runner()
        model_case = {key: value for key, value in case.items() if key not in
                      {"provenance", "source_artifact", "source_seed_id", "parent_id", "parent_question", "prompt_template"}}
        identity = f"{phase}:{case_index}:{case['id']}:{text}"
        if phase == "heldout":
            identity += f":evaluation-{time.time_ns()}"
        ledger_key = content_key(identity)[:16]
        ledger_path = self.output_dir / "episode_attempts" / phase / f"{ledger_key}.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"case_id": case["id"], "attempts": []}
        def read_episode(path):
            return {**json.loads(Path(path).read_text()), "episode_path": str(path)}

        def failed_episode(reason):
            return {"valid": False, "invalid_reason": reason, "steps": [],
                    "provider_requests": [], "final_output": None, "first_payload_step": None,
                    "baseline": text is None, "defense_enabled": text is not None,
                    "injection": {"injected": False, "payload_text": text}}

        def finish_attempt(row, episode):
            source = Path(row["episode_path"])
            # The browser owns its terminal artifact. Scripted runners and
            # failures before browser startup still receive one canonical file.
            if not source.exists():
                _json_write(source, episode)
            episode = read_episode(source)
            outcome = score_episode(case, episode)
            row.update(status="valid" if outcome["valid"] else "invalid",
                       invalid_reason=outcome.get("invalid_reason"))
            if outcome["valid"]:
                ledger["valid_episode_path"] = str(source)
            _json_write(ledger_path, ledger)
            return episode, outcome

        if ledger.get("valid_episode_path"):
            return read_episode(ledger["valid_episode_path"])
        # Recover the canonical browser result before spending another attempt.
        # An interrupted reservation is consumed even if no browser file exists.
        for row in ledger["attempts"]:
            if row["status"] != "started":
                continue
            episode, outcome = finish_attempt(row, failed_episode("episode_attempt_interrupted"))
            if outcome["valid"]:
                return episode
        while len(ledger["attempts"]) < MAX_ATTEMPTS:
            attempt = len(ledger["attempts"]) + 1
            episode_dir = self.output_dir / "episodes" / phase / f"{ledger_key}-attempt-{attempt}"
            row = {"attempt": attempt, "status": "started",
                   "episode_path": str(episode_dir / "browsergym_episode.json")}
            ledger["attempts"].append(row)
            _json_write(ledger_path, ledger)
            try:
                with agent_slots(self.config):
                    episode = runner(candidate_text=text, case=model_case, model=self.model, config=self.config,
                                     artifact_dir=str(episode_dir), client_factory=self.model_client_factory)
            except Exception as exc:
                episode = failed_episode(f"episode_error:{type(exc).__name__}:{exc}")
            episode, outcome = finish_attempt(row, episode)
            if outcome["valid"]:
                return episode
        last = read_episode(ledger["attempts"][-1]["episode_path"])
        return {**last, "retry_exhausted": True,
                "attempt_count": len(ledger["attempts"]), "attempts_file": str(ledger_path)}

    @staticmethod
    def _feedback_episode(episode):
        return episode_feedback(episode)

    def _scorer_call(self, case, episode):
        return self._load_scorer().score(case, episode)

    @request_attempt_scope()
    def _heldout_scorer_call(self, case, episode):
        """Retry diagnostic feedback once without discarding the deterministic outcome."""
        attempts = []
        for attempt in (1, 2):
            try:
                score = dict(self._scorer_call(case, episode))
            except Exception as exc:
                score = {**score_episode(case, episode), "feedback_valid": False,
                         "feedback_invalid_reason": f"scorer_error:{type(exc).__name__}:{exc}",
                         "explicit_refusal": None, "raw_response": None}
            attempts.append({"judge_attempt": attempt, **score,
                             "error": score.get("feedback_invalid_reason")})
            if not score.get("valid") or score.get("feedback_valid", True):
                break
        return {**score, "attempts": attempts}

    def _save_discovery_state(self, state):
        state["rng_state"] = self.rng.getstate()
        _json_write(self.state_path, state)

    def _read_discovery_state(self):
        if not self.state_path.exists():
            return {"phase": "discovery", "complete": False, "attempts": 0, "refinements": 0,
                    "library": [], "pending": None, "rng_state": None}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("phase") not in {"discovery", "bandit"}:
            raise ValueError("checkpoint is not in discovery phase")
        if state.get("rng_state"):
            self.rng.setstate(_tuple_tree(state["rng_state"]))
        return state

    @exclusive_search_run
    def discover(self, *, attempt_budget, step_budget, paid_authorized=False):
        if not paid_authorized:
            raise PermissionError("discovery makes paid model calls; pass explicit paid execution authorization")
        if type(attempt_budget) is not int or attempt_budget <= 0:
            raise ValueError("an explicit positive discovery attempt_budget is required")
        if type(step_budget) is not int or step_budget <= 0:
            raise ValueError("an explicit positive per-episode step_budget is required for discovery")
        self.config["browser"]["episode_step_budget"] = step_budget
        self.paid_authorized = True
        self._refresh_manifest()
        target_k = self.config["search"]["K"]
        if target_k != 100:
            raise ValueError("formal paper config requires K=100")
        state = self._read_discovery_state()
        if state.get("phase") == "bandit" and state.get("discovery_complete"):
            self._freeze_cases()
            self._refresh_manifest()
            return {"phase": "discovery", "complete": True, "library": self._load_library(),
                    "attempts": state.get("discovery_attempts")}
        if state.get("complete"):
            self._freeze_cases()
            self._refresh_manifest()
            return state
        self._freeze_cases()
        self._refresh_manifest()
        generation_prompt = (self.prompt_dir / "defender.txt").read_text(encoding="utf-8")
        refine_prompt = (self.prompt_dir / "refine.txt").read_text(encoding="utf-8")
        discovery_cases = self.splits["discovery"]
        if not discovery_cases:
            raise ValueError("discovery cases are missing")
        state["discovery_attempt_budget"] = attempt_budget
        chains = state.setdefault("chains", {})
        state.setdefault("wave", [])
        with self._job_pool() as pool:
            while len(state["library"]) < target_k:
                if not state["wave"]:
                    if state["attempts"] >= attempt_budget:
                        break
                    # Reserve at most one eventual library place per active chain.
                    # Random draws and proposal IDs never depend on finish order.
                    desired = min(self.workers, target_k - len(state["library"]))
                    for slot in range(self.workers):
                        if len(chains) >= desired:
                            break
                        if str(slot) not in chains:
                            chains[str(slot)] = {"case": self.rng.choice(discovery_cases),
                                                 "lineage": [], "stage": "defender"}
                    for slot, chain in sorted(chains.items()):
                        if state["attempts"] >= attempt_budget:
                            break
                        case = chain["case"]
                        role = "defender" if chain["stage"] == "defender" else "refiner"
                        if role == "defender":
                            value = {"case_id": case["id"], "question": case["question"],
                                     "pattern": case["pattern"], "page_id": case.get("page_id")}
                            prompt = generation_prompt
                        else:
                            previous = json.loads(Path(chain["last_job_state"]).read_text())
                            value = {"candidate": chain["candidate_text"], "task": case["question"],
                                     "case_id": case["id"], "case_pattern": case["pattern"],
                                     "scorer_feedback": previous["score"],
                                     "previous_trajectory": self._feedback_episode(json.loads(Path(previous["episode_path"]).read_text()))}
                            prompt = refine_prompt
                        state["attempts"] += 1
                        index = state["attempts"]
                        job_id = f"proposal-{index:06d}"
                        state["wave"].append({
                            "id": job_id, "phase": "discovery", "index": index, "slot": slot,
                            "case": case, "directory": str(self.output_dir / "jobs/discovery" / job_id),
                            "generation": {"role": role, "system": prompt,
                                           "user": json.dumps(value, ensure_ascii=False, default=str)},
                        })
                    self._save_discovery_state(state)
                # Each worker finishes one generation -> episode -> score attempt.
                # Drain the bounded wave and commit in reserved order, so a fast
                # response cannot win a library place ahead of an earlier job.
                results = sorted(self._run_jobs(pool, state["wave"]), key=lambda pair: pair[0]["index"])
                blocked, errors = [], []
                for job, outcome in results:
                    chain = chains[job["slot"]]
                    if not outcome.get("valid"):
                        errors.append({"job_id": job["id"], "stage": outcome.get("error_stage"),
                                       "error": outcome.get("error")})
                        # A failed generation consumed its proposal budget. An
                        # episode/worker failure resumes this same durable job.
                        if outcome.get("error_stage") != "generation":
                            blocked.append(job)
                        continue
                    text, score = outcome["candidate_text"], outcome["score"]
                    candidate_id = content_key(text)[:16]
                    chain["lineage"].append({"candidate_id": candidate_id, "case_id": job["case"]["id"],
                                              "score": score, "episode_path": outcome["episode_path"]})
                    self._append_record(self.output_dir / "discovery_attempts.jsonl", {
                        "job_id": job["id"], "attempt": job["index"], "candidate_id": candidate_id,
                        "candidate_text": text, "case_id": job["case"]["id"], "score": score, "episode_path": outcome["episode_path"]})
                    if score["success"]:
                        if all(candidate["text"] != text for candidate in state["library"]):
                            candidate = {"id": candidate_id, "text": text,
                                         "source_case_id": job["case"]["id"], "lineage": chain["lineage"]}
                            state["library"].append(candidate)
                            self._append_record(self.library_path, candidate, key="id")
                        del chains[job["slot"]]
                    else:
                        state["refinements"] += 1
                        chain.update(stage="refine", candidate_text=text,
                                     last_job_state=str(Path(job["directory"]) / "job_state.json"))
                state["wave"] = blocked
                state["job_errors"] = errors
                self._save_discovery_state(state)
                if errors:
                    state.update(paused=True, pause_reason="parallel discovery job failed; completed jobs preserved")
                    self._save_discovery_state(state)
                    return state
        state["complete"] = len(state["library"]) == target_k
        state["paused"] = not state["complete"]
        state["pause_reason"] = None if state["complete"] else "explicit discovery-attempt budget exhausted"
        self._save_discovery_state(state)
        if state["complete"]:
            _json_write(self.output_dir / "frozen_library.json", state["library"])
        return state

    def _load_library(self):
        if not self.state_path.exists():
            raise FileNotFoundError("discovery checkpoint is missing; complete discovery first")
        discovery = json.loads(self.state_path.read_text(encoding="utf-8"))
        discovery_finished = (discovery.get("phase") == "discovery" and discovery.get("complete")) or (
            discovery.get("phase") == "bandit" and discovery.get("discovery_complete"))
        if not discovery_finished:
            raise ValueError("discovery has not completed; partial candidates cannot start bandit training")
        path = self.output_dir / "frozen_library.json"
        if not path.exists():
            if not self.library_path.exists():
                raise FileNotFoundError("frozen candidate library is missing; complete discovery first")
            rows = [json.loads(line) for line in self.library_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            rows = json.loads(path.read_text(encoding="utf-8"))
        if len(rows) != 100:
            raise ValueError(f"formal bandit requires a frozen K=100 library, got {len(rows)}")
        texts = [str(row.get("text") or "") for row in rows]
        ids = [str(row.get("id") or "") for row in rows]
        if any(not text.strip() for text in texts) or len(set(texts)) != 100 or len(set(ids)) != 100:
            raise ValueError("frozen library must contain 100 unique, non-empty free-text candidates")
        return rows

    def _load_train_state(self, arms):
        state = {}
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("phase") == "bandit":
                if state.get("rng_state"):
                    self.rng.setstate(_tuple_tree(state["rng_state"]))
                return state
            if state.get("phase") == "discovery":
                if not state.get("complete"):
                    raise ValueError("discovery checkpoint is incomplete")
                if state.get("rng_state"):
                    self.rng.setstate(_tuple_tree(state["rng_state"]))
        bandit = EXP3IX(arms, T=self.config["search"]["T"],
                        alpha=self.config["search"]["alpha"], beta=self.config["search"]["beta"])
        return {"phase": "bandit", "round_index": 0,
                "discovery_complete": True,
                "discovery_attempts": state.get("attempts") if self.state_path.exists() else None,
                "log_weights": bandit.log_weights, "counts": bandit.counts,
                "reward_sums": bandit.reward_sums, "failure_by_model": {}, "failure_by_pattern": {},
                "pending_round": None, "rounds": [], "rng_state": None}

    @exclusive_search_run
    def train(self, *, step_budget, paid_authorized=False):
        if not paid_authorized:
            raise PermissionError("bandit training makes paid model calls; pass explicit paid execution authorization")
        if type(step_budget) is not int or step_budget <= 0:
            raise ValueError("an explicit positive per-episode step_budget is required for a paid run")
        self.config["browser"]["episode_step_budget"] = step_budget
        self.paid_authorized = True
        self._refresh_manifest()
        if self.config["search"]["T"] != 500:
            raise ValueError("formal paper config requires T=500")
        validate_cases(self.cases, require_paper_cardinality=True)
        arms = self._load_library()
        bandit = EXP3IX(arms, T=self.config["search"]["T"],
                        alpha=self.config["search"]["alpha"], beta=self.config["search"]["beta"])
        state = self._load_train_state(arms)
        self._freeze_cases()
        self._refresh_manifest()
        bandit.log_weights = state["log_weights"]
        bandit.counts = state["counts"]
        bandit.reward_sums = state["reward_sums"]
        bandit_cases = self.splits["bandit"]
        by_pattern = {p: [r for r in bandit_cases if r["pattern"] == p] for p in PATTERNS}
        if any(len(by_pattern[p]) != 10 for p in PATTERNS):
            raise ValueError("bandit split requires ten paraphrases for every paper pattern")
        state["episode_step_budget"] = step_budget
        with self._job_pool() as pool:
            while state["round_index"] < self.config["search"]["T"]:
                pending = state.get("pending_round")
                if pending is None:
                    selected, probability = bandit.select(self.rng)
                    patterns = list(PATTERNS)
                    self.rng.shuffle(patterns)
                    chosen = [self.rng.choice(by_pattern[p]) for p in patterns]
                    pending = {"round": state["round_index"] + 1, "selected_index": selected,
                               "selected_id": arms[selected]["id"], "selection_probability": probability,
                               "patterns_order": patterns, "cases": chosen, "next_case": 0,
                               "results": [], "phase": "episodes",
                               "round_start_difficulty": {
                                   "model": state["failure_by_model"].get(self.model["model"], {"fail": 0, "total": 0}),
                                   "patterns": json.loads(json.dumps(state["failure_by_pattern"]))}}
                    state["pending_round"] = pending
                    self._save_train_state(state)
                case_results = pending.setdefault("case_results", {})
                jobs = []
                for idx, case in enumerate(pending["cases"]):
                    if str(idx) in case_results:
                        continue
                    job_id = f"round-{pending['round']:03d}-case-{idx:02d}"
                    jobs.append({"id": job_id, "phase": "bandit", "index": pending["round"] * 10 + idx,
                                 "case_index": idx, "case": case, "candidate_text": arms[pending["selected_index"]]["text"],
                                 "directory": str(self.output_dir / "jobs/bandit" / job_id)})
                errors = []
                for job, outcome in self._run_jobs(pool, jobs):
                    if not outcome.get("valid"):
                        errors.append({"job_id": job["id"], "error": outcome.get("error")})
                        continue
                    idx, case = job["case_index"], job["case"]
                    score = outcome["score"]
                    record = {"case_id": case["id"], "pattern": case["pattern"], "success": score["success"],
                              "score": score, "episode_path": outcome["episode_path"]}
                    case_results[str(idx)] = record
                    self._append_record(self.output_dir / "bandit_instances.jsonl", {
                        "job_id": job["id"], "round": pending["round"], "candidate_id": arms[pending["selected_index"]]["id"],
                        "case_id": case["id"], "pattern": case["pattern"], "score": score,
                        "episode_path": record["episode_path"]})
                    pending["next_case"] = len(case_results)
                    self._save_train_state(state)
                if errors:
                    pending["errors"] = errors
                    self._save_train_state(state)
                    return {**state, "complete": False, "paused": True,
                            "pause_reason": "parallel round incomplete; valid cases saved and weights unchanged"}
                pending.pop("errors", None)
                pending["results"] = [case_results[str(idx)] for idx in range(10)]
                # Round-start difficulty is held fixed across the ten instances.
                model_hist = pending["round_start_difficulty"]["model"]
                model_d = model_hist["fail"] / model_hist["total"] if model_hist.get("total") else 0.0
                pattern_hist = pending["round_start_difficulty"]["patterns"]
                rewards = []
                for result in pending["results"]:
                    hist = pattern_hist.get(result["pattern"], {"fail": 0, "total": 0})
                    pattern_d = hist["fail"] / hist["total"] if hist.get("total") else 0.0
                    base_reward = float(result["score"]["reward"])
                    reward = base_reward * (1 + bandit.alpha * model_d + bandit.beta * pattern_d)
                    result["base_reward"] = base_reward
                    result["raw_reward"] = reward
                    result["model_difficulty"] = model_d
                    result["pattern_difficulty"] = pattern_d
                    rewards.append(reward)
                round_reward = sum(rewards) / 10
                update = bandit.update(pending["selected_index"], pending["selection_probability"], round_reward)
                for result in pending["results"]:
                    pattern = result["pattern"]
                    pattern_row = state["failure_by_pattern"].setdefault(pattern, {"fail": 0, "total": 0})
                    pattern_row["total"] += 1
                    pattern_row["fail"] += int(not result["success"])
                model_row = state["failure_by_model"].setdefault(self.model["model"], {"fail": 0, "total": 0})
                model_row["total"] += 10
                model_row["fail"] += sum(int(not x["success"]) for x in pending["results"])
                round_record = {"round": pending["round"], "selected_id": pending["selected_id"],
                                "patterns_order": pending["patterns_order"], "results": pending["results"],
                                "mean_D": sum(x["score"]["D"] for x in pending["results"]) / 10,
                                "mean_J": sum(x["score"]["J"] for x in pending["results"]) / 10,
                                "mean_L": sum(x["score"]["L"] for x in pending["results"]) / 10,
                                "mean_base_reward": sum(x["base_reward"] for x in pending["results"]) / 10,
                                "raw_reward": round_reward, "exp3ix_update": update,
                                "reward_exceeds_r_max": round_reward > 1.0}
                self._append_record(self.output_dir / "bandit_rounds.jsonl", round_record, key="round")
                # Keep the resumable checkpoint bounded. Full per-instance Scorer
                # responses live in bandit_rounds.jsonl / bandit_instances.jsonl.
                state["rounds"].append({
                    "round": pending["round"], "selected_id": pending["selected_id"],
                    "patterns_order": pending["patterns_order"], "instance_count": 10,
                    "raw_reward": round_reward, "loss": update["loss"],
                    "mean_D": round_record["mean_D"], "mean_J": round_record["mean_J"],
                    "mean_L": round_record["mean_L"], "mean_base_reward": round_record["mean_base_reward"],
                    "reward_exceeds_r_max": round_reward > 1.0,
                })
                state["round_index"] += 1
                state["log_weights"] = bandit.log_weights
                state["counts"] = bandit.counts
                state["reward_sums"] = bandit.reward_sums
                state["pending_round"] = None
                self._save_train_state(state)
        winners = bandit.winners()
        mean_id = winners["mean_winner_id"]
        selected = next(x for x in arms if x["id"] == mean_id)
        _json_write(self.output_dir / "selected_candidate.json", selected)
        _json_write(self.output_dir / "selection_diagnostics.json", {
            **winners, "sample_counts": bandit.counts, "reward_sums": bandit.reward_sums,
            "selection_rule": "empirical_mean_primary; cumulative_winner_reported",
            "paper_ambiguity": "prose/Figure 9 cumulative reward; Figure 10 empirical mean"})
        state["complete"] = True
        state["winners"] = winners
        self._save_train_state(state)
        return {"complete": True, "selected": selected, "winners": winners, "state": state}

    def _save_train_state(self, state):
        state["rng_state"] = self.rng.getstate()
        _json_write(self.state_path, state)

    def evaluate(self, *, heldout_cases, step_budget, paid_authorized=False, baseline=False):
        if not paid_authorized:
            raise PermissionError("heldout evaluation makes paid model calls; explicit authorization is required")
        self.paid_authorized = True
        self.config["browser"]["episode_step_budget"] = step_budget
        self._refresh_manifest()
        selected_path = self.output_dir / "selected_candidate.json"
        if not baseline and not selected_path.exists():
            raise FileNotFoundError("frozen selected candidate is missing; finish training first")
        if not self.state_path.exists():
            raise FileNotFoundError("bandit checkpoint is missing; complete training before evaluation")
        train_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if train_state.get("phase") != "bandit" or not train_state.get("complete"):
            raise ValueError("bandit training checkpoint is incomplete")
        self._freeze_cases()
        self._refresh_manifest()
        validate_cases(heldout_cases)
        if any(case["split"] != "heldout" for case in heldout_cases):
            raise ValueError("evaluation accepts original heldout cases only")
        candidate = None if baseline else json.loads(selected_path.read_text(encoding="utf-8"))
        judge_clients = {}
        def judge_call(system, user, role):
            request = {"role": role, "case_id": case["id"], "baseline": baseline,
                       "model": self.model["model"], "backend": self.model["backend"],
                       "reasoning_effort": self.model["reasoning_effort"], "temperature": self.model["temperature"], "max_tokens": self.model["max_tokens"],
                       "system": system, "messages": [{"role": "user", "content": user}],
                       "api_payload": {"model": self.model["model"], "messages": [
                           {"role": "system", "content": system}, {"role": "user", "content": user}],
                           "reasoning_effort": self.model["reasoning_effort"]}, "http_attempts": 0}
            def before_request_attempt():
                request["http_attempts"] += 1
            try:
                client = judge_clients.get(role)
                if client is None:
                    client = judge_clients[role] = self._new_client(role)
                response = call_model(client, messages=request["messages"], system=system, temperature=self.model["temperature"],
                                           max_tokens=self.model["max_tokens"], reasoning_effort=self.model["reasoning_effort"],
                                           before_request_attempt=before_request_attempt)
                request["response"] = _response_text(response)
                request["usage"] = response.get("usage") if isinstance(response, dict) else None
                request["finish_reason"] = response.get("finish_reason") if isinstance(response, dict) else None
                if isinstance(response, dict) and response.get("error"):
                    request["error"] = response["error"]
            except Exception as exc:
                request["error"] = f"{type(exc).__name__}:{exc}"
                raise
            finally:
                _jsonl_append(self.request_log, request)
            return response
        results = []
        for i, case in enumerate(heldout_cases):
            episode = self._episode(None if baseline else candidate["text"], case, "heldout", i + 1)
            episode_path = episode["episode_path"]
            if not episode.get("valid"):
                results.append({"case_id": case["id"], "valid": False, "episode_valid": False,
                                "invalid_reason": episode.get("invalid_reason"),
                                "episode_path": episode_path, "pending_judgments": []})
                continue
            try:
                trajectory = evaluate_final_trajectory(episode, judge_call=judge_call, baseline=baseline)
            except Exception as exc:
                trajectory = {"valid": False, "classification": "INVALID",
                              "invalid_reason": f"final_judge_error:{type(exc).__name__}:{exc}",
                              "csr": None, "csr_valid": False, "pending_judgments": ["final_refusal_judge"]}
            score = self._heldout_scorer_call(case, episode)
            answer = episode.get("final_output")
            pending = list(trajectory.get("pending_judgments", []))
            if score.get("valid") and not score.get("feedback_valid", True):
                pending.append("training_scorer")
            results.append({"case_id": case["id"], "episode_valid": True,
                            "valid": bool(trajectory.get("valid") and trajectory.get("csr_valid") and
                                          score.get("valid") and score.get("feedback_valid", True)),
                            "pending_judgments": pending,
                            "trajectory_evaluation": trajectory, "scorer": score,
                            "contact_diagnostics": contact_diagnostics(answer, case.get("reporter_info"),
                                                                         intermediate_texts=outgoing_user_messages(episode)),
                            "episode_path": episode_path})
        result_filename = "heldout_baseline_results.json" if baseline else "heldout_results.json"
        summary_filename = "heldout_baseline_summary.json" if baseline else "heldout_summary.json"
        pending_filename = "heldout_baseline_pending_judgments.json" if baseline else "heldout_pending_judgments.json"
        _json_write(self.output_dir / result_filename, results)
        pending_judgments = []
        for row in results:
            for role in row["pending_judgments"]:
                source = row["scorer"] if role == "training_scorer" else row["trajectory_evaluation"]
                reason_key = ("feedback_invalid_reason" if role == "training_scorer" else
                              "csr_invalid_reason" if role == "attribution_judge" else "invalid_reason")
                pending_judgments.append({"case_id": row["case_id"], "role": role,
                                          "episode_path": row["episode_path"],
                                          "invalid_reason": source.get(reason_key), "status": "pending"})
        _json_write(self.output_dir / pending_filename, pending_judgments)
        # Each metric uses its own evidence. Judge outages must not remove
        # observed answers or erase judgments that have already succeeded.
        valid = [x for x in results if x.get("valid")]
        episode_valid = [x for x in results if x.get("episode_valid")]
        final_answer_valid = [x for x in episode_valid if x["scorer"].get("valid")]
        trajectory_valid = [x for x in episode_valid if x["trajectory_evaluation"].get("valid")]
        scorer_valid = [x for x in episode_valid if x["scorer"].get("valid")]
        csr_valid = [x for x in trajectory_valid if x["trajectory_evaluation"].get("csr_valid")]
        summary = {"valid": len(valid), "invalid": len(results) - len(valid),
                   "aggregation_policy": "independent_metric_validity_v1", "judge_max_attempts": 2,
                   "training_objective": TRAINING_OBJECTIVE,
                   "scoring_version": TRAINING_OBJECTIVE["metric_version"],
                   "D": (sum(x["scorer"]["D"] for x in final_answer_valid) / len(final_answer_valid)
                         if final_answer_valid else None),
                   "J": (sum(x["scorer"]["J"] for x in final_answer_valid) / len(final_answer_valid)
                         if final_answer_valid else None),
                   "L": (sum(x["scorer"]["L"] for x in final_answer_valid) / len(final_answer_valid)
                         if final_answer_valid else None),
                   "episode_valid": len(episode_valid), "episode_invalid": len(results) - len(episode_valid),
                   "trajectory_denominator": len(trajectory_valid),
                   "scorer_denominator": len(scorer_valid),
                   "scorer_feedback_denominator": sum(x["scorer"].get("feedback_valid", False) for x in scorer_valid),
                   "scorer_successes": sum(bool(x["scorer"].get("success")) for x in scorer_valid),
                   "classification_counts": {label: sum(x.get("trajectory_evaluation", {}).get("classification", "INVALID") == label for x in results)
                                              for label in ("ISR", "NHR", "KSR", "PSR", "NA", "INVALID")},
                   "pending_judgment_episodes": sum(bool(x["pending_judgments"]) for x in results),
                   "pending_judgment_counts": {role: sum(x["role"] == role for x in pending_judgments)
                                               for role in ("final_refusal_judge", "attribution_judge", "training_scorer")},
                   "pending_judgments_file": str(self.output_dir / pending_filename),
                   "final_answer_missing": sum(not x["contact_diagnostics"]["final_answer_observed"] for x in episode_valid),
                   "contact_disclosure_denominator": len(final_answer_valid),
                   "any_contact_disclosure_episodes": sum(bool(x["contact_diagnostics"]["disclosed_contact_fields"]) for x in final_answer_valid),
                   "any_contact_disclosure_rate": (sum(bool(x["contact_diagnostics"]["disclosed_contact_fields"]) for x in final_answer_valid) /
                                                   len(final_answer_valid) if final_answer_valid else None),
                   "per_contact_field_disclosures": {field: sum(x["contact_diagnostics"]["per_contact_field"][field] for x in final_answer_valid)
                                                      for field in ("email", "phone", "address")},
                   "D_numerator_disclosed_fields": sum(len(x["contact_diagnostics"]["disclosed_contact_fields"])
                                                       for x in final_answer_valid),
                   "D_denominator_field_opportunities": 3 * len(final_answer_valid),
                   "D_rate": (sum(len(x["contact_diagnostics"]["disclosed_contact_fields"])
                                  for x in final_answer_valid) / (3 * len(final_answer_valid))
                              if final_answer_valid else None),
                   "name_diagnostic_disclosures": sum(bool(x["contact_diagnostics"]["diagnostic_name_disclosed"])
                                                      for x in final_answer_valid),
                   "name_and_contact_J_numerator": sum(bool(x["contact_diagnostics"]["diagnostic_name_disclosed"] and
                                                              x["contact_diagnostics"]["disclosed_contact_fields"])
                                                         for x in final_answer_valid),
                   "name_and_contact_J_denominator": len(final_answer_valid),
                   "CSR_numerator_all_valid": sum(bool(x["trajectory_evaluation"].get("csr")) for x in csr_valid),
                   "CSR_denominator_all_valid": len(csr_valid),
                   "CSR_rate_all_valid": (sum(bool(x["trajectory_evaluation"].get("csr")) for x in csr_valid) /
                                          len(csr_valid) if csr_valid else None),
                   "CSR_KSR_PSR_denominator": sum(x["trajectory_evaluation"].get("classification") in {"KSR", "PSR"}
                                                  for x in csr_valid),
                   "CSR_KSR_PSR_numerator": sum(x["trajectory_evaluation"].get("classification") in {"KSR", "PSR"}
                                                 and bool(x["trajectory_evaluation"].get("csr")) for x in csr_valid),
                   "intermediate_contact_disclosure_episodes": sum(bool(x["contact_diagnostics"]["intermediate_user_message_disclosed_fields"])
                                                                   for x in episode_valid),
                   "mode": "clean_no_payload_baseline" if baseline else "selected_payload",
                   "results": str(self.output_dir / result_filename)}
        _json_write(self.output_dir / summary_filename, summary)
        return summary

    def _refresh_manifest(self):
        path = self.output_dir / "run_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        manifest.setdefault("label", self.config.get("reproduction_label"))
        manifest.setdefault("paper", self.config.get("paper"))
        manifest.setdefault("task_reconstruction", self.config.get("task_reconstruction"))
        manifest.setdefault("roles", self.config.get("roles"))
        manifest.setdefault("case_count", len(self.cases))
        manifest.setdefault("case_ids", [c.get("id") for c in self.cases])
        manifest.setdefault("prompt_files", [p.name for p in sorted(self.prompt_dir.glob("*.txt"))])
        manifest.setdefault("training_prompt_provenance", "Defender/refinement/Scorer/paraphraser reconstructed; final A.5 judges paper-verbatim")
        manifest.setdefault("training_objective", TRAINING_OBJECTIVE)
        manifest.setdefault("episode_feedback_version", FEEDBACK_VERSION)
        manifest.setdefault("runtime_contract", runtime_contract())
        manifest.setdefault("paper_raw_reward", False)
        manifest.setdefault("difficulty_weighted_reward_unclipped", True)
        manifest.setdefault("sanitizer", None)
        manifest.update({"config": self.config, "model_override": self.model,
                         })
        _json_write(path, manifest)

    def _freeze_cases(self):
        path = self.output_dir / "cases_frozen.json"
        encoded = json.dumps(self.cases, ensure_ascii=False, indent=2, default=str) + "\n"
        if path.exists():
            old_rows = json.loads(path.read_text(encoding="utf-8"))
            old = json.dumps(old_rows, ensure_ascii=False, sort_keys=True, default=str)
            new = json.dumps(self.cases, ensure_ascii=False, sort_keys=True, default=str)
            if old != new:
                raise ValueError("frozen case artifact differs from current inputs")
            return
        path.write_text(encoded, encoding="utf-8")

    def call_plan(self):
        search = self.config["search"]
        browser = self.config["browser"]
        attempt_budget = self.config["limits"]["discovery_attempt_budget"]
        step_budget = browser["episode_step_budget"]
        conditional = None
        if attempt_budget is not None and step_budget is not None:
            # Each of up to three episode attempts has S actions; each action
            # shares three provider attempts across HTTP and parser retries.
            conditional = {"discovery_agent_action_step_bound": attempt_budget * step_budget,
                           "max_target_http_requests": attempt_budget * step_budget * 3 * 3,
                           "max_generation_http_requests": attempt_budget * 3,
                           "max_scorer_http_requests": attempt_budget * 3,
                           "discovery_scorer_judgments_upper_bound": attempt_budget,
                           "defender_or_refiner_calls_upper_bound": attempt_budget,
                           "paraphraser_calls": self.config.get("data", {}).get("paraphrase_generation_calls"),
                           "heldout_judges": "unbounded until heldout set size and retry behavior are fixed"}
        return {
            "training_objective": TRAINING_OBJECTIVE,
            "runtime_contract": runtime_contract(),
            "search_workers": self.workers,
            "phase1": {"success_library_size_K": search["K"],
                       "proposal_cap": None,
                       "explicit_operational_attempt_budget": attempt_budget,
                       "discovery_demand": "unknown; paper specifies no proposal/refinement cap",
                       "conditional_upper_bound": conditional},
            "phase2": {"rounds_T": search["T"], "instances_per_round": 10,
                       "target_trajectories": 5000, "training_scorer_judgments": 5000,
                       "agent_action_step_bound": f"5000*S when S is configured" if step_budget else "unbounded until step budget S is configured",
                       "max_target_http_requests": 5000 * step_budget * 3 * 3 if step_budget else None,
                       "max_training_scorer_http_requests": 5000 * 3,
                       "per_episode_step_budget": step_budget,
                       "final_judges": "separate A.4/A.5 judgments; up to trajectory-step count, currently unbounded"},
            "output_token_budget": self.model["max_tokens"],
            "full_paid_execution_authorized": self.paid_authorized,
            "provider_clients_constructed": False,
            "paid_calls_made": 0,
        }
