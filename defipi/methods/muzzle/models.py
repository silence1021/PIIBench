"""Model/storage boundaries for the pinned MUZZLE and PAIR author code."""
from __future__ import annotations

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from defipi.methods.muzzle.browser import BudgetExceeded, ExecutionStopped, model_request, response_text, write_json

ROOT = _PACKAGE_ROOT
for directory in (ROOT / "third_party/muzzle", ROOT / "third_party/muzzle/modules/JailbreakingLLMs"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from autogen_core.models import CreateResult, RequestUsage
from muzzle.prototype.utils.storage.controller import StorageController
from PAIR import common
from PAIR.conversers import AttackLM
from PAIR.judges import GPTJudge
from PAIR.loggers import logger


class FileStorage(StorageController):
    """Use the author's store_* API without a MongoDB service."""
    def __init__(self, directory):
        super().__init__(connect=False)
        self.directory = Path(directory)
        self.index = 0

    async def create_run(self, task_spec, run_id=None):
        run_id = run_id or self.directory.parent.name
        write_json(self.directory / "task_spec.json", asdict(task_spec))
        return run_id

    async def _conditional_store(self, artifact_type, data, run_id, goal_idx):
        self.index += 1
        path = self.directory / f"{self.index:03d}-{artifact_type}.json"
        write_json(path, {"run_id": run_id, "goal_idx": goal_idx, "data": data})
        return str(path)


class RoleClient:
    """AutoGen's create interface bound to the shared budgeted model gateway."""
    def __init__(self, engine, role, name, appendix=""):
        self.engine, self.role, self.name, self.appendix = engine, role, name, appendix

    def call(self, messages):
        if self.engine.fatal is not None:
            raise self.engine.fatal
        instructions = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if self.appendix:
            instructions += "\n\n" + self.appendix
        inputs = [m for m in messages if m["role"] != "system"]
        request = model_request(self.engine.config["roles"][self.role], inputs, instructions)
        try:
            response = self.engine.gateway.call(
                request, purpose=f"{self.engine.page_dir.parent.name}:{self.engine.record['slug']}:author:{self.name}")
        except (BudgetExceeded, ExecutionStopped) as exc:
            self.engine.fatal = exc
            raise
        if response.get("status") != "completed" or not response_text(response):
            raise ValueError(f"Incomplete author {self.name} response")
        return response_text(response), response.get("usage", {})

    async def create(self, messages, **_kwargs):
        plain = [{"role": "system" if m.type == "SystemMessage" else
                  "assistant" if m.type == "AssistantMessage" else "user", "content": m.content}
                 for m in messages]
        text, usage = await asyncio.to_thread(self.call, plain)
        return CreateResult(content=text, finish_reason="stop", cached=False,
                            usage=RequestUsage(prompt_tokens=usage.get("input_tokens", 0),
                                               completion_tokens=usage.get("output_tokens", 0)))


class Conversation:
    """Chat-only conversation container; PAIR owns history updates."""
    roles = ("user", "assistant")

    def __init__(self, name):
        self.name, self.system_message, self.messages = name, "", []

    def set_system_message(self, text):
        self.system_message = text

    def append_message(self, role, content):
        self.messages.append([role, content])

    def update_last_message(self, content):
        self.messages[-1][1] = content

    def to_openai_api_messages(self):
        # Match FastChat's positional role serialization. The patched AttackLM
        # keeps each user feedback message paired with its assistant candidate.
        result = [{"role": "system", "content": self.system_message}] if self.system_message else []
        result.extend({"role": self.roles[index % 2], "content": content}
                      for index, (_role, content) in enumerate(self.messages)
                      if index % 2 == 0 or content is not None)
        return result


class BatchModel:
    use_open_source_model = False

    def __init__(self, client):
        self.client = client

    def batched_generate(self, conversations, **_author_sampling_defaults):
        # Each role's configuration replaces the author's legacy sampling and
        # token defaults. All providers' attempts use the shared budget.
        with ThreadPoolExecutor(max_workers=self.client.engine.config["parallelism"]) as pool:
            return list(pool.map(lambda messages: self.client.call(messages)[0], conversations))


class PairLog:
    """WandBLogger's consumed interface, persisted locally without W&B traffic."""
    def __init__(self, engine, args, system_prompts):
        self.engine = engine
        self.is_jailbroken = False
        self.jailbreak_prompt = self.jailbreak_response = None
        self.rows = []
        self.path = engine.page_dir / "pair" / f"{engine.pair_count:03d}"
        write_json(self.path / "config.json", {**vars(args), "system_prompts": system_prompts,
                   "effective_roles": engine.config["roles"], "legacy_sampling_parameters_ignored": True})

    def log(self, iteration, attack_list, response_list, judge_scores):
        if any(score == 10 for score in judge_scores):
            index = judge_scores.index(10)
            self.is_jailbroken = True
            self.jailbreak_prompt = attack_list[index]["prompt"]
            self.jailbreak_response = response_list[index]
        self.rows.append({"iteration": iteration, "attacks": attack_list,
                          "responses": response_list, "scores": judge_scores})
        write_json(self.path / "iterations.json", self.rows)

    def finish(self):
        write_json(self.path / "outcome.json", {
            "is_jailbroken": self.is_jailbroken, "selected_prompt": self.jailbreak_prompt,
            "meaning": "PAIR next-step proxy only; complete browser validation is separate"})


class PairBackend:
    """Keep upstream AttackLM, GPTJudge, prompts, feedback and search loop."""
    logger = logger
    process_target_response = staticmethod(common.process_target_response)

    def __init__(self, engine):
        self.engine = engine
        # PAIR assigns the extracted web-agent system prompt through this API.
        self.model = SimpleNamespace(system_prompt=None)

    def initialize_conversations(self, *args, **kwargs):
        return common.initialize_conversations(*args, **kwargs, conversation_factory=Conversation)

    def WandBLogger(self, args, system_prompts):
        return PairLog(self.engine, args, system_prompts)

    def load_attack_and_target_models(self, args):
        self.model.system_prompt = None
        attack = AttackLM(args.attack_model, args.attack_max_n_tokens, args.max_n_attack_attempts,
                          args.category, args.evaluate_locally,
                          model=BatchModel(RoleClient(self.engine, "generator", "pair_attacker")),
                          template=args.attack_model)
        return attack, self

    def load_judge(self, args):
        return GPTJudge(args, model=BatchModel(RoleClient(self.engine, "judge", "pair_judge")),
                        conversation_factory=Conversation)

    def get_response(self, prompts):
        # The author loop retains stream order; shared Pi/model pools cap actual work.
        with ThreadPoolExecutor(max_workers=self.engine.config["parallelism"]) as pool:
            return list(pool.map(lambda prompt: self.engine.pair_response(self.model.system_prompt, prompt), prompts))
