import os
import logging
import json
import asyncio
import re
from types import SimpleNamespace
from rich.progress import Progress
from dotenv import load_dotenv
load_dotenv()

from autogen_core import (
    MessageContext,
    RoutedAgent,
    TopicId,
    message_handler,
    type_subscription,
)

from ..types.topics import PAIR_TOPIC_TYPE, EXPLORER_TOPIC_TYPE
from ..types.messages import PAIRMessage, PAIRConfig, PromptInjectionMessage, InstructionMessage
from ..utils.storage.controller import StorageController

# PAIR package imports will be loaded dynamically in _run_pair to avoid
# import resolution issues in environments where PAIR isn't on PYTHONPATH yet.

class PlaceHolderNotFoundError(Exception):
    """Raised when a required placeholder string is not found in the transcript template."""
    pass

@type_subscription(topic_type=PAIR_TOPIC_TYPE)
class PAIRAgent(RoutedAgent):
    def __init__(self, *, storage=None, backend=None) -> None:
        super().__init__("PAIR Agent")
        self.logger = logging.getLogger(self.__class__.__name__)
        self._storage_controller = storage if storage is not None else StorageController()
        self._backend = backend

    def _load_transcript_template(self, transcript) -> tuple[str, str | list | dict | None]:
        """Load and dynamically extract the transcript step and the target system prompt.

        Scans the transcript to collect all unique system prompts (in order of
        first appearance), finds the entry whose message contains
        ``[INSTRUCTION_PLACEHOLDER]``, and returns the system prompt that was
        sent in *that same request*.

        If the accompanying system prompt is a redacted marker such as
        ``[repeated system prompt]`` or ``[repeated system prompt 2]``, it is
        resolved back to the corresponding original unique prompt.

        Returns (step_text, web_agent_system_prompt)
        - step_text: string of the template step containing "[PLACEHOLDER]"
        - web_agent_system_prompt: the system prompt content that accompanied
          the placeholder message (raw content: str | list | dict), or None
        """
        _REPEATED_RE = re.compile(r"^\[repeated system prompt(?:\s+(\d+))?\]$")

        def _extract_texts(content) -> list[str]:
            """Extract text content from various possible shapes (str | list | dict)."""
            texts: list[str] = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        texts.append(item)
                    elif isinstance(item, dict):
                        if 'text' in item and isinstance(item['text'], str):
                            texts.append(item['text'])
                        if 'content' in item:
                            texts.extend(_extract_texts(item['content']))
            elif isinstance(content, dict):
                if 'text' in content and isinstance(content['text'], str):
                    texts.append(content['text'])
                if 'content' in content:
                    texts.extend(_extract_texts(content['content']))
            return [t for t in texts if isinstance(t, str)]

        data = transcript

        # --- Pass 1: collect all unique system prompts in order of first appearance ---
        unique_system_prompts: list[str | list | dict] = []  # 0-indexed
        seen_prompt_keys: set[str] = set()

        for entry in data:
            request = entry.get('request', {}) if isinstance(entry, dict) else {}
            json_msgs = request.get('json', []) if isinstance(request, dict) else []
            if not isinstance(json_msgs, list):
                continue
            for msg in json_msgs:
                if not isinstance(msg, dict) or msg.get('role') != 'system':
                    continue
                content = msg.get('content')
                if content is None:
                    continue
                texts = _extract_texts(content)
                if not texts:
                    continue
                # Skip redacted markers
                if isinstance(content, str) and _REPEATED_RE.match(content.strip()):
                    continue
                key = json.dumps(content, sort_keys=True) if not isinstance(content, str) else content
                if key not in seen_prompt_keys:
                    seen_prompt_keys.add(key)
                    unique_system_prompts.append(
                        content if isinstance(content, (str, list, dict)) else texts[0]
                    )

        # --- Pass 2: find the placeholder message and the system prompt in the same entry ---
        step_text: str | None = None
        accompanying_system_prompt: str | list | dict | None = None

        for entry in data:
            request = entry.get('request', {}) if isinstance(entry, dict) else {}
            json_msgs = request.get('json', []) if isinstance(request, dict) else []
            if not isinstance(json_msgs, list):
                continue

            # Check if this entry contains the placeholder
            entry_step_text: str | None = None
            for msg in json_msgs:
                if not isinstance(msg, dict) or 'content' not in msg:
                    continue
                for t in _extract_texts(msg['content']):
                    if isinstance(t, str) and "[INSTRUCTION_PLACEHOLDER]" in t:
                        entry_step_text = t
                        break
                if entry_step_text is not None:
                    break

            if entry_step_text is None:
                continue

            step_text = entry_step_text

            # Now find the system prompt in this same entry
            for msg in json_msgs:
                if not isinstance(msg, dict) or msg.get('role') != 'system':
                    continue
                content = msg.get('content')
                if content is None:
                    continue

                # If it's a redacted marker, resolve it back to the original
                if isinstance(content, str):
                    m = _REPEATED_RE.match(content.strip())
                    if m:
                        # "[repeated system prompt]" -> index 1 (first unique)
                        # "[repeated system prompt 2]" -> index 2, etc.
                        idx = int(m.group(1)) if m.group(1) else 1
                        if 1 <= idx <= len(unique_system_prompts):
                            accompanying_system_prompt = unique_system_prompts[idx - 1]
                        break

                # Otherwise it's a real (non-redacted) system prompt
                accompanying_system_prompt = content
                break

            break  # found the placeholder entry, no need to continue

        if step_text is None:
            raise PlaceHolderNotFoundError(
                "No transcript step containing '[INSTRUCTION_PLACEHOLDER]' found in transcript template."
            )

        return step_text, accompanying_system_prompt

    def _run_pair(self, args: PAIRConfig) -> dict:
        """Run the PAIR pipeline synchronously and return a dict with best prompt and score.

        Returns: {"best_prompt": str, "best_score": float, "best_improvement": str}
        """
        if self._backend is not None:
            WandBLogger = self._backend.WandBLogger
            pair_logger = self._backend.logger
            load_judge = self._backend.load_judge
            load_attack_and_target_models = self._backend.load_attack_and_target_models
            process_target_response = self._backend.process_target_response
            initialize_conversations = self._backend.initialize_conversations
        else:
            # Resolve PAIR modules dynamically
            import sys as _sys
            import importlib as _importlib
            try:
                _pair_loggers = _importlib.import_module("PAIR.loggers")
                _pair_judges = _importlib.import_module("PAIR.judges")
                _pair_conversers = _importlib.import_module("PAIR.conversers")
                _pair_common = _importlib.import_module("PAIR.common")
            except ModuleNotFoundError:
                _local_pair_root = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "../../../modules/JailbreakingLLMs")
                )
                if _local_pair_root not in _sys.path:
                    _sys.path.append(_local_pair_root)
                _pair_loggers = _importlib.import_module("PAIR.loggers")
                _pair_judges = _importlib.import_module("PAIR.judges")
                _pair_conversers = _importlib.import_module("PAIR.conversers")
                _pair_common = _importlib.import_module("PAIR.common")

            WandBLogger = getattr(_pair_loggers, "WandBLogger")
            pair_logger = getattr(_pair_loggers, "logger")
            load_judge = getattr(_pair_judges, "load_judge")
            load_attack_and_target_models = getattr(_pair_conversers, "load_attack_and_target_models")
            process_target_response = getattr(_pair_common, "process_target_response")
            initialize_conversations = getattr(_pair_common, "initialize_conversations")

        # Configure PAIR logger verbosity
        pair_logger.set_level(args.verbosity)

        transcript_step, web_agent_system_prompt = (None, None)
        if args.transcript:
            transcript_step, web_agent_system_prompt = self._load_transcript_template(args.transcript)

        # Initialize models and judge
        attackLM, targetLM = load_attack_and_target_models(args)
        if web_agent_system_prompt is not None:
            # Override target system prompt to match the web agent
            targetLM.model.system_prompt = web_agent_system_prompt
        judgeLM = load_judge(args)

        # Initialize conversations
        convs_list, processed_response_list, system_prompts = initialize_conversations(
            args.n_streams, 
            args.goal, 
            args.target_str, 
            attackLM.template, 
            original_user_task=args.original_user_task, 
            feedback=args.feedback
        )

        wandb_logger = WandBLogger(args, system_prompts)
        target_response_list, judge_scores = None, None

        adv_prompt_list, improv_list = [], []
        best_idx, best_score = None, float('-inf')

        self.logger.info("Starting PAIR iterations...")
        with Progress(disable=self._backend is not None) as progress:
            total_iterations = args.n_iterations + 1
            task = progress.add_task("Iterating...", total=total_iterations)
            for iteration in range(1, total_iterations):
                if iteration > 1 and target_response_list is not None and judge_scores is not None:
                    processed_response_list = [
                        process_target_response(tr, sc, args.goal, args.target_str)
                        for tr, sc in zip(target_response_list, judge_scores)
                    ]

                # Get adversarial prompts and improvements
                extracted_attack_list = attackLM.get_attack(convs_list, processed_response_list)
                self.logger.debug("Finished getting adversarial prompts.")

                # Extract prompts and improvements
                adv_prompt_list = [attack["prompt"] for attack in extracted_attack_list]
                improv_list = [attack["improvement"] for attack in extracted_attack_list]

                # Apply transcript template step if provided
                if transcript_step is not None:
                    placeholder_token = None
                    for token in ("[INSTRUCTION_PLACEHOLDER]", "[PLACEHOLDER]"):
                        if token in transcript_step:
                            placeholder_token = token
                            break

                    if placeholder_token is not None:
                        adv_prompt_list = [transcript_step.replace(placeholder_token, p) for p in adv_prompt_list]
                        for adv_prmpt in adv_prompt_list:
                            assert placeholder_token not in adv_prmpt, "The placeholder was not replaced correctly."
                    else:
                        # If no known placeholder is found, fall back to concatenation
                        adv_prompt_list = [f"{transcript_step}\n\n{p}" for p in adv_prompt_list]

                # Get target responses
                target_response_list = targetLM.get_response(adv_prompt_list)
                self.logger.debug("Finished getting target responses.")

                # Get judge scores
                judge_scores = judgeLM.score(adv_prompt_list, target_response_list)
                self.logger.debug("Finished getting judge scores.")
                # Log to WandB
                wandb_logger.log(iteration, extracted_attack_list, target_response_list, judge_scores)

                # Truncate conversation to avoid context length issues
                for i, conv in enumerate(convs_list):
                    conv.messages = conv.messages[-2 * (args.keep_last_n):]

                # Track best so far
                for idx, sc in enumerate(judge_scores):
                    if sc is not None and sc > best_score:
                        best_score = sc
                        best_idx = idx

                # Early stopping criterion
                if any([score == 10 for score in judge_scores]):
                    self.logger.info("Found a jailbreak. Exiting.")
                    break
                progress.update(task, advance=1)

        wandb_logger.finish()

        if best_idx is None:
            # Fallback: choose first if available
            if adv_prompt_list:
                best_idx = 0
                best_score = judge_scores[0] if judge_scores else None
            else:
                return {"best_prompt": None, "best_score": None, "best_improvement": None}

        return {
            "is_jailbroken": wandb_logger.is_jailbroken if wandb_logger.is_jailbroken else None,
            "best_prompt": wandb_logger.jailbreak_prompt if wandb_logger.jailbreak_prompt else None,
            "best_response": wandb_logger.jailbreak_response if wandb_logger.jailbreak_response else None,
            "best_score": best_score,
            "best_improvement": improv_list[best_idx] if improv_list else None,
        }

    @message_handler
    async def handle_step(
        self, 
        message: PAIRMessage, 
        ctx: MessageContext
    ) -> None:

        # Combine missing arguments with the rest of the PAIRConfig
        message.pair_cfg.transcript = message.transcript
        message.pair_cfg.goal = message.instructions
        message.pair_cfg.feedback = message.reflection
        message.pair_cfg.target_str = message.pair_cfg.target_str + message.instructions        

        # Run PAIR synchronously in a thread to avoid blocking the async handler
        self.logger.info("Running PAIR pipeline to generate adversarial injection...")
        i, MAX_TRIES = 0, 5
        result = None
        while i < MAX_TRIES:
            try:
                result = await asyncio.to_thread(self._run_pair, message.pair_cfg)
                if result and result.get("best_prompt") is not None:
                    break  # Success
                elif not result.get("is_jailbroken"):
                    self.logger.info(f"PAIR completed iteration {i+1}/{MAX_TRIES} but did not find a prompt.")
                    break
                else:
                    self.logger.warning(f"PAIR did not produce a valid prompt on attempt {i+1}/{MAX_TRIES}. Retrying...")
                    i += 1
            except PlaceHolderNotFoundError as pe:
                self.logger.error(f"Transcript template error during PAIR execution: {pe}")
                output_obj = {
                    "explanation": f"PAIR execution failed due to transcript template error: {pe}",
                    "prompt": "PAIR Failure: proceed to the next element.",
                }
                # Publish instructions to Explorer
                await self.publish_message(
                    InstructionMessage(
                        run_id=message.run_id,
                        prompt=output_obj,
                        goal_idx=message.goal_idx,
                        reflection=False,
                        skip_element=True
                    ),
                    topic_id=TopicId(
                        EXPLORER_TOPIC_TYPE, 
                        source=self.id.key
                    )
                )
                return
            except Exception as e:
                self.logger.error(f"Error during PAIR execution (attempt {i+1}/{MAX_TRIES}): {e}")
                i += 1
        if result is None or result.get("best_prompt") is None:
            self.logger.error("PAIR failed to produce a valid prompt after multiple attempts.")
            return

        best_prompt = result["best_prompt"]
        best_score = result.get("best_score")
        best_improv = result.get("best_improvement")

        # Construct the output object expected downstream
        reasoning_parts = []
        if best_score is not None:
            reasoning_parts.append(f"Selected via PAIR; judge score={best_score}")
        if best_improv:
            reasoning_parts.append(f"Improvement hint: {best_improv}")
        reasoning = "; ".join(reasoning_parts) if reasoning_parts else "Selected via PAIR."

        output_obj = {
            "reasoning": reasoning,
            "injection": best_prompt,
        }

        # Log for visibility
        self.logger.info(f"Generated injection via PAIR:\n{json.dumps(output_obj, indent=2)}\n")

        # Store prompt injection
        if best_prompt is not None:
            await self._storage_controller.store_prompt_injection(
                output_obj,
                message.run_id,
                goal_idx=message.goal_idx
            )

        # Publish output to final Output topic
        await self.publish_message(
            PromptInjectionMessage(
                run_id=message.run_id,
                output=output_obj,
                goal_idx=message.goal_idx,
                reflection=False if message.reflection is None else True
            ),
            topic_id=TopicId(EXPLORER_TOPIC_TYPE, source=self.id.key),
        )
