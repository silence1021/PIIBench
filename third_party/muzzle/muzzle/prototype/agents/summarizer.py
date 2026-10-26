import os
import logging
import json

from autogen_core import (
    MessageContext,
    RoutedAgent,
    TopicId,
    message_handler,
    type_subscription,
)
from autogen_core.models import ChatCompletionClient, UserMessage, SystemMessage

from ..types.topics import SUMMARIZER_TOPIC_TYPE, PROMPTER_TOPIC_TYPE, GRAFTER_TOPIC_TYPE
from ..types.messages import SummarizerMessage, PrompterMessage, GrafterMessage
from ..types.enumerators import TranscriptType
from .utils import load_system_prompt, extract_json_object
from ..utils.storage.controller import StorageController

@type_subscription(topic_type=SUMMARIZER_TOPIC_TYPE)
class SummarizerAgent(RoutedAgent):
    def __init__(self, model_client: ChatCompletionClient, *, storage=None) -> None:
        super().__init__("Summarizer Agent")
        self._model = model_client
        system_prompt_text = load_system_prompt(os.path.join(os.path.dirname(__file__), "prompts", "summarizer.yaml"))
        self._system_prompt = SystemMessage(
            content=system_prompt_text
        )
        self.logger = logging.getLogger(self.__class__.__name__)
        self._storage_controller = storage if storage is not None else StorageController()

    async def extract_playbook(self, transcript: dict, ctx: MessageContext) -> dict:
        # Extract the transcript
        transcript_text = str(transcript)

        # Prepare the prompt for the LLM:
        prompt = (
            "Transcript (user/assistant chat, may include <step>/<think>/HTML):\n"
            "\n**START OF TRANSCRIPT**\n" + transcript_text.strip() + "\n**END OF TRANSCRIPT**\n"
            "\nProduce the playbook now, following the strict JSON schema."
        )

        # Generate output from the LLM
        self.logger.info("Running Summarizer...")
        OUT_OK, MAX_TRY = False, 5
        while not OUT_OK and MAX_TRY > 0:
            try:
                llm_result = await self._model.create(
                    messages=[
                        self._system_prompt, 
                        UserMessage(
                            content=prompt, 
                            source=self.id.key
                        )
                    ],
                    cancellation_token=ctx.cancellation_token,
                )
                
                # Extract the response
                raw = llm_result.content
                assert isinstance(raw, str)
                playbook_obj = extract_json_object(raw)
                if playbook_obj is None or not isinstance(playbook_obj, dict):
                    raise ValueError(
                        "Failed to extract valid JSON playbook from LLM response."
                    )
                OUT_OK = True
            except Exception as e:
                self.logger.error(
                    f"Error during Summarizer LLM processing: {e}. Retries left: {MAX_TRY-1}"
                )
                MAX_TRY -= 1
        
        if not OUT_OK:
            raise ValueError("summarizer exhausted its 5 attempts without valid output")

        # Print the generated playbook
        self.logger.info(f"Generated playbook:\n{json.dumps(playbook_obj, indent=2)}\n")
        return playbook_obj

    @message_handler
    async def handle_transcript(
        self,
        message: SummarizerMessage,
        ctx: MessageContext,
    ) -> None:

        # Extract the transcript
        playbook_obj = await self.extract_playbook(message.transcript, ctx)

        if message.transcript_type == TranscriptType.BENIGN:
            # Store playbook for run_id (if not none)
            await self._storage_controller.store_playbook(playbook_obj, message.run_id)

            # Publish the playbook to the Grafter topic since the transcript is benign
            # --> to ultimately come up with the attack method
            await self.publish_message(
                GrafterMessage(
                    run_id=message.run_id,
                    base_playbook=playbook_obj
                ),
                topic_id=TopicId(
                    GRAFTER_TOPIC_TYPE, 
                    source=self.id.key
                )
            )
        elif message.transcript_type == TranscriptType.ADVERSARIAL:
            # Store playbook for run_id (if not none)
            await self._storage_controller.store_playbook(
                playbook_obj, 
                message.run_id, 
                goal_idx=message.goal_idx
            )

            # Publish the playbook to the Prompter topic since the transcript is adversarial
            # --> to ultimately generate an injection
            await self.publish_message(
                PrompterMessage(
                    run_id=message.run_id,
                    playbook=playbook_obj,
                    goal_idx=message.goal_idx
                ),
                topic_id=TopicId(
                    PROMPTER_TOPIC_TYPE, 
                    source=self.id.key
                )
            )
        else:
            self.logger.error(
                f"Unknown transcript type: {message.transcript_type}"
            )
        return
