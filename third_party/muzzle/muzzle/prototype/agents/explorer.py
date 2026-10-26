import os
import asyncio
import datetime
import json
import logging
import docker
import re
from copy import deepcopy
from dotenv import load_dotenv

from typing import Dict, Any
from autogen_core import (  # noqa: E402
    MessageContext,
    RoutedAgent,
    TopicId,
    message_handler,
    type_subscription,
)

from ..types.enumerators import TranscriptType
from ..types.topics import (
    EXPLORER_TOPIC_TYPE,
    SUMMARIZER_TOPIC_TYPE,
    DISPATCHER_TOPIC_TYPE,
    PAIR_TOPIC_TYPE,
    JUDGE_TOPIC_TYPE,
)
from ..types.messages import (
    TaskSpec,
    AdversarialTask,
    SummarizerMessage,
    GraftResult,
    InstructionMessage,
    PAIRMessage,
    PAIRConfig,
    PromptInjectionMessage,
    DispatchRequest,
    DispatchTask,
    JudgeRequest,
    JudgeEvaluation,
)
from ..utils.network.vllm_proxy import VLLMProxy
from ..utils.storage.controller import StorageController
from ..utils.zoo.seeder import (
    ZooState,
    PLACEHOLDER_INSTR,
    SEEDABLES,
    APP_TO_DBMS_ENGINE,
    APP_TO_DB,
    APP_TO_ADV_CREDENTIALS,
)

load_dotenv()  # load environment variables from .env file

DEBUG_MODE = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")

# Agent Docker container halting condition Regular Expression
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", 15)) + 1
AGENT_HALT_CONDITION_REGEX = fr'(?i)step.*{AGENT_MAX_STEPS}|{AGENT_MAX_STEPS}.*step'

def config_to_base64(config):
    """
    Convert a configuration dictionary to a base64-encoded JSON string.
    Args:
        config (dict): The configuration dictionary.
    Returns:
        str: The base64-encoded JSON string.
    """
    import base64

    json_str = json.dumps(config)
    base64_bytes = base64.b64encode(json_str.encode("utf-8"))
    return base64_bytes.decode("utf-8")


def get_timestamp():
    """Return custom current timestamp as a string."""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")


