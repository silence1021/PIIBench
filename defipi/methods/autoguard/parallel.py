"""Independent, durable search jobs. Only the coordinator changes search state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time


def execute_job(config, job, client_factory, episode_runner=None, scorer_factory=None):
    from defipi.methods.autoguard.runtime import FileSlots
    # A restarted coordinator must wait for a surviving old worker, not replay
    # the same job while its provider request is still running.
    with FileSlots(Path(job["directory"]) / ".runtime/job_slot", 1):
        return _execute_job(config, job, client_factory, episode_runner, scorer_factory)


def _execute_job(config, job, client_factory, episode_runner, scorer_factory):
    # Import inside the entry point so multiprocessing spawn has no import cycle.
    from defipi.methods.autoguard.search import AutoGuardSearch, _json_write
    from defipi.jsonio import content_key
    from defipi.methods.autoguard.scoring import score_episode

    directory = Path(job["directory"])
    path = directory / "job_state.json"
    state = json.loads(path.read_text()) if path.exists() else {
        "job_id": job["id"], "finished": False, "candidate_text": job.get("candidate_text"),
        "episode_path": None, "score": None, "started_at": time.time(),
    }
    if state["finished"]:
        return str(path)
    state["worker_pid"] = os.getpid()
    _json_write(path, state)
    worker_config = {**config, "output_dir": str(directory)}
    worker = AutoGuardSearch(worker_config, cases=[job["case"]],
                            model_client_factory=client_factory, episode_runner=episode_runner,
                            scorer_factory=scorer_factory)
    stage = "generation" if job.get("generation") else "episode"
    try:
        if job.get("generation") and not state.get("generation_complete"):
            # A lost worker cannot silently repeat a possibly charged generation.
            if state.get("generation_started"):
                records = directory / "role_requests_responses.jsonl"
                rows = [json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
                if not rows or "response" not in rows[-1]:
                    raise RuntimeError("generation interrupted; reserved proposal remains consumed")
                text = rows[-1]["response"]
            else:
                state["generation_started"] = True
                _json_write(path, state)
                generation = job["generation"]
                text = worker._generate(generation["role"], generation["system"], generation["user"])
            state.update(candidate_text=text, candidate_id=content_key(text)[:16], generation_complete=True)
            _json_write(path, state)
        stage = "episode"
        if state["episode_path"] is None:
            episode = worker._episode(state["candidate_text"], job["case"], job["phase"], job["index"])
            state["episode_path"] = episode["episode_path"]
            _json_write(path, state)
        else:
            episode = json.loads(Path(state["episode_path"]).read_text())
        if not score_episode(job["case"], episode)["valid"]:
            raise RuntimeError(episode.get("invalid_reason") or "invalid PII episode")
        stage = "scorer"
        if state["score"] is None:
            if state.get("scorer_started"):
                # The PII outcome is already known. Do not repeat an uncertain
                # diagnostic request after a process interruption.
                state["score"] = {**score_episode(job["case"], episode),
                                  "feedback_valid": False, "feedback_invalid_reason": "scorer_interrupted",
                                  "explicit_refusal": None}
            else:
                state["scorer_started"] = True
                _json_write(path, state)
                state["score"] = worker._scorer_call(job["case"], episode)
        if not state["score"].get("valid"):
            raise RuntimeError(state["score"].get("invalid_reason") or "invalid PII score")
        state.update(valid=True, error=None, error_stage=None)
    except Exception as exc:
        state.update(valid=False, error=f"{type(exc).__name__}:{exc}", error_stage=stage)
    state.update(finished=True, finished_at=time.time())
    _json_write(path, state)
    return str(path)
