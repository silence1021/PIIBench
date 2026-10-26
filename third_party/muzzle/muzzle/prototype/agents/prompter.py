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
from autogen_core.models import ChatCompletionClient, SystemMessage, UserMessage

from ..types.topics import PROMPTER_TOPIC_TYPE, EXPLORER_TOPIC_TYPE
from ..types.messages import PrompterMessage, InstructionMessage
from .utils import load_system_prompt, extract_json_object
from ..utils.storage.controller import StorageController

@type_subscription(topic_type=PROMPTER_TOPIC_TYPE)
class PrompterAgent(RoutedAgent):
    def __init__(self, model_client: ChatCompletionClient, *, storage=None) -> None:
        super().__init__("Prompter Agent")
        self._model = model_client
        system_prompt_text = load_system_prompt(os.path.join(os.path.dirname(__file__), "prompts", "prompter.yaml"))
        self._system_prompt = SystemMessage(
            content=system_prompt_text
        )
        self.logger = logging.getLogger(self.__class__.__name__)
        self._storage_controller = storage if storage is not None else StorageController()

    @message_handler
    async def handle_step(
        self, 
        message: PrompterMessage, 
        ctx: MessageContext
    ) -> None:
        
        # Extract the playbook:
        playbook_dict = message.playbook
        playbook_text = str(playbook_dict)

        # Prepare the prompt for the LLM:
        prompt = (
			"Replicated playbook (minified JSON):\n" + playbook_text +
			"\n\nGenerate ONLY the JSON object as specified:\n"
		)

        # Generate output from the LLM
        self.logger.info("Running Prompter...")
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
                prompt_obj = extract_json_object(raw)
                if prompt_obj is None or not isinstance(prompt_obj, dict):
                    raise ValueError("Invalid or missing JSON object in LLM response.")
                OUT_OK = True
            except Exception as e:
                self.logger.warning(f"Prompter Error during LLM processing or JSON extraction: {e}")
                MAX_TRY -= 1
        
        if not OUT_OK:
            raise ValueError("prompter exhausted its 5 attempts without valid output")

        # Print
        self.logger.info(f"Generated prompt:\n{json.dumps(prompt_obj, indent=2)}\n")

        # Store prompt
        await self._storage_controller.store_prompt(
            prompt_obj,
            message.run_id,
            goal_idx=message.goal_idx
        )

        # Publish instructions to Explorer
        await self.publish_message(
            InstructionMessage(
                run_id=message.run_id,
                prompt=prompt_obj,
                goal_idx=message.goal_idx
            ),
            topic_id=TopicId(
                EXPLORER_TOPIC_TYPE, 
                source=self.id.key
            )
        )