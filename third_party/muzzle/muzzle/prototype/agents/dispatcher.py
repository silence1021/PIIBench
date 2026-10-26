"""Dispatcher Agent
---------------------

LLM-driven agent that composes an actionable red-team web-agent task from two
inputs:
    1) attack_method: Dict with keys like {element, how, why, confidence}
    2) malicious_instruction: Dict with key {injection}

Output: a dict with a single field "task" that instructs a browser web-agent
on precisely how to carry out the attack (where to inject and what content).

This agent subscribes to the DISPATCHER topic, calls the LLM with a strict
system prompt, parses the JSON output, and publishes a DispatchTask to the
OUTPUT topic. It includes a safe fallback template if the LLM output is not
valid JSON.
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict
from dotenv import load_dotenv
load_dotenv()

from autogen_core import (
    MessageContext,
    RoutedAgent,
    TopicId,
    message_handler,
    type_subscription,
)

from autogen_core.models import ChatCompletionClient, SystemMessage, UserMessage

from ..types.topics import DISPATCHER_TOPIC_TYPE, EXPLORER_TOPIC_TYPE
from ..types.messages import DispatchRequest, DispatchTask
from .utils import load_system_prompt, extract_json_object
from ..utils.storage.controller import StorageController

@type_subscription(topic_type=DISPATCHER_TOPIC_TYPE)
class DispatcherAgent(RoutedAgent):
    def __init__(self, model_client: ChatCompletionClient, *, storage=None) -> None:
        super().__init__("Dispatcher Agent")
        self._model = model_client
        # Load system prompt from YAML
        system_prompt_text = load_system_prompt(
            os.path.join(os.path.dirname(__file__), "prompts", "dispatcher.yaml")
        )
        self._system_prompt = SystemMessage(content=system_prompt_text)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._storage_controller = storage if storage is not None else StorageController()

    @message_handler
    async def handle_dispatch(self, message: DispatchRequest, ctx: MessageContext) -> None:
        """Handle an incoming DispatchRequest and publish a composed DispatchTask via LLM.

        If the LLM fails to return valid JSON, fall back to a deterministic template.
        """
        attack_method = message.attack_method or {}
        malicious_instruction = message.malicious_instruction or {}

        self.logger.info("Running Dispatcher...")
        OUT_OK, MAX_TRY = False, 5
        while not OUT_OK and MAX_TRY > 0:
            try:
                llm_result = await self._model.create(
                    messages=[
                        self._system_prompt,
                        UserMessage(content=json.dumps(attack_method, separators=(",", ":")), source=self.id.key),
                    ],
                    cancellation_token=ctx.cancellation_token,
                )

                raw = llm_result.content
                assert isinstance(raw, str)
                task_obj = extract_json_object(raw)

                if not isinstance(task_obj, dict) or "task" not in task_obj or not isinstance(task_obj["task"], str) or not task_obj["task"].strip():
                    # Fallback to deterministic composition
                    raise ValueError("Invalid or missing 'task' field in LLM response; falling back.")

                if "[injection]" in task_obj["task"]:
                    task_obj["task"] = task_obj["task"].replace("[injection]", "\""+malicious_instruction["injection"]+"\"")
                    self.logger.info("Dispatched Task: \n" + json.dumps(task_obj, indent=2) + "\n")

                    # Store dispatched task
                    await self._storage_controller.store_dispatched_task(
                        task_obj, 
                        message.run_id, 
                        goal_idx=message.goal_idx
                    )
                    OUT_OK = True # break the loop
                else:
                    raise ValueError("Dispatcher task text missing [injection] placeholder; injection content not included.")
            except Exception as e:
                self.logger.error(f"Error during Dispatcher LLM processing: {e}. Retries left: {MAX_TRY-1}")
                MAX_TRY -= 1

        if not OUT_OK:
            raise ValueError("dispatcher exhausted its 5 attempts without valid output")

        await self.publish_message(
            DispatchTask(
                run_id=message.run_id,
                attack_method=attack_method,
                malicious_instruction=malicious_instruction,
                task=task_obj, 
                mode=message.mode,
                goal_idx=message.goal_idx,
                reflection=message.reflection
            ),
            topic_id=TopicId(EXPLORER_TOPIC_TYPE, source=self.id.key),
        )
