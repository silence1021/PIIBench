"""Bounded paid execution; preparation, status and receipt parsing are offline."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
from pathlib import Path
import threading
import time

from ..concurrency import RequestLimiter
from .file_task_evidence import fingerprint
from .presence_statistics import judge_vote_summary

MAX_SENDS = 3
DEFAULT_CONCURRENCY = {"openai": 20, "deepseek": 20, "openrouter": 20, "anthropic": 20}
REQUEST_SCHEMA = "presence-request-v1"
CONFIG_STOP_CODES = frozenset((400, 401, 402, 403, 404, 405, 422))
STOP_SCHEMA = "presence-provider-stops-v1"


def validate_job(job):
    from .file_task_judges import VERSION
    if job.get("protocol") != VERSION:
        raise ValueError("Judge job uses a different protocol; prepare a new directory with the current prompt")
    if job.get("job_id") != fingerprint({k: v for k, v in job.items() if k != "job_id"}):
        raise ValueError("Judge job was modified after preparation")


def receipt_path(directory, job):
    root = Path(directory) / "votes"
    if job.get("job_schema") == REQUEST_SCHEMA:
        root /= job["job_id"][:2]
    return root / (job["job_id"] + ".json")


def read_receipt(directory, job):
    """Reparse retained responses offline; malformed output abstains, never becomes ABSENT."""
    from .file_task_judges import parse_vote
    p = receipt_path(directory, job)
    if not p.exists():
        return {"status": "missing", "attempts": []}
    vote = json.loads(p.read_text())
    if (vote.get("protocol") != job["protocol"] or vote.get("job_id") != job["job_id"] or
            vote.get("model") != job["binding"]["model"] or
            ("subject_id" in job and vote.get("subject_id") != job["subject_id"])):
        raise ValueError("Judge receipt identity/protocol mismatch")
    # Older completed single-response receipts had no numbered history. They can
    # still be read as cached results, but never enter a recovery/send path.
    legacy_completed = ("attempts" not in vote and "number" not in vote
                        and vote.get("status") in ("ok", "unparsed"))
    if "attempts" not in vote:
        if not legacy_completed and (type(vote.get("number")) is not int or vote["number"] != 1):
            raise ValueError("Legacy judge receipt lacks complete numbered attempt history")
        vote["attempts"] = [{k: v for k, v in vote.items() if k != "attempts"}]
    attempts = vote["attempts"]
    if (not isinstance(attempts, list) or not 1 <= len(attempts) <= MAX_SENDS
            or not legacy_completed and any(not isinstance(a, dict) or type(a.get("number")) is not int or a["number"] != n
                                            for n, a in enumerate(attempts, 1))):
        raise ValueError("Judge receipt requires complete consecutive attempt history within the three-send limit")
    # A retained top-level response can belong to an older attempt. Only the
    # latest attempt describes the current result; keep identity from the envelope.
    vote = {**attempts[-1], **{k: vote[k] for k in ("job_id", "protocol", "model", "subject_id", "attempts")
                             if k in vote}}
    if vote.get("response") is not None and vote["status"] in ("ok", "unparsed"):
        try:
            vote["value"] = parse_vote(job["role"], vote["response"]["text"])
            vote["status"] = "ok"
        except (ValueError, KeyError, TypeError):
            vote.update(status="unparsed", value=None)
    return vote


def _error(exc, now):
    """Only persist error types/status/Retry-After, not keys or request/response bodies."""
    chain, current, seen = [], exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    status, retry_after = None, None
    for item in chain:
        status = getattr(item, "status_code", None) or getattr(getattr(item, "response", None), "status_code", None) or status
        headers = getattr(item, "headers", None) or getattr(getattr(item, "response", None), "headers", None) or {}
        for key, value in headers.items():
            if str(key).lower() != "retry-after":
                continue
            try:
                delay = float(value)
                if delay >= 0 and delay < float("inf"):
                    retry_after = now + delay
            except (ValueError, TypeError):
                try:
                    date = parsedate_to_datetime(str(value))
                    retry_after = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
                except (ValueError, TypeError, OverflowError):
                    pass
    transient = status == 429 or isinstance(status, int) and 500 <= status < 600
    transient = transient or any(isinstance(x, (TimeoutError, ConnectionError)) or
                                "timeout" in type(x).__name__.lower() or "connection" in type(x).__name__.lower()
                                for x in chain)
    if status in CONFIG_STOP_CODES:
        transient = False
    return {"error_type": type(chain[-1]).__name__, "http_status": status,
            "retryable": bool(transient), "retry_after_at": retry_after}


def _stop_state(directory):
    path = Path(directory) / "provider_stops.json"
    if not path.exists():
        return {"schema": STOP_SCHEMA, "active": {}, "history": [], "acknowledged": {}}
    data = json.loads(path.read_text())
    if (data.get("schema") != STOP_SCHEMA or not isinstance(data.get("active"), dict)
            or not isinstance(data.get("history"), list) or not isinstance(data.get("acknowledged"), dict)):
        raise ValueError("Invalid provider stop state")
    return data


def _is_config_error(vote):
    return (vote["status"] == "error" and (vote.get("http_status") in CONFIG_STOP_CODES
            or vote.get("http_status") is None and vote.get("retryable") is False))


def _config_failure(job, vote):
    if not _is_config_error(vote):
        return None
    return {"provider": job["binding"]["provider"], "job_id": job["job_id"],
            "attempts": len(vote["attempts"]),
            "reason": "client_error" if vote.get("http_status") is not None else "local_request_error",
            "http_status": vote.get("http_status"), "error_type": vote.get("error_type"),
            "stopped_at": vote.get("completed_at") or vote.get("started_at"),
            "requires_explicit_recovery": True}


def _inferred_stops(state, failures):
    active = dict(state["active"])
    for failure in failures:
        if failure["attempts"] > state["acknowledged"].get(failure["job_id"], 0):
            active.setdefault(failure["provider"], failure)
    return active


def _backup_config_receipt(directory, job, attempt_number):
    """Retain exact old bytes before appending a resumed configuration attempt."""
    original = receipt_path(directory, job).read_bytes()
    target = Path(directory) / "history" / "config-recovery" / job["job_id"] / f"before-attempt-{attempt_number}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as stream:
            stream.write(original)
    except FileExistsError:
        if target.read_bytes() != original:
            raise ValueError("Configuration recovery backup differs from the current receipt")


def _response_usage(response):
    """Read actual normalized/raw usage offline, without modifying a receipt."""
    from .presence_usage import _normalize_usage, _usage_number
    response = response if isinstance(response, dict) else {}
    raw = response.get("raw_response") or {}
    raw_usage = raw.get("usage") if isinstance(raw, dict) else None
    merged = {}
    for data in (raw_usage, response.get("usage")):
        if not isinstance(data, dict):
            continue
        def token(*names):
            return next((valid for name in names
                         if (valid := _usage_number(data.get(name), integer=True)) is not None), None)
        usage = _normalize_usage(prompt_tokens=token("prompt_tokens", "input_tokens"),
                                 completion_tokens=token("completion_tokens", "output_tokens"),
                                 total_tokens=token("total_tokens"), usage_details=data)
        # In a receipt fallback, a newly synthesized total must not replace an
        # explicitly retained total from another layer of the same response.
        if token("total_tokens") is None:
            usage.pop("total_tokens", None)
        for key, value in usage.items():
            if value is not None:
                merged[key] = {**merged.get(key, {}), **value} if isinstance(value, dict) else value
    return merged or None


def _run_jobs_unlocked(directory, max_requests, client_factory=None, *, concurrency=None,
             reclaim_started=False, sleep=time.sleep, clock=time.time,
             progress=None, progress_interval=5.0, retry_config_errors=False):
    """At most three sends per saved request, including across invocations.

    One OS lock owns the directory. Worker pools are bounded per provider. A crashed
    started attempt consumes a send and requires explicit reclaim after its timeout.
    """
    from .file_task_judges import jsonl, parse_vote, write_json
    if type(retry_config_errors) is not bool:
        raise ValueError("retry_config_errors must be boolean")
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError("A positive explicit request budget is required")
    if (isinstance(progress_interval, bool) or not isinstance(progress_interval, (int, float))
            or not math.isfinite(progress_interval) or progress_interval <= 0):
        raise ValueError("Progress interval must be positive and finite")
    directory = Path(directory)
    jobs = list(jsonl(directory / "jobs.jsonl"))
    seen = set()
    for job in jobs:
        validate_job(job)
        if job["job_id"] in seen:
            raise ValueError("Duplicate judge job")
        seen.add(job["job_id"])
    limits = {**DEFAULT_CONCURRENCY, **(concurrency or {})}
    if any(type(v) is not int or v < 1 for v in limits.values()):
        raise ValueError("Provider concurrency must be a positive integer")
    if client_factory is None:
        from .presence_transport import build_judge_client
        client_factory = build_judge_client
    count, reused, retries, active, waiting = 0, 0, 0, 0, 0
    mutex, stopped = threading.Lock(), threading.Event()
    local_clients = threading.local()
    windows = defaultdict(lambda: deque(maxlen=200))
    stop_state = _stop_state(directory)
    failures, job_states = [], {}
    # Retain only compact states/config-failure metadata, never all response bodies.
    for job in jobs:
        prior = read_receipt(directory, job)
        job_states[job["job_id"]] = prior["status"]
        failure = _config_failure(job, prior)
        if failure:
            failures.append(failure)
    inferred = _inferred_stops(stop_state, failures)
    changed = inferred != stop_state["active"]
    stop_state["active"] = inferred
    if retry_config_errors:
        for failure in failures:
            if failure["attempts"] > stop_state["acknowledged"].get(failure["job_id"], 0):
                stop_state["acknowledged"][failure["job_id"]] = failure["attempts"]
                changed = True
        for provider, record in list(stop_state["active"].items()):
            stop_state["history"].append({"event": "recovered", "provider": provider,
                "recovered_at": datetime.fromtimestamp(clock(), timezone.utc).isoformat(), "previous_stop": record})
            changed = True
        stop_state["active"] = {}
    if changed:
        write_json(directory / "provider_stops.json", stop_state)
    halt_reasons = dict(stop_state["active"])
    halted = set(halt_reasons)
    state_counts = Counter(job_states.values())
    cached = sum(state == "ok" for state in job_states.values())
    started_clock = time.monotonic()
    pools = defaultdict(list)
    for job in jobs:
        if job.get("ready", True):
            pools[job["binding"]["provider"]].append(job)
    provider_cancel = {provider: threading.Event() for provider in pools}
    for provider in halted:
        if provider in provider_cancel:
            provider_cancel[provider].set()

    def emit(phase):
        if progress is None:
            return
        with mutex:
            snapshot = {"phase": phase, "total_jobs": len(jobs),
                        "states": {k: n for k, n in state_counts.items() if n},
                        "new_requests": count, "automatic_retries": retries,
                        "in_flight": active, "waiting_retries": waiting,
                        "cached_jobs": cached, "existing_receipts": reused,
                        "elapsed_seconds": max(0, time.monotonic() - started_clock),
                        "halted_providers": sorted(halted), "provider_stops": dict(halt_reasons),
                        "requires_config_recovery": bool(stop_state["active"])}
        progress(snapshot)

    def set_state(job, state):
        key = job["job_id"]
        state_counts[job_states[key]] -= 1
        state_counts[state] += 1
        job_states[key] = state

    def stamp():
        return datetime.fromtimestamp(clock(), timezone.utc).isoformat()

    def stop_provider(provider, reason, error, job):
        # Caller holds the scheduling mutex: block all later reservations before I/O.
        halted.add(provider)
        provider_cancel[provider].set()
        if provider in stop_state["active"]:
            return
        record = {"reason": reason, "error_type": error["error_type"],
                  "http_status": error.get("http_status"), "stopped_at": stamp(),
                  "job_id": job["job_id"], "requires_explicit_recovery": True}
        stop_state["active"][provider] = record
        halt_reasons[provider] = record
        stop_state["history"].append({"event": "stopped", "provider": provider, **record})
        write_json(directory / "provider_stops.json", stop_state)

    def work(job, limiter):
        nonlocal count, reused, retries, active, waiting
        vote = read_receipt(directory, job)
        history = vote["attempts"]
        if vote["status"] in ("ok", "unparsed") or len(history) >= MAX_SENDS:
            with mutex:
                reused += 1
            return
        if vote["status"] == "started":
            started = datetime.fromisoformat(vote["started_at"]).timestamp()
            if not reclaim_started or clock() - started < 1200:
                with mutex:
                    reused += 1
                return
        elif (vote["status"] == "error" and not vote.get("retryable")
              and not (retry_config_errors and _is_config_error(vote))):
            with mutex:
                reused += 1
            return
        binding = job["binding"]
        # Build only when a send can be scheduled; clients are reused within each worker.
        client = None
        recovering_config = retry_config_errors and _config_failure(job, vote) is not None
        while len(history) < MAX_SENDS and not stopped.is_set():
            with mutex:
                if binding["provider"] in halted:
                    return
            if history:
                wait_until = vote.get("next_retry_at") or 0
                delay = max(0, wait_until - clock())
                with mutex:
                    waiting += 1
                try:
                    if sleep is time.sleep:
                        provider_cancel[binding["provider"]].wait(delay)  # Provider/global stops release idle retries.
                    else:
                        sleep(delay)
                finally:
                    with mutex:
                        waiting -= 1
            with limiter:
                with mutex:
                    if count >= max_requests or stopped.is_set() or binding["provider"] in halted:
                        return
                    # Client construction/credential failure sends nothing and must not consume a ticket.
                    if client is None:
                        if not hasattr(local_clients, "clients"):
                            local_clients.clients = {}
                        binding_id = fingerprint(binding)
                        if binding_id not in local_clients.clients:
                            try:
                                local_clients.clients[binding_id] = client_factory(binding)
                            except Exception as exc:
                                stop_provider(binding["provider"], "client_initialization_error", _error(exc, clock()), job)
                                return
                        client = local_clients.clients[binding_id]
                    if recovering_config:
                        _backup_config_receipt(directory, job, len(history) + 1)
                        recovering_config = False
                    count += 1
                    retries += bool(history)
                    active += 1
                    set_state(job, "started")
                attempt = {"number": len(history) + 1, "status": "started", "started_at": stamp()}
                history.append(attempt)
                vote = {"job_id": job["job_id"], "protocol": job["protocol"], "model": binding["model"],
                        "attempts": history, **attempt}
                if "subject_id" in job:
                    vote["subject_id"] = job["subject_id"]
                path = receipt_path(directory, job)
                write_json(path, vote)
                start = clock()
                try:
                    parameters = binding["request_parameters"]
                    kwargs = {k: v for k, v in parameters.items()
                              if k in ("reasoning_effort", "thinking", "output_config")}
                    if binding["provider"] == "openrouter":
                        kwargs["provider"] = parameters.get("provider", {})
                    response = client.generate(messages=job["messages"], system=job["system"],
                        temperature=None if "temperature" in binding.get("omit_parameters", []) else parameters.get("temperature", 0),
                        max_tokens=None, **kwargs)
                    attempt["response"] = response
                    try:
                        attempt.update(value=parse_vote(job["role"], response["text"]), status="ok")
                    except (ValueError, KeyError, TypeError):
                        attempt.update(status="unparsed", error_type="LabelParseError")
                except Exception as exc:
                    attempt.update(status="error", **_error(exc, clock()))
                    if _is_config_error(attempt):
                        reason = "client_error" if attempt.get("http_status") is not None else "local_request_error"
                        with mutex:
                            stop_provider(binding["provider"], reason, attempt, job)
                    if attempt["retryable"]:
                        attempt["next_retry_at"] = (attempt["retry_after_at"] if attempt["retry_after_at"] is not None
                                                    else clock() + min(60, 5 * 2 ** (len(history) - 1)))
                attempt.update(completed_at=stamp(), latency_seconds=max(0, clock() - start))
                vote.update(attempt)
                write_json(path, vote)
                event = {k: v for k, v in attempt.items() if k != "response"}
                event.update(job_id=job["job_id"], provider=binding["provider"], model=binding["model"],
                             usage=_response_usage(attempt.get("response")))
                with mutex:
                    active -= 1
                    set_state(job, attempt["status"])
                    with (directory / "ledger.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    window = windows[binding["provider"]]
                    window.append(attempt["status"])
                    if len(window) == 200 and (window.count("error") / 200 > .05 or window.count("unparsed") / 200 > .03):
                        halted.add(binding["provider"])
                        provider_cancel[binding["provider"]].set()
                        halt_reasons.setdefault(binding["provider"], {
                            "reason": "error_or_parse_rate", "requires_explicit_recovery": False})
            if attempt["status"] != "error" or not attempt.get("retryable"):
                return

    emit("start")
    try:
        with ThreadPoolExecutor(max_workers=max(1, sum(limits.get(p, 1) for p in pools))) as executor:
            # A fixed number of workers pull jobs; do not submit 100,000 futures at once.
            futures = []
            for provider, items in pools.items():
                queue = deque(items)
                limiter = RequestLimiter(limits.get(provider, 1))
                def drain(queue=queue, limiter=limiter, provider=provider):
                    while not stopped.is_set():
                        with mutex:
                            if not queue or count >= max_requests or provider in halted:
                                return
                            job = queue.popleft()
                        work(job, limiter)
                futures.extend(executor.submit(drain) for _ in range(limiter.limit))
            try:
                pending = set(futures)
                next_progress = time.monotonic() + progress_interval
                while pending:
                    timeout = max(0, next_progress - time.monotonic()) if progress else None
                    done, pending = wait(pending, timeout=timeout,
                                         return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()
                    if progress is not None and time.monotonic() >= next_progress:
                        emit("progress")
                        next_progress = time.monotonic() + progress_interval
            except BaseException:
                stopped.set()
                for cancelled in provider_cancel.values():
                    cancelled.set()
                raise
    finally:
        emit("end")
    return {"new_requests": count, "existing_receipts": reused, "automatic_retries": retries,
            "max_sends_per_job": MAX_SENDS, "halted_providers": sorted(halted),
            "provider_stops": halt_reasons, "requires_config_recovery": bool(stop_state["active"])}


def run_jobs(directory, max_requests, client_factory=None, **options):
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError("A positive explicit request budget is required")
    directory = Path(directory)
    with (directory / ".runner.lock").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This jobs directory already has an active runner") from exc
        try:
            manifest = directory / "manifest.json"
            if manifest.exists() and json.loads(manifest.read_text()).get("schema") == "presence-store-v1":
                from .presence_judges import validate_store
                validate_store(directory)
            return _run_jobs_unlocked(directory, max_requests, client_factory, **options)
        finally:
            fcntl.flock(owner, fcntl.LOCK_UN)


def status(directory):
    from .file_task_judges import jsonl
    counts, sends, in_flight, costs = Counter(), 0, 0, []
    tokens, token_attempts, errors = Counter(), Counter(), Counter()
    cost_details, cost_detail_attempts = Counter(), Counter()
    last_activity = None
    seen, jp_votes, config_failures = set(), [], []
    for job in jsonl(Path(directory) / "jobs.jsonl"):
        validate_job(job)
        if job["job_id"] in seen:
            continue
        seen.add(job["job_id"])
        vote = read_receipt(directory, job)
        failure = _config_failure(job, vote)
        if failure:
            config_failures.append(failure)
        jp_votes.append({"job_id": job["job_id"], "role": job["role"],
                         "model": job["binding"]["model"], "provider": job["binding"]["provider"],
                         "alias": job["binding"].get("alias"), "status": vote["status"], "value": vote.get("value")})
        counts[vote["status"]] += 1
        sends += len(vote["attempts"])
        in_flight += vote["status"] == "started"
        for attempt in vote["attempts"]:
            usage = _response_usage(attempt.get("response")) or {}
            cost = usage.get("cost")
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                costs.append(cost)
            for key, value in usage.get("cost_details", {}).items():
                cost_details[key] += value
                cost_detail_attempts[key] += 1
            for name, aliases in {"input_tokens": ("input_tokens", "prompt_tokens"),
                                  "output_tokens": ("output_tokens", "completion_tokens"),
                                  "total_tokens": ("total_tokens",),
                                  "reasoning_tokens": ("reasoning_tokens",),
                                  "cached_tokens": ("cached_tokens",),
                                  "cache_write_tokens": ("cache_write_tokens",)}.items():
                value = next((usage[k] for k in aliases if type(usage.get(k)) is int and usage[k] >= 0), None)
                if value is not None:
                    tokens[name] += value
                    token_attempts[name] += 1
            if attempt.get("error_type"):
                errors[str(attempt.get("http_status") or attempt["error_type"])] += 1
            activity = attempt.get("completed_at") or attempt.get("started_at")
            if activity and (last_activity is None or activity > last_activity):
                last_activity = activity
    provider_stops = _inferred_stops(_stop_state(directory), config_failures)
    return {"jobs": sum(counts.values()), "states": dict(counts), "recorded_sends": sends,
            "uncertain_or_in_flight": in_flight, "max_sends_per_job": MAX_SENDS,
            "reported_tokens": dict(tokens), "token_reporting_attempts": dict(token_attempts),
            "errors": dict(errors), "last_activity_at": last_activity,
            "reported_cost_sum": sum(costs) if costs else None, "costed_attempts": len(costs),
            "reported_cost_details": dict(cost_details), "cost_detail_attempts": dict(cost_detail_attempts),
            "judge_votes": judge_vote_summary(jp_votes),
            "halted_providers": sorted(provider_stops), "provider_stops": provider_stops,
            "requires_config_recovery": bool(provider_stops)}