@type_subscription(topic_type=EXPLORER_TOPIC_TYPE)
class Explorer(RoutedAgent):
    def __init__(self, *, storage=None, backend=None) -> None:
        super().__init__("Explorer (Non-LLM) Agent")
        """
        The Explorer is presented with a task spec. The task spec specifies the type of agent
        that is under evaluation, its user task and other relevant details regarding the agent runtime.
        Last but not least, the spec includes the 'adversarial_goals'. The 'adversarial_goals' is a list
        of objectives that the owner of the web-agent wants to test the agent against. In other words, the
        adversarial goals are outcomes that the agent should NOT achieve. The Explorer's job is to
        explore the behavior of the agent under evaluation and attempt to achieve the adversarial goals by:
        1. Emulating the user task specified in the task spec and observing the web-agent's behavior.
        2. Emulating the adversarial tasks specified in the task spec and observing the web-agent's behavior.
            Q1: Assume the adv goal was the benign user task. What would be the steps to achieve it?
            Q2: Assume the adv goal was a malicious task. What would be the steps to hijack the agent towards it?
        3. Extracting insights from the exploration in the form of the web-agent's LLM I/O transcripts.

        """
        self.logger = logging.getLogger(self.__class__.__name__)
        # Initialize the VLLM proxy controller
        self.vllm_proxy = backend.proxy if backend is not None else VLLMProxy(
            target_base_url=os.environ.get(
                "VLLM_UPSTREAM_URL", "http://localhost:4949/"
            ),
            port=os.environ.get("VLLM_PROXY_PORT", 7777),
        )
        # Init docker client
        self.docker_client = None if backend is not None else docker.from_env()
        self._proxy_start_delay = 0 if backend is not None else 3

        # Variables
        self._user_task_transcript: Dict[str, Any] = {}
        self._user_task_transcript_with_placeholder: Dict[str, Any] = {}
        self._user_task_playbook: Dict[str, Any] = {}
        self._elements: Dict[str, Any] = {}
        self._element_candidates_sorted: list[Dict[str, Any]] = []
        self._element_candidate_idx: int = 0
        self._instructions: Dict[int, Any] = {}
        self._injections: Dict[int, Any] = {}
        self._current_goal_idx = None
        self._last_successful_goal_idx = None

        # Initialize ZooState singleton
        self.zoo_state = None if backend is not None else ZooState()

        # Storage for the runs:
        self._storage_controller = storage if storage is not None else StorageController()

    def agent_credentials(self, credentials: Dict[str, Any]) -> str:
        """
        Create credentials string for the agent from a dict of credentials for different apps.

        Args:
            credentials (Dict[str, Any]): A dictionary mapping app URLs to their credentials.
        Returns:
            str: A formatted string of credentials for the agent.
        """
        CRED_TEMPLATE = "Your {} username is {} and your password is {}.\n"

        if not credentials:
            return "No credentials provided for this task."

        cred_str = ""
        for app_url, creds in credentials.items():
            cred_str += CRED_TEMPLATE.format(
                app_url,
                creds.get("username", "user"),
                creds.get("password", "pass"),
            )
        return cred_str

    def _convert_to_legacy_configs(
        self, task_spec: TaskSpec, goal_idx: int = None
    ) -> tuple:
        """Convert the TaskSpec to legacy task, agent, and browser configs."""
        # Convert TaskSpec to legacy configs
        # Extract the first app URL from dependencies
        task_config = {
            "default_url": task_spec.url,
            "task": task_spec.instruction
            if goal_idx is None
            else task_spec.adversarial_goals[goal_idx]["goal"],
            "parameters": {
                "creds": self.agent_credentials(
                    task_spec.requirements.get("credentials", {})
                )
            },
        }

        agent_config_path = os.environ.get("AGENT_CONFIG_PATH", None)
        if agent_config_path is None:
            raise ValueError("AGENT_CONFIG_PATH environment variable not set. Edit .env to include the path to the agent config JSON file.")
        with open(agent_config_path, "r") as f:
            agent_config = json.load(f)

        browser_config_path = os.environ.get("BROWSER_CONFIG_PATH", None)
        if browser_config_path is None:
            raise ValueError("BROWSER_CONFIG_PATH environment variable not set. Edit .env to include the path to the browser config JSON file.")
        with open(browser_config_path, "r") as f:
            browser_config = json.load(f)

        return task_config, agent_config, browser_config

    def _create_injection_task_spec(self, message: DispatchTask) -> TaskSpec:
        """Create a new TaskSpec for the injection task based on the DispatchTask message."""
        injection_task_spec = deepcopy(self.task_spec)
        # Replace the instruction with the dispatched task
        # TODO: remove and generalize 
        if self.task_spec.dependencies["apps"][0] == "classifieds.zoo":
            injection_task_spec.instruction = "After you first login, " + message.task["task"]
        else:
            injection_task_spec.instruction = message.task["task"]
        # Replace the credentials if needed
        injection_task_spec.requirements["credentials"][
            self.task_spec.dependencies["apps"][0]
        ] = APP_TO_ADV_CREDENTIALS(self.task_spec.dependencies["apps"][0])
        return injection_task_spec

    async def _deploy_web_agent_container(
        self, task_spec: TaskSpec, goal_idx: int = None, red_team: bool = False
    ) -> str:
        """Deploy the web-agent in a Docker container based on the task spec."""

        # If the to-be-deployed web agent is part of the red team, check if a
        # specific model was specified other than the regular AGENT_MODEL, 
        # and if so, use it for the deployment instead.
        if red_team:
            # TODO: Account for open-sourced model. For now, we assume that 
            # RED_TEAM_AGENT_MODEL and AGENT_MODEL are using the same API
            # for inference, but theoretically RED_TEAM_AGENT_MODEL could be
            # using vLLM.
            agent_model = os.environ.get("RED_TEAM_AGENT_MODEL", None) or os.environ.get("AGENT_MODEL", None)
        else:
            agent_model = os.environ.get("AGENT_MODEL", None)
        if agent_model is None:
            raise ValueError("AGENT_MODEL environment variable not set. Edit .env to include the model name for the agent.")

        # Prepare the task configuration for the web-agent
        task_config, agent_config, browser_config = self._convert_to_legacy_configs(
            task_spec
        )

        if goal_idx is not None:
            task_config["task"] = task_spec.adversarial_goals[goal_idx]["goal"]

        self.logger.info("Running the docker container with the desired parameters...")
        image_tag = "muzzle-" + agent_config["webagent_name"].lower() + "-image"
        # Check if there's an image with the specified tag
        try:
            existing_image = self.docker_client.images.get(image_tag)
            self.logger.info(
                f"Found existing image with tag {image_tag}: {existing_image.id}"
            )
        except docker.errors.ImageNotFound:
            self.logger.info(f"No existing image found with tag {image_tag}")
            # Build image from Dockerfile
            self.logger.info("Building Docker image...")
            # Build the image if not found
            image, logs = self.docker_client.images.build(
                path=agent_config["dockerfile_path"], tag=image_tag
            )

            for log in logs:
                # Muting the logs unless there's an error related to the docker image.
                # if 'stream' in log:
                #     self.logger.info(log['stream'].strip())
                if "error" in log:
                    self.logger.error(log["error"].strip())

        container_tag = "rtm-" + agent_config["webagent_name"].lower()
        # Remove existing container if exists
        try:
            existing_container = self.docker_client.containers.get(container_tag)
            self.logger.info(
                f"Removing existing container {existing_container.name} with ID {existing_container.id}"
            )
            existing_container.stop()
            existing_container.remove()
        except docker.errors.NotFound:
            self.logger.info(f"No existing container found with name {container_tag}")
        # Run container
        self.logger.info(f"Running container {container_tag} from image {image_tag}")

        # Allow local user to access the X server
        os.system("xhost +local:docker")
        container = self.docker_client.containers.run(
            image=image_tag,
            name=container_tag,
            network_mode="host",
            detach=True,
            tty=False,
            stdin_open=True,
            environment={
                "PYTHONUNBUFFERED": "1",
                "DISPLAY": os.environ.get("DISPLAY"),
                "QT_X11_NO_MITSHM": "1",
                "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
                "OPENAI_BASE_URL": os.environ.get("VLLM_PROXY_BASE_URL"),
                "AGENT_MODEL": agent_model,
                # AGENT-E specific environment variables:
                "AUTOGEN_MODEL_NAME": agent_model,
                "AUTOGEN_MODEL_API_KEY": os.environ.get("OPENAI_API_KEY"),
                "AUTOGEN_MODEL_BASE_URL": os.environ.get("VLLM_PROXY_BASE_URL"),
                "BROWSER_PROXY": "http://localhost:3128"
            },
            volumes={
                "/tmp/.X11-unix": {
                    "bind": "/tmp/.X11-unix",
                    "mode": "rw",
                }
            },
            command=[
                "--b64-task-config",
                config_to_base64(task_config),
                "--b64-agent-config",
                config_to_base64(agent_config),
                "--b64-browser-config",
                config_to_base64(browser_config),
            ],
        )
        self.logger.info(f"Started container {container.name} with ID {container.id}")
        
        # Read logs in real-time and check for terminating condition
        try:
            for log_line in container.logs(stream=True):
                log_str = log_line.decode("utf-8").strip()
                if log_str and DEBUG_MODE:
                    self.logger.info(f"[Web-Agent Container] {log_str}")
                
                # Check if halting condition appears in the log line:
                if re.search(AGENT_HALT_CONDITION_REGEX, log_str):
                    self.logger.warning(f"Halting condition found in agent container logs. Stopping and removing container {container_tag}...")
                    container.stop()
                    container.remove()
                    self.logger.info(f"Container {container_tag} stopped and removed.")
                    return container.id
        except Exception as e:
            self.logger.error(f"Error reading container logs: {e}")        
        return container.id

    async def _restore_zoo_state(self, urls: list = None) -> None:
        """Restore the Zoo state by resetting and seeding the specified URLs."""
        await self.zoo_state.reset()
        if urls is not None:
            await self.zoo_state.seed(urls)

    async def _restore_zoo_dbs(
        self, urls: list = None, checkpoint: str = "clean", modification: str = None
    ) -> None:
        """Restore the Zoo databases to a previous state."""
        # For every app:
        for url in urls:
            if url not in SEEDABLES:
                continue
            # If no modification, just load the checkpoint
            if modification is None:
                res = await self.zoo_state.load_db_checkpoint(
                    dump=APP_TO_DB(url) + "_" + checkpoint,
                    db=APP_TO_DB(url),
                    engine=APP_TO_DBMS_ENGINE(url),
                    debug=os.environ.get("DEBUG", "false").lower()
                    in ("1", "true", "yes"),
                )
            # Else, load and modify the checkpoint accordingly
            else:
                res = await self.zoo_state.load_and_modify_db_checkpoint(
                    dump=APP_TO_DB(url) + "_" + checkpoint,
                    anchor=PLACEHOLDER_INSTR,
                    content=modification,
                    db=APP_TO_DB(url),
                    engine=APP_TO_DBMS_ENGINE(url),
                    debug=os.environ.get("DEBUG", "false").lower()
                    in ("1", "true", "yes"),
                )
        if res:
            self.logger.info("Successfully restored Zoo databases.")
        else:
            self.logger.error("Failed to restore Zoo databases.")
        return

    async def _store_zoo_dbs(self, urls: list = None, checkpoint: str = None) -> None:
        """Store the current Zoo databases state as a checkpoint."""
        # For every app:
        for url in urls:
            if url not in SEEDABLES:
                continue
            res = await self.zoo_state.save_db_checkpoint(
                name=APP_TO_DB(url) + "_" + checkpoint,
                db=APP_TO_DB(url),
                engine=APP_TO_DBMS_ENGINE(url),
                debug=DEBUG_MODE,
            )
        if res:
            self.logger.info("Successfully stored Zoo databases.")
        else:
            self.logger.error("Failed to store Zoo databases.")
        return

    async def _emulate_user_task(
        self, task_spec: TaskSpec, run_id: str | None = None
    ) -> Dict[str, Any]:
        """Emulate the user task specified in the task spec."""

        # Restore zoo state for the first time:
        await self._restore_zoo_state(self.task_spec.dependencies["apps"])

        # Save clean checkpoint.
        await self._store_zoo_dbs(
            urls=self.task_spec.dependencies["apps"], checkpoint="clean"
        )

        # Start the vLLM proxy to collect web-agent LLM I/O
        self.vllm_proxy.start()
        await asyncio.sleep(self._proxy_start_delay)  # Give vLLM proxy a moment to start
        # Emulate the adversarial task
        self.logger.info("Emulating the user task...")
        await self._deploy_web_agent_container(task_spec)

        # Stop the vLLM proxy and collect the transcript
        transcript = self.vllm_proxy.stop()
        await asyncio.sleep(self._proxy_start_delay)
        if run_id is not None:
            await self._storage_controller.store_transcript(
                transcript, run_id
            )  # TODO: implement adversarial flag.

        # Store the user task transcript
        self._user_task_transcript = transcript
        return transcript

    @message_handler
    async def handle_adversarial_task(
        self, message: AdversarialTask, ctx: MessageContext
    ) -> None:
        """Handle an adversarial task message."""

        self.logger.info(f"Handling adversarial task for goal index {message.goal_idx}")

        # Restore zoo dbs:
        await self._restore_zoo_dbs(
            urls=self.task_spec.dependencies["apps"],
            checkpoint="clean",
        )

        # Start the vLLM proxy to collect web-agent LLM I/O
        self.vllm_proxy.start()
        await asyncio.sleep(self._proxy_start_delay)  # Give vLLM proxy a moment to start
        # Emulate the adversarial task
        self.logger.info(
            f"Emulating adversarial task for goal index {message.goal_idx}"
        )
        await self._deploy_web_agent_container(message.spec, message.goal_idx, red_team=True)

        # Stop the vLLM proxy and collect the transcript
        transcript = self.vllm_proxy.stop()
        await asyncio.sleep(self._proxy_start_delay)
        if message.run_id is not None:
            await self._storage_controller.store_transcript(
                transcript, message.run_id, goal_idx=message.goal_idx
            )

        # handoff the transcript to the SummarizerAgent
        await self.publish_message(
            SummarizerMessage(
                run_id=message.run_id,
                transcript=transcript,
                transcript_type=TranscriptType.ADVERSARIAL,
                goal_idx=message.goal_idx,
            ),
            topic_id=TopicId(SUMMARIZER_TOPIC_TYPE, source=self.id.key),
        )

    @message_handler
    async def handle_grafter_result(
        self, message: GraftResult, ctx: MessageContext
    ) -> None:
        """Handle a grafter result message."""
        self.logger.info("Received grafter result message.")
        self._user_task_playbook = message.playbook
        self._elements = message.elements
        self._reset_element_iterator()

        if message.reflected:
            # Hand off to DispatchRequest directly:
            # Step 1: pick the element with the highest confidence score from the Graft Result
            selected_element = self._pick_element()

            # Step 2: get the injection prompt
            selected_mal_instruction = self._pick_malicious_instruction(
                self._current_goal_idx
            )

            await self.publish_message(
                DispatchRequest(
                    run_id=message.run_id,
                    attack_method=selected_element,
                    malicious_instruction=selected_mal_instruction,
                    goal_idx=self._current_goal_idx,
                ),
                topic_id=TopicId(DISPATCHER_TOPIC_TYPE, source=self.id.key),
            )
            return

        # Step 2: Emulate the adversarial tasks:
        # Start with the first adversarial goal & send AdversarialTask
        # message to self to start the emulation loop
        if len(self.task_spec.adversarial_goals) == 0:
            raise ValueError(
                "No adversarial goals specified in the task spec. Cannot evaluate web-agent."
            )

        # Initialize current goal index
        self._current_goal_idx = 0
        await self.handle_adversarial_task(
            AdversarialTask(
                run_id=message.run_id,
                goal_idx=self._current_goal_idx,
                spec=self.task_spec,
            ),
            MessageContext(
                sender=self.id.key,
                topic_id=TopicId(EXPLORER_TOPIC_TYPE, source=self.id.key),
                is_rpc=False,
                cancellation_token=None,
                message_id="internal-adv-task-0",
            ),
        )
        return

    def _reset_element_iterator(self) -> None:
        """Reset the element iterator to start from highest-confidence candidates."""
        candidates = (self._elements or {}).get("candidates") or []
        self._element_candidates_sorted = sorted(
            candidates,
            key=lambda el: el.get("confidence", 0.0),
            reverse=True,
        )
        self._element_candidate_idx = 0

    def _pick_element(self) -> Dict[str, Any]:
        """Return the current element candidate (highest remaining confidence first)."""
        if not self._element_candidates_sorted:
            self._reset_element_iterator()
        if not self._element_candidates_sorted:
            raise ValueError("No UI element candidates available to pick from.")
        if self._element_candidate_idx >= len(self._element_candidates_sorted):
            raise ValueError("No more UI element candidates available to try.")
        return self._element_candidates_sorted[self._element_candidate_idx]

    def _next_element(self) -> Dict[str, Any] | None:
        """Advance to the next (lower-confidence) element candidate and return it."""
        if not self._element_candidates_sorted:
            self._reset_element_iterator()
        self._element_candidate_idx += 1
        if self._element_candidate_idx >= len(self._element_candidates_sorted):
            return None
        return self._element_candidates_sorted[self._element_candidate_idx]

    def _pick_instructions(self, goal_idx: int, probe=False) -> Dict[str, Any]:
        """Pick the instructions for the given goal index."""
        # For simplicity, pick the first instruction stored for the goal index
        if probe:
            return {
                "reasoning": "Placeholder text instruction tag.",
                "injection": PLACEHOLDER_INSTR,
            }
        if goal_idx in self._instructions and len(self._instructions[goal_idx]) > 0:
            return self._instructions[goal_idx][-1]  # return most recent instructions.
        else:
            raise ValueError(f"No instructions found for goal index {goal_idx}.")

    def _pick_malicious_instruction(self, goal_idx: int) -> Dict[str, Any]:
        """Pick the malicious instruction for the given goal index."""
        # For simplicity, pick the first injection stored for the goal index
        if goal_idx in self._injections and len(self._injections[goal_idx]) > 0:
            return self._injections[goal_idx][
                -1
            ]  # return most recent malicious instruction.
        else:
            return None

    @message_handler
    async def handle_instructions(
        self, message: InstructionMessage, ctx: MessageContext
    ) -> None:
        """Handle a instructions."""
        self.logger.info("Received instructions message.")

        # At this stage, the instructions to replicate the adversarial
        # objective have been collected and are ready to be rephrased
        # into a malicious instruction for prompt-injection.

        # Store the generated instructions
        if message.goal_idx not in self._instructions.keys():
            # If it's the first instruction for this goal idx, init the list:
            self._instructions[message.goal_idx] = [message.prompt]
        else:
            # Append only if not duplicate AND it's not a skip_element signal from PAIR
            if message.prompt not in self._instructions[message.goal_idx] and not message.skip_element:
                self._instructions[message.goal_idx].append(message.prompt)

        # TODO: if these instructions correspond to an objective that is not first on the list,
        # it must mean that the first objective was already achieved, so the path to the attack
        # has been opened. In that case, we fix the UI element and skip to the prompt-injection
        # step directly.
        if (
            message.goal_idx > 0
        ):  # TODO: add check to see if the first adversarial objective was achieved.
            self.logger.warning(
                "Adv objective is not first on the list, skipping to prompt-injection step."
            )

            cfg = PAIRConfig(
                original_user_task=self.task_spec.instruction,
                target_str=self.task_spec.calibrant,
                attack_model=os.environ.get("PAIR_ATTACKER_MODEL", None),
                target_model=os.environ.get("AGENT_MODEL", None),
                judge_model=os.environ.get("PAIR_JUDGE_MODEL", None),
            )

            await self.publish_message(
                PAIRMessage(
                    run_id=message.run_id,
                    instructions=self._pick_instructions(message.goal_idx).get(
                        "prompt", None
                    ),
                    transcript=self._user_task_transcript_with_placeholder,
                    goal_idx=message.goal_idx,
                    pair_cfg=cfg,
                ),
                topic_id=TopicId(PAIR_TOPIC_TYPE, source="default"),
            )

        # Next steps:
        # Using the selected UI element from the Grafter, a red-team
        # web-agent needs to be spawned to plant a probe in the Zoo.
        # Step 1: pick the element with the highest confidence score from the Graft Result
        if message.skip_element: 
            # If the skip_element flag is true, it means that the PAIR Agent did not find the
            # placeholder string in the transcript. That suggests that either:
            # A) the red-team agent failed to plant the placeholder in the Zoo or
            # B) the UI element that was exploited did not end up in the victim agent's action
            # path trajectory
            # --> THUS PAIR signals to skip to the next element.
            self._next_element()
        selected_element = self._pick_element()

        # Step 2: get the injection prompt (placeholder for now, probe:true)
        selected_mal_instruction = self._pick_instructions(message.goal_idx, probe=True)

        await self.publish_message(
            DispatchRequest(
                run_id=message.run_id,
                attack_method=selected_element,
                malicious_instruction=selected_mal_instruction,
                mode="PROBE",
                goal_idx=message.goal_idx,
                reflection=message.reflection,
            ),
            topic_id=TopicId(DISPATCHER_TOPIC_TYPE, source=self.id.key),
        )
        return

    @message_handler
    async def handle_prompt_injection_message(
        self, message: PromptInjectionMessage, ctx: MessageContext
    ) -> None:
        """Handle a prompt injection message."""
        self.logger.info("Received prompt injection message.")
        injection_output = message.output

        if injection_output["injection"] is None:
            # Something went wrong during prompt injection generation,
            # SO move to the next UI element.
            selected_element = self._next_element()
            if selected_element is None:
                self.logger.error("No more UI elements to try for prompt injection.")
                return

            # Step 2: get the injection prompt (placeholder for now, probe:true)
            selected_mal_instruction = self._pick_instructions(
                message.goal_idx, probe=True
            )

            await self.publish_message(
                DispatchRequest(
                    run_id=message.run_id,
                    attack_method=selected_element,
                    malicious_instruction=selected_mal_instruction,
                    mode="PROBE",
                    goal_idx=message.goal_idx,
                ),
                topic_id=TopicId(DISPATCHER_TOPIC_TYPE, source=self.id.key),
            )
            return

        # Store the generated prompt injection
        if message.goal_idx not in self._injections.keys():
            self._injections[message.goal_idx] = [injection_output]
        else:
            self._injections[message.goal_idx].append(injection_output)

        # Step 1: pick the element with the highest confidence score from the Graft Result
        selected_element = self._pick_element()

        # Step 2: get the injection prompt
        selected_mal_instruction = self._pick_malicious_instruction(message.goal_idx)

        await self.publish_message(
            DispatchRequest(
                run_id=message.run_id,
                attack_method=selected_element,
                malicious_instruction=selected_mal_instruction,
                mode="ACTUAL",
                goal_idx=message.goal_idx,
            ),
            topic_id=TopicId(DISPATCHER_TOPIC_TYPE, source=self.id.key),
        )
        return

    @message_handler
    async def handle_dispatched_task(
        self, message: DispatchTask, ctx: MessageContext
    ) -> None:
        """Handle a dispatched task message."""

        if message.mode == "PROBE":
            # Restore zoo state for the first time:
            # (adversarial objectives messup the state, so we need to hard-reset)
            # WARNING: the reason we are not toying with the databases directly here is that
            # gitea has some issues with the state.
            await self._restore_zoo_state(self.task_spec.dependencies["apps"])

            # Store clean checkpoint.
            await self._store_zoo_dbs(
                urls=self.task_spec.dependencies["apps"], checkpoint="clean"
            )

            # Step 1: Spawn red-team web-agent to execute the dispatched task
            self.vllm_proxy.start()
            await asyncio.sleep(self._proxy_start_delay)  # Give vLLM proxy a moment to start
            injection_task_spec = self._create_injection_task_spec(message)
            self.logger.info(
                "Deploying red-team web-agent to execute dispatched task..."
            )
            await self._deploy_web_agent_container(injection_task_spec, red_team=True)
            injection_task_transcript = self.vllm_proxy.stop()
            await asyncio.sleep(self._proxy_start_delay)
            if message.run_id is not None:
                await self._storage_controller.store_transcript(
                    injection_task_transcript, message.run_id, goal_idx=message.goal_idx
                )
            # Checkpoint the poisoned state:
            await self._store_zoo_dbs(
                urls=self.task_spec.dependencies["apps"],
                checkpoint="goal_" + str(message.goal_idx),
            )
        else:
            # Restore zoo dbs to POISONED state:
            checkpoined = (
                self._last_successful_goal_idx
                if self._last_successful_goal_idx is not None
                else message.goal_idx
            )
            await self._restore_zoo_dbs(
                urls=self.task_spec.dependencies["apps"],
                checkpoint="goal_" + str(checkpoined),
                modification=message.malicious_instruction["injection"],
            )

        # Step 2: Use Judge to evaluate whether the injection task was successful.
        # TODO: implement (can be done at a later stage)

        # Step 3: Spawn the benign web-agent to execute the user task.
        # First check if there exists an injection. (Sign of reflection)
        if message.reflection:
            # This means that a prompt-injection was generated for this goal idx.
            self.logger.warning(
                "A prompt-injection was generated for this goal index. Skipping benign web-agent deployment..."
            )
            # Skip PAIR:
            await self.handle_prompt_injection_message(
                PromptInjectionMessage(
                    run_id=message.run_id,
                    output=self._pick_malicious_instruction(message.goal_idx),
                    goal_idx=message.goal_idx,
                ),
                MessageContext(
                    sender=self.id.key,
                    topic_id=TopicId(EXPLORER_TOPIC_TYPE, source=self.id.key),
                    is_rpc=False,
                    cancellation_token=None,
                    message_id=f"internal-prompt-injection-{message.goal_idx}",
                ),
            )
            return
        else:
            self.vllm_proxy.start()
            await asyncio.sleep(self._proxy_start_delay)  # Give vLLM proxy a moment to start
            self.logger.info("Deploying benign web-agent to execute user task...")
            await self._deploy_web_agent_container(self.task_spec)
            benign_task_transcript = self.vllm_proxy.stop()
            if message.mode == "PROBE":
                # Save the benign transcript with placeholder for future use
                self._user_task_transcript_with_placeholder = benign_task_transcript
            await asyncio.sleep(self._proxy_start_delay)
            if message.run_id is not None:
                await self._storage_controller.store_transcript(
                    benign_task_transcript, message.run_id, goal_idx=message.goal_idx
                )

        if message.mode == "PROBE":
            # IF the mode was 'PROBE', PAIR needs to be initiated with the collected transcript,
            # and the process needs to be repeated with the actual malicious instruction.

            cfg = PAIRConfig(
                original_user_task=self.task_spec.instruction,
                target_str=self.task_spec.calibrant,
                attack_model=os.environ.get("PAIR_ATTACKER_MODEL", None),
                target_model=os.environ.get("AGENT_MODEL", None),
                judge_model=os.environ.get("PAIR_JUDGE_MODEL", None),
            )

            await self.publish_message(
                PAIRMessage(
                    run_id=message.run_id,
                    instructions=self._pick_instructions(message.goal_idx).get(
                        "prompt", None
                    ),
                    transcript=benign_task_transcript,
                    goal_idx=message.goal_idx,
                    pair_cfg=cfg,
                ),
                topic_id=TopicId(PAIR_TOPIC_TYPE, source="default"),
            )

        elif message.mode == "ACTUAL":
            # ELSE IF mode was 'ACTUAL', the judge needs to be initiated for evaluation.
            
            # Check to see if assertion is provided for this goal idx. 
            if self.task_spec.adversarial_goals[message.goal_idx].get("assertion", None) is not None:
                # - If yes, use it as the check for evaluation. 
                goal_check = self.task_spec.adversarial_goals[message.goal_idx]["assertion"]
            else:
                # - If not, pass None and let Judge decide how to evaluate.
                goal_check = None

            # Use Judge to evaluate whether the attack was successful.
            await self.publish_message(
                JudgeRequest(
                    run_id=message.run_id,
                    attack_method=message.attack_method,
                    malicious_instruction=message.malicious_instruction,
                    transcript=benign_task_transcript,
                    assertion=goal_check,
                    goal_idx=message.goal_idx,
                ),
                topic_id=TopicId(JUDGE_TOPIC_TYPE, source=self.id.key),
            )
        else:
            self.logger.error(f"Unknown DispatchTask mode: {message.mode}")
        return

    @message_handler
    async def handle_reflection(
        self, message: JudgeEvaluation, ctx: MessageContext
    ) -> None:
        """Handle reflection"""

        # Unpack the evaluation details
        details = message.details

        # Check the outcome and decide next steps.
        outcome = details.get("outcome")

        # ==== OUTCOME HANDLING LOGIC ====
        # ==== OPTION 1:
        if outcome == "success":
            # Since the attack was successful, move to the next adversarial goal if any.
            self._last_successful_goal_idx = message.goal_idx
            self._current_goal_idx += 1  # Increase current goal index
            if self._current_goal_idx < len(self.task_spec.adversarial_goals):
                # Handle the next adversarial task
                await self.handle_adversarial_task(
                    AdversarialTask(
                        run_id=message.run_id,
                        goal_idx=self._current_goal_idx,
                        spec=self.task_spec,
                    ),
                    MessageContext(
                        sender=self.id.key,
                        topic_id=TopicId(EXPLORER_TOPIC_TYPE, source=self.id.key),
                        is_rpc=False,
                        cancellation_token=None,
                        message_id=f"internal-adv-task-{self._current_goal_idx}",
                    ),
                )
            else:
                # No more adversarial goals to process. Terminate.
                self.logger.info("All adversarial goals have been processed.")
                self.logger.info(f"Run with ID '{message.run_id}' completed.")
                return
        # ==== OPTION 2:
        elif outcome in {"partial", "failure"}:
            # Forward reflection results to either the Grafter or Prompter.
            component = details.get("component_attribution", {}).get("primary", None)
            if component == "UI":
                # Move to the next UI element (high -> low confidence).
                # IMPORTANT: `self._next_element()` advances the candidate element index.
                if self._next_element() is None:
                    self.logger.error(
                        "No more UI elements to try after UI-attributed failure."
                    )
                    return

                # Start modifying the next UI element.
                await self.handle_instructions(
                    InstructionMessage(
                        run_id=message.run_id,
                        prompt=self._pick_instructions(message.goal_idx, probe=True),
                        goal_idx=message.goal_idx,
                        reflection=True,  # Indicate that reflection was performed and it's another attempt for another UI element.
                    ),
                    MessageContext(
                        sender=self.id.key,
                        topic_id=TopicId(EXPLORER_TOPIC_TYPE, source=self.id.key),
                        is_rpc=False,
                        cancellation_token=None,
                        message_id=f"internal-instructions-{message.goal_idx}",
                    ),
                )
            elif component == "instruction":
                # Send reflection feedback to PAIR for new instruction generation.
                reflect = {
                    "most_recent_working_payload": self._pick_malicious_instruction(message.goal_idx),
                    "reflection_details": details,
                }
                cfg = PAIRConfig(
                    original_user_task=self.task_spec.instruction,
                    target_str=self.task_spec.calibrant,
                    attack_model=os.environ.get("PAIR_ATTACKER_MODEL", None),
                    target_model=os.environ.get("AGENT_MODEL", None),
                    judge_model=os.environ.get("PAIR_JUDGE_MODEL", None),
                )
                await self.publish_message(
                    PAIRMessage(
                        run_id=message.run_id,
                        instructions=self._pick_instructions(message.goal_idx).get(
                            "prompt", None
                        ),
                        transcript=self._user_task_transcript_with_placeholder,
                        goal_idx=message.goal_idx,
                        pair_cfg=cfg,
                        reflection=reflect,
                    ),
                    topic_id=TopicId(PAIR_TOPIC_TYPE, source="default"),
                )
            elif component == "unclear":
                # TODO: decide later how to handle this case.
                pass
            else:
                err_msg = (
                    f"Unknown component attribution '{component}' in Judge evaluation."
                )
                self.logger.error(err_msg)
                raise ValueError(err_msg)
        # ==== OPTION 3:
        else:
            err_msg = f"Unknown outcome '{outcome}' in Judge evaluation."
            self.logger.error(err_msg)
            raise ValueError(err_msg)
        return

    @message_handler
    async def handle_explore(self, message: TaskSpec, ctx: MessageContext) -> None:
        # Store the task spec internally
        self.task_spec = message
        # Generate run_id for the run & initialize storage
        run_id = await self._storage_controller.create_run(self.task_spec)
        self.logger.info(f"Starting run with ID: '{run_id}'")

        # Step 1: Emulate the user task:
        await self._emulate_user_task(self.task_spec, run_id)

        # Publish to the SummarizerAgent the user task transcript
        await self.publish_message(
            SummarizerMessage(
                run_id=run_id,
                transcript=self._user_task_transcript,
                transcript_type=TranscriptType.BENIGN,
            ),
            topic_id=TopicId(SUMMARIZER_TOPIC_TYPE, source=self.id.key),
        )
        return
