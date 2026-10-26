import os
import logging
import json
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

from ..types.topics import GRAFTER_TOPIC_TYPE, EXPLORER_TOPIC_TYPE
from ..types.messages import GrafterMessage, GraftResult
from .utils import load_system_prompt, extract_json_object
from ..utils.storage.controller import StorageController


@type_subscription(topic_type=GRAFTER_TOPIC_TYPE)
class GrafterAgent(RoutedAgent):
	def __init__(self, model_client: ChatCompletionClient, *, storage=None) -> None:
		super().__init__("Grafter Agent")
		self._model = model_client
		system_prompt_text = load_system_prompt(
			os.path.join(os.path.dirname(__file__), "prompts", "grafter.yaml")
		)
		self._system_prompt = SystemMessage(content=system_prompt_text)
		self.logger = logging.getLogger(self.__class__.__name__)
		self._storage_controller = storage if storage is not None else StorageController()

	@message_handler
	async def handle_step(
		self,
		message: GrafterMessage,
		ctx: MessageContext,
	) -> None:
		# Extract the two playbooks
		base_playbook = message.base_playbook

		base_text = str(base_playbook)

		# Prepare the prompt for the LLM
		if message.reflection is not None:
			reflection_notes = "\n".join([f"- {note}" for note in message.reflection])
			prompt = (
				"Base playbook (minified JSON):\n" + base_text +
				"\n\nIMPORTANT SUGGESTIONS FOR BETTER OUTPUT:\n" + reflection_notes +
				"\n\nReturn ONLY the JSON object as specified."
			)
		else:
			prompt = (
				"Base playbook (minified JSON):\n" + base_text +
				"\n\nReturn ONLY the JSON object as specified."
			)

		# Generate output from the LLM
		self.logger.info("Running Grafter...")
		OUT_OK, MAX_TRY = False, 5
		while not OUT_OK and MAX_TRY > 0:
			try:
				llm_result = await self._model.create(
					messages=[
						self._system_prompt,
						UserMessage(content=prompt, source=self.id.key),
					],
					cancellation_token=ctx.cancellation_token,
				)

				# Extract the response
				raw = llm_result.content
				assert isinstance(raw, str)
				graft_obj = extract_json_object(raw)
				if graft_obj is None or not isinstance(graft_obj, dict):
					raise ValueError("GrafterAgent: LLM output invalid JSON.")
				OUT_OK = True
			except Exception as e:
				self.logger.error(f"Grafter LLM call failed with error: {e}. Retrying...")
				MAX_TRY -= 1

		if not OUT_OK:
		    raise ValueError("grafter exhausted its 5 attempts without valid output")

		# Log the generated elements
		self.logger.info(f"Generated elements:\n{json.dumps(graft_obj, indent=2)}\n")

		# Store graft
		await self._storage_controller.store_graft(graft_obj, message.run_id)

		# Publish back to Explorer:
		await self.publish_message(
                GraftResult(
					run_id=message.run_id,
					elements=graft_obj,
					playbook=message.base_playbook,
					reflected=False if message.reflection is None else True
				),
                topic_id=TopicId(
                    EXPLORER_TOPIC_TYPE, 
                    source=self.id.key
                )
            )