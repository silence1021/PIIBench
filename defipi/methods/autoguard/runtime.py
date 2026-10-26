"""Shared request-attempt budgets and process-safe execution slots."""
from __future__ import annotations

from defipi.concurrency import FileSlots

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import ThreadPoolExecutor
import asyncio
from functools import wraps

MAX_ATTEMPTS = 3
API_CONCURRENCY = 20
AGENT_CONCURRENCY = 5
RUNTIME_VERSION = "autoguard-parallel-execution-v2"
_request_budget = ContextVar("autoguard_request_budget", default=None)


class AttemptLimitExceeded(RuntimeError):
    pass


class RequestBudget:
    def __init__(self):
        self.used = 0

    def consume(self):
        if self.used >= MAX_ATTEMPTS:
            raise AttemptLimitExceeded("request exhausted its three total HTTP/parse attempts")
        self.used += 1


@contextmanager
def request_attempt_scope():
    """Nested transport, parser, and judgment retries share one three-attempt limit."""
    budget = _request_budget.get()
    if budget is not None:
        yield budget
        return
    budget = RequestBudget()
    token = _request_budget.set(budget)
    try:
        yield budget
    finally:
        _request_budget.reset(token)


def call_model(client, **kwargs):
    callback = kwargs.pop("before_request_attempt", None)
    with request_attempt_scope() as budget:
        def before_request_attempt():
            budget.consume()
            if callback is not None:
                callback()
        def generate():
            return client.generate(**kwargs, before_request_attempt=before_request_attempt)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return generate()
        # BrowserGym keeps Playwright's loop alive even after an episode closes.
        # Run every affected provider role on a worker, preserving the shared
        # request budget across HTTP, AgentLab parsing, and judgment retries.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoguard-model") as executor:
            return executor.submit(copy_context().run, generate).result()


def agent_slots(config):
    directory = config["limits"]["agent_slot_directory"]
    return FileSlots(directory, AGENT_CONCURRENCY)


class ModelClientFactory:
    """Picklable model-client factory for spawned search workers."""
    def __call__(self, model, role):
        from defipi.execution.api.clients import build_text_client
        backend = model["backend"]
        return build_text_client(model=model["model"], backend=backend,
                                 client_settings=model["client_settings"], max_retries=MAX_ATTEMPTS, retry_backoff_seconds=1.0)


def model_client_factory(config):
    return ModelClientFactory()


def search_workers(config):
    workers = config["limits"]["search_workers"]
    if type(workers) is not int or not 1 <= workers <= AGENT_CONCURRENCY:
        raise ValueError(f"limits.search_workers must be an integer from 1 to {AGENT_CONCURRENCY}")
    return workers


def exclusive_search_run(function):
    """A run has one coordinator; worker jobs only write their own directories."""
    @wraps(function)
    def run(self, *args, **kwargs):
        with FileSlots(self.output_dir / ".runtime/coordinator_slot", 1, busy=RuntimeError(
                "a search coordinator is already running in this output directory")):
            return function(self, *args, **kwargs)
    return run


def runtime_contract():
    return {"version": RUNTIME_VERSION, "request_max_attempts": MAX_ATTEMPTS,
            "agent_max_attempts": MAX_ATTEMPTS, "api_concurrency_per_task": API_CONCURRENCY,
            "agent_concurrency": AGENT_CONCURRENCY, "sdk_retries": 0,
            "request_retry_accounting": "HTTP and parser/judgment retries share three total attempts",
            "agent_retry_accounting": "three episode attempts total, persisted across resume",
            "scheduling": "spawned worker processes; parallel discovery chains and within-round cases; sequential bandit rounds",
            "search_workers_limit": AGENT_CONCURRENCY,
            "discovery_commit_order": "reserved proposal order within bounded waves"}
