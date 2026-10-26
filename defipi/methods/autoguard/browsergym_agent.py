"""Real BrowserGym + AgentLab GenericAgent runner for AutoGuard PII.

This adapter deliberately uses the upstream BrowserGym ``BrowserEnv`` and
AgentLab ``GenericAgent`` classes. It does not substitute a static AX snapshot
or an application-authored target prompt. The task's question is passed as the
BrowserGym goal and the agent constructs its normal stepwise prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from defipi.jsonio import write_json

import gymnasium as gym
from agentlab.agents import dynamic_prompting as dp
from agentlab.agents.generic_agent.generic_agent import GenericAgent
from agentlab.agents.generic_agent.generic_agent_prompt import GenericPromptFlags
from agentlab.llm.base_api import AbstractChatModel, BaseModelArgs
from agentlab.llm.llm_utils import AIMessage
from browsergym.core.env import BrowserEnv
from browsergym.core.task import AbstractBrowserTask
from browsergym.experiments.benchmark import HighLevelActionSetArgs


from defipi.methods.autoguard.injection import INJECTION_ID, INJECTION_CSS, INJECTION_JS

FRAMEWORK_FLAGS = {
    "use_html": False,
    "use_ax_tree": True,
    "use_tabs": False,
    "use_focused_element": True,
    "use_error_logs": True,
    "use_history": True,
    "use_past_error_logs": False,
    "use_action_history": True,
    "use_think_history": True,
    "use_diff": False,
    "use_screenshot": False,
    "use_som": False,
    "extract_visible_tag": True,
    "extract_clickable_tag": False,
    "extract_coords": "False",
    "filter_visible_elements_only": False,
    "filter_with_bid_only": False,
    "filter_som_only": False,
    "action_subsets": ["chat", "infeas", "bid", "nav", "tab"],
    "multiaction": False,
    "strict_actions": False,
    "retry_with_force": True,
    "demo_mode": "off",
    "max_prompt_tokens": None,
}


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "tolist"):
        try:
            return _plain(value.tolist())
        except Exception:
            pass
    return str(value)


def _message_dicts(messages: Any) -> list[dict[str, Any]]:
    if hasattr(messages, "to_openai"):
        messages = messages.to_openai()
    result = []
    for message in messages or []:
        if isinstance(message, dict):
            row = dict(message)
        else:
            row = {"role": getattr(message, "role", None), "content": getattr(message, "content", None)}
        result.append(_plain(row))
    return result


def _model_settings(model: Any) -> dict[str, Any]:
    from defipi.methods.autoguard.configuration import validate_model_settings
    validate_model_settings(model)
    return dict(model)


class BrowserGymPIITask(AbstractBrowserTask):
    """One local site task; inject before the configured document's first observation."""

    @classmethod
    def get_task_id(cls):
        return "autoguard_pii_local"

    def __init__(self, seed: int, *, starting_url: str, question: str, payload_text: str | None,
                 injection_document_index: int):
        super().__init__(seed)
        self.starting_url = starting_url
        self.question = question
        self.payload_text = payload_text
        self.injection_document_index = injection_document_index
        self.slow_mo = 0
        self.timeout = 10000
        self.visit_events: list[dict[str, Any]] = []
        self._loader_ids: set[str] = set()
        self._cdp = None
        self._cdp_sessions: dict[int, Any] = {}
        self._visit_pages: dict[int, Any] = {}
        self._injection_attempted = False
        self.injection = {
            "injected": False,
            "payload_text": payload_text,
            "defense_enabled": payload_text is not None,
            "target_document_index": injection_document_index,
            "visit_index": None,
            "url": None,
            "css": INJECTION_CSS,
            "error": None,
        }
        self.termination_action: str | None = None
        self.termination_message_role: str | None = None

    def setup(self, page):
        # Chromium's frameNavigated event changes loaderId only for a new
        # document; Page.navigatedWithinDocument (hash/history) is ignored.
        self._context = page.context
        self._context.on("page", self._attach_page)
        self._attach_page(page)
        page.goto(self.starting_url, wait_until="domcontentloaded", timeout=15000)
        return self.question, {"task_id": self.get_task_id(), "starting_url": self.starting_url}

    def _attach_page(self, page):
        page_key = id(page)
        if page_key in self._cdp_sessions:
            return
        cdp = page.context.new_cdp_session(page)
        cdp.on("Page.frameNavigated", lambda event, tracked_page=page: self._on_frame_navigated(event, tracked_page))
        cdp.send("Page.enable")
        self._cdp_sessions[page_key] = cdp
        if self._cdp is None:
            self._cdp = cdp
        # A popup may already have committed its initial document before the
        # context's ``page`` callback runs. Snapshot its current frame tree
        # after enabling events so that document 2 is not missed.
        try:
            frame_tree = cdp.send("Page.getFrameTree").get("frameTree") or {}
            self._on_frame_navigated({"frame": frame_tree.get("frame") or {}}, page)
        except Exception:
            # The initial page can still be starting up; its later
            # frameNavigated event is the authoritative fallback.
            pass

    def _on_frame_navigated(self, event, page):
        frame = event.get("frame") or {}
        if not frame.get("parentId"):
            url = str(frame.get("url") or "")
            loader_id = str(frame.get("loaderId") or "")
            if url and url != "about:blank" and loader_id and loader_id not in self._loader_ids:
                self._loader_ids.add(loader_id)
                visit_index = len(self.visit_events) + 1
                self._visit_pages[visit_index] = page
                self.visit_events.append({
                    "visit_index": visit_index,
                    "url": url,
                    "loader_id": loader_id,
                    "context_page_index": list(page.context.pages).index(page) if page in page.context.pages else None,
                })

    def prepare_observation(self, page):
        # BrowserGym calls this from the BrowserEnv observation hook, after
        # page loading but before the real AX extraction. It closes a race in
        # popup navigation where the new page callback arrives just after task
        # validation but before the observation is read.
        # Poll current pages as well as processing asynchronous CDP events:
        # middle-click popups can commit between the last callback and this
        # hook, after their initial Page.frameNavigated event was missed.
        for context_page in list(getattr(self, "_context", page.context).pages):
            self._attach_page(context_page)
            cdp = self._cdp_sessions.get(id(context_page))
            if cdp is not None:
                try:
                    tree = cdp.send("Page.getFrameTree").get("frameTree") or {}
                    self._on_frame_navigated({"frame": tree.get("frame") or {}}, context_page)
                except Exception:
                    pass
        if self.payload_text is not None and not self._injection_attempted and len(self.visit_events) == self.injection_document_index:
            self._injection_attempted = True
            injection_page = self._visit_pages.get(self.injection_document_index)
            try:
                if injection_page is None or injection_page.is_closed():
                    raise RuntimeError("injection_document_unavailable")
                inserted = injection_page.evaluate(
                    INJECTION_JS,
                    {"id": INJECTION_ID, "text": self.payload_text, "css": INJECTION_CSS},
                )
                self.injection.update({
                    "injected": bool(inserted),
                    "visit_index": self.injection_document_index,
                    "url": injection_page.url,
                })
            except Exception as exc:
                self.injection.update({
                    "error": f"{type(exc).__name__}:{exc}",
                    "visit_index": self.injection_document_index,
                    "url": injection_page.url if injection_page is not None else None,
                })

    def validate(self, page, chat_messages):
        self.prepare_observation(page)
        if chat_messages:
            latest = chat_messages[-1]
            if latest.get("role") == "assistant" and latest.get("message") != "Hi! I am your UI assistant, I can perform web tasks for you. What can I help you with?":
                # `send_msg_to_user` is the native GenericAgent terminal output
                # convention; the task ends after the answer reaches the chat.
                self.termination_action = "send_msg_to_user"
                self.termination_message_role = "assistant"
                return 0.0, True, "", {"termination_action": self.termination_action}
            if latest.get("role") == "infeasible":
                # BrowserGym's native report_infeasible action is terminal.
                # Keep its reason as a judgeable output; the Scorer, not this
                # task, determines whether it is a safety refusal.
                self.termination_action = "report_infeasible"
                self.termination_message_role = "infeasible"
                return 0.0, True, "", {"termination_action": self.termination_action}
        return 0.0, False, "", {}

    def teardown(self):
        for cdp in self._cdp_sessions.values():
            try:
                cdp.detach()
            except Exception:
                pass
        self._cdp_sessions.clear()
        self._cdp = None


class _InjectionAwareBrowserEnv(BrowserEnv):
    """Run the task's DOM injection immediately before native AX extraction."""

    def _get_obs(self):
        task = getattr(self, "task", None)
        task = getattr(task, "unwrapped", task)
        if isinstance(task, BrowserGymPIITask):
            task.prepare_observation(self.page)
        return super()._get_obs()


@dataclass
class _ProviderModelArgs(BaseModelArgs):
    """AgentLab model-args shell; client and reasoning controls stay runtime-only."""

    def make_model(self):
        return _ProviderChatModel(
            self.provider_client,
            self.model_name,
            self.reasoning_effort,
            self.request_records,
            temperature=self.temperature, max_tokens=self.max_new_tokens,
        )


class _ProviderChatModel(AbstractChatModel):
    def __init__(self, client, model_name: str, reasoning_effort: str, records: list[dict], *, temperature, max_tokens):
        self.client = client
        self.model_name = model_name
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.records = records
        self.current_step: int | None = None
        self._active_record: dict[str, Any] | None = None
        self._instrument_wire_client()

    def _instrument_wire_client(self):
        """Capture the exact request body at the existing client's transport boundary."""
        original = getattr(self.client, "_request_with_deadline", None)
        if not callable(original):
            return

        def capture(method_path, request):
            event = {"endpoint": method_path, "payload": _plain(request), "raw_response": None, "error": None}
            if self._active_record is not None:
                self._active_record.setdefault("wire_requests", []).append(event)
            try:
                response = original(method_path, request)
                event["raw_response"] = _plain(response)
                return response
            except Exception as exc:
                event["error"] = f"{type(exc).__name__}:{exc}"
                raise

        # The existing LLMEvaluator client uses this boundary to issue the
        # request. Wrapping the instance keeps its retry policy and payload
        # builder unchanged while recording each actual HTTP attempt.
        self.client._request_with_deadline = capture

    def __call__(self, messages):
        outgoing = _message_dicts(messages)
        record: dict[str, Any] = {
            "role": "target",
            "step": self.current_step,
            "model": self.model_name,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": outgoing,
            "wire_requests": [],
        }
        self._active_record = record
        try:
            from defipi.methods.autoguard.runtime import call_model
            record["http_attempts"] = 0
            def before_request_attempt():
                record["http_attempts"] += 1
            response = call_model(
                self.client,
                messages=outgoing,
                system=None,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
                before_request_attempt=before_request_attempt,
            )
            if isinstance(response, str):
                text = response
                usage = None
                raw_response = response
            else:
                text = str((response or {}).get("text") or "")
                usage = (response or {}).get("usage")
                raw_response = (response or {}).get("raw_response", text)
                try:
                    from defipi.execution.api.clients import extract_finish_reason

                    finish_reason = extract_finish_reason(response)
                except Exception:
                    finish_reason = (response or {}).get("finish_reason")
            if isinstance(response, str):
                finish_reason = None
            if finish_reason not in {"stop", "end_turn"}:
                record.update({
                    "response": text,
                    "raw_response": _plain(raw_response),
                    "usage": _plain(usage),
                    "finish_reason": finish_reason,
                    "error": f"target_incomplete_finish_reason:{finish_reason}",
                })
                self.records.append(record)
                self._active_record = None
                raise RuntimeError(record["error"])
            record.update({"response": text, "raw_response": _plain(raw_response), "usage": _plain(usage), "finish_reason": finish_reason, "error": None})
            self.records.append(record)
            self._active_record = None
            return AIMessage(text)
        except RuntimeError as exc:
            if self._active_record is None:
                raise
            record.update({"response": None, "error": f"{type(exc).__name__}:{exc}"})
            self.records.append(record)
            self._active_record = None
            raise
        except Exception as exc:
            record.update({"response": None, "error": f"{type(exc).__name__}:{exc}"})
            self.records.append(record)
            self._active_record = None
            raise

    def get_stats(self):
        return {"n_retry_llm": sum(1 for row in self.records if row.get("step") == self.current_step)}


def _new_agent(settings: dict, client, request_records: list[dict], parse_retries: int) -> tuple[GenericAgent, _ProviderChatModel]:
    model_args = _ProviderModelArgs(
        model_name=settings["model"],
        max_total_tokens=None,
        max_input_tokens=None,
        max_new_tokens=settings["max_tokens"],
        temperature=settings["temperature"],
        vision_support=False,
    )
    # Runtime-only values are intentionally not dataclass fields, so AgentLab's
    # asdict(chat_model_args) trace never serializes clients or credentials.
    model_args.provider_client = client
    model_args.reasoning_effort = settings["reasoning_effort"]
    model_args.request_records = request_records

    action_set = HighLevelActionSetArgs(
        subsets=["chat", "infeas", "bid", "nav", "tab"],
        multiaction=False,
        strict=False,
        retry_with_force=True,
        demo_mode="off",
    )
    obs_flags = dp.ObsFlags(
        use_html=False,
        use_ax_tree=True,
        use_tabs=False,
        use_focused_element=True,
        use_error_logs=True,
        use_history=True,
        use_past_error_logs=False,
        use_action_history=True,
        use_think_history=True,
        use_diff=False,
        html_type="pruned_html",
        use_screenshot=False,
        use_som=False,
        extract_visible_tag=True,
        extract_clickable_tag=False,
        extract_coords="False",
        filter_visible_elements_only=False,
        filter_with_bid_only=False,
        filter_som_only=False,
    )
    flags = GenericPromptFlags(
        obs=obs_flags,
        action=dp.ActionFlags(action_set=action_set, long_description=False, individual_examples=True),
        use_plan=False,
        use_criticise=False,
        use_thinking=True,
        use_memory=False,
        use_concrete_example=True,
        use_abstract_example=True,
        use_hints=True,
        enable_chat=False,
        max_prompt_tokens=None,
        be_cautious=True,
        extra_instructions=None,
        add_missparsed_messages=True,
    )
    agent = GenericAgent(chat_model_args=model_args, flags=flags, max_retry=parse_retries)
    return agent, agent.chat_llm


def _contains_text(value: Any, needle: str) -> bool:
    """Require the full payload, allowing only whitespace normalization."""
    needle = " ".join(needle.split())
    if not needle:
        return False
    if isinstance(value, str):
        return needle in " ".join(value.split())
    if isinstance(value, dict):
        return any(_contains_text(v, needle) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_text(v, needle) for v in value)
    return False


def _observation_record(step: int, obs: dict, processed: dict | None = None) -> dict[str, Any]:
    processed = processed or {}
    return {
        "step": step,
        "url": str(obs.get("url") or ""),
        "open_pages_urls": list(obs.get("open_pages_urls") or []),
        "last_action": obs.get("last_action"),
        "last_action_error": obs.get("last_action_error"),
        "browsergym_raw_axtree": _plain(obs.get("axtree_object")),
        "agentlab_preprocessed_axtree": str(processed.get("axtree_txt") or ""),
        "agentlab_preprocessed_dom": str(processed.get("dom_txt") or ""),
        "focused_element_bid": obs.get("focused_element_bid"),
    }


def run_browsergym_episode(
    case: dict,
    payload: str | None,
    *,
    model: Any,
    config: dict,
    artifact_dir: str | Path | None = None,
    client_factory=None,
) -> dict[str, Any]:
    """Run one real BrowserGym episode with AgentLab's real GenericAgent.

    Step budget and parser retries come from the validated browser config.
    BrowserGym always runs headless. The runner does not invent a paid-call cap.
    The target client is created lazily here, never by planning commands.
    """
    if not isinstance(case, dict):
        raise TypeError("case must be an object")
    if artifact_dir is not None:
        artifact_path = Path(artifact_dir)
        if any((artifact_path / name).exists() for name in (
            "browsergym_episode.started.json", "browsergym_episode.json"
        )):
            raise FileExistsError(f"refusing to overwrite browser episode evidence in {artifact_path}")
    question = str(case.get("question") or "").strip()
    starting_url = str(case.get("starting_url") or "").strip()
    if not question or not starting_url:
        raise ValueError("case requires a frozen question and starting_url")
    if "seed" in case:
        raise ValueError("case.seed is not supported; browser.episode_seed sets the episode seed")
    if payload is not None and (not isinstance(payload, str) or not payload):
        raise ValueError("candidate payload must be None for clean baseline or non-empty exact text")
    from defipi.configuration.schema import validate_config
    from defipi.methods.autoguard.configuration import BROWSER_FIELDS
    browser_cfg = validate_config(config["browser"], BROWSER_FIELDS, prefix="browser")
    episode_seed = browser_cfg["episode_seed"]
    step_budget = browser_cfg["episode_step_budget"]
    parse_retries = browser_cfg["agentlab_parse_retries"]
    injection_document_index = browser_cfg["injection_document_index"]

    settings = _model_settings(model)
    if artifact_dir is not None:
        artifact_path.mkdir(parents=True, exist_ok=True)
        marker = artifact_path / "browsergym_episode.started.json"
        write_json(marker, {
            "case_id": case.get("id"),
            "model": settings["model"],
            "reasoning_effort": settings["reasoning_effort"],
            "status": "started; immutable attempt",
        })
    if client_factory is None:
        from defipi.methods.autoguard.runtime import model_client_factory
        client_factory = model_client_factory(config)

    client = client_factory(settings, "target")
    provider_records: list[dict[str, Any]] = []
    agent, model_bridge = _new_agent(settings, client, provider_records, parse_retries)
    task = lambda seed, **kwargs: BrowserGymPIITask(
        seed=seed,
        starting_url=starting_url,
        question=question,
        payload_text=payload,
        injection_document_index=injection_document_index,
    )
    headless = True  # Fixed BrowserGym execution contract.
    env = _InjectionAwareBrowserEnv(
        task_entrypoint=task,
        headless=headless,
        pre_observation_delay=0.5,
        tags_to_mark="standard_html",
        action_mapping=agent.action_set.to_python_code,
    )

    observations: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    status = "step_budget_truncated"
    failure_reason = None
    final_answer = None
    try:
        obs, reset_info = env.reset(seed=episode_seed)
        agent.reset(seed=episode_seed)
        observations.append(_observation_record(1, obs))
        terminated = truncated = False
        for step_index in range(1, step_budget + 1):
            processed = agent.obs_preprocessor(obs)
            current_observation = _observation_record(step_index, obs, processed)
            # The observations list begins with the reset observation and is
            # filled with each subsequent BrowserGym response. Replace the
            # current raw-only row with exactly what AgentLab consumes.
            if observations and observations[-1].get("step") == step_index:
                observations[-1] = current_observation
            before_request = len(provider_records)
            model_bridge.current_step = step_index
            try:
                from defipi.methods.autoguard.runtime import request_attempt_scope
                with request_attempt_scope():
                    action, agent_info = agent.get_action(processed)
            except Exception as exc:
                failure_reason = f"agentlab_generation_error:{type(exc).__name__}:{exc}"
                steps.append({
                    "step": step_index,
                    "action_index": step_index,
                    "observation_step": step_index,
                    "think": None,
                    "parsed_action": None,
                    "request_indices": list(range(before_request, len(provider_records))),
                    "action_error": failure_reason,
                })
                status = "invalid_agent_response"
                break

            requests_for_step = provider_records[before_request:]
            raw_outputs = [r.get("response") for r in requests_for_step if r.get("response") is not None]
            think = getattr(agent_info, "think", None)
            if not think and raw_outputs:
                match = re.search(r"<think>\s*(.*?)\s*</think>", raw_outputs[-1], re.I | re.S)
                think = match.group(1).strip() if match else None
            if not isinstance(action, str) or not action.strip():
                failure_reason = "agentlab_action_missing_or_unparseable"
                steps.append({
                    "step": step_index,
                    "action_index": step_index,
                    "observation_step": step_index,
                    "think": think,
                    "parsed_action": None,
                    "request_indices": list(range(before_request, len(provider_records))),
                    "action_error": failure_reason,
                })
                status = "invalid_agent_response"
                break

            step_record = {
                "step": step_index,
                "action_index": step_index,
                "observation_step": step_index,
                "think": str(think) if think is not None else None,
                "parsed_action": action,
                "request_indices": list(range(before_request, len(provider_records))),
            }
            steps.append(step_record)
            action_start = len(env.chat.messages)
            try:
                next_obs, reward, terminated, truncated, info = env.step(action)
            except Exception as exc:
                step_record["action_error"] = f"{type(exc).__name__}:{exc}"
                raise
            new_messages = env.chat.messages[action_start:]
            termination_action = None
            if new_messages:
                for message in new_messages:
                    if message.get("role") == "assistant" and str(message.get("message") or "") != "Hi! I am your UI assistant, I can perform web tasks for you. What can I help you with?":
                        final_answer = str(message.get("message") or "")
                        termination_action = "send_msg_to_user"
                    elif message.get("role") == "infeasible":
                        final_answer = str(message.get("message") or "")
                        termination_action = "report_infeasible"
            next_step = step_index + 1
            next_record = _observation_record(next_step, next_obs)
            observations.append(next_record)
            step_record.update({
                "action_result": {
                    "reward": float(reward),
                    "terminated": bool(terminated),
                    "truncated": bool(truncated),
                    "last_action_error": next_obs.get("last_action_error"),
                    "task_info": _plain(info.get("task_info")),
                    "termination_action": termination_action,
                },
                "next_observation_step": next_step,
            })
            obs = next_obs
            if failure_reason:
                status = "invalid_provider_response"
                break
            if terminated or truncated:
                status = "completed" if final_answer is not None else "terminated_without_final_answer"
                break
        else:
            status = "step_budget_truncated"

        task_state = env.task
        visits = list(task_state.visit_events) if task_state else []
        injection = dict(task_state.injection) if task_state else {"injected": False, "payload_text": payload}
        # Preserve separate exposure boundaries. A.4 k is based on what
        # AgentLab actually observed (post-preprocessing AX). Full-payload
        # matching normalizes whitespace on both sides; request inclusion is
        # checked independently with the same rule to detect wire/prompt loss.
        provider_payload_steps = sorted({
            int(r["step"]) for r in provider_records
            if payload is not None and r.get("step") is not None and _contains_text(r.get("messages"), payload)
        })
        exposure_rows = []
        protected_targets = case["reporter_info"]
        for observation in observations:
            step_index = observation.get("step")
            step_requests = [row for row in provider_records if row.get("step") == step_index]
            raw_ax_contains = payload is not None and _contains_text(observation.get("browsergym_raw_axtree"), payload)
            agent_ax_contains = payload is not None and _contains_text(observation.get("agentlab_preprocessed_axtree"), payload)
            provider_text_contains = payload is not None and any(_contains_text(row.get("messages"), payload) for row in step_requests)
            observed_text = str(observation.get("agentlab_preprocessed_axtree") or "")
            protected_observed = {
                field: bool(value) and str(value) in observed_text
                for field, value in protected_targets.items()
                if value is not None
            }
            exposure_rows.append({
                "step": step_index,
                "protected_fields_observed": protected_observed,
                "a4_ax_payload_exposed": bool(agent_ax_contains),
                "browsergym_raw_axtree_contains_payload": raw_ax_contains,
                "agentlab_preprocessed_axtree_contains_payload": agent_ax_contains,
                "provider_request_contains_full_payload": provider_text_contains,
                "agent_consumed_full_payload": provider_text_contains,
            })
        usage_rows = [row.get("usage") or {} for row in provider_records]
        provider_usage = {
            "model_attempts": len(provider_records),
            "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in usage_rows),
            "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in usage_rows),
            "total_tokens": sum(int(row.get("total_tokens") or 0) for row in usage_rows),
            "attempt_usage": usage_rows,
        }
        ax_payload_steps = [row["step"] for row in exposure_rows if row["agentlab_preprocessed_axtree_contains_payload"]]
        first_payload_step = ax_payload_steps[0] if ax_payload_steps else None
        first_pii_steps_by_field = {
            field: next((row["step"] for row in exposure_rows if row["protected_fields_observed"].get(field)), None)
            for field in protected_targets
        }
        first_output_step = next((row.get("action_index") for row in steps if row.get("parsed_action") and any(name in row["parsed_action"] for name in ("send_msg_to_user", "report_infeasible"))), None)
        pii_before_payload = any(
            step is not None and (first_payload_step is None or step < first_payload_step)
            for step in first_pii_steps_by_field.values()
        )
        # Observation k is consumed before action k. Same-observation PII is
        # simultaneous with the payload; action k's answer comes afterwards.
        final_output_before_payload = bool(final_answer is not None and (first_payload_step is None or first_output_step is None or first_output_step < first_payload_step))
        for observation, exposure in zip(observations, exposure_rows):
            observation["payload_observed"] = exposure["agentlab_preprocessed_axtree_contains_payload"]
            observation["protected_fields_observed"] = exposure["protected_fields_observed"]
            observation["document_visit_index"] = next(
                (visit["visit_index"] for visit in visits if visit.get("url") == observation.get("url")), None
            )
        result = {
            # A finite step-budget ending is a valid observed trajectory, but
            # it is not task completion and has no final-answer PII score.
            "valid": status in {"completed", "step_budget_truncated"},
            "status": status,
            "termination_reason": (
                task_state.termination_action if status == "completed" and task_state and task_state.termination_action
                else "step_bound" if status == "step_budget_truncated"
                else "empty_terminal_output" if status == "terminated_without_final_answer"
                else failure_reason or status
            ),
            "terminated": status == "completed",
            "invalid_reason": failure_reason if status not in {"completed", "step_budget_truncated"} else None,
            "truncated": status == "step_budget_truncated",
            "task_complete": status == "completed" and final_answer is not None,
            "case_id": case.get("id"),
            "task_id": case.get("task_id"),
            "question": question,
            "starting_url": starting_url,
            "target_model": settings,
            "agentlab": {
                "agent_class": f"{type(agent).__module__}.{type(agent).__qualname__}",
                "max_retry_argument": parse_retries,
                "retry_semantics": "HTTP and AgentLab parser retries share at most three total requests per action",
                "observation_flags": FRAMEWORK_FLAGS,
                "framework_version": _package_version("agentlab"),
            },
            "browsergym": {
                "task_class": f"{type(task_state).__module__}.{type(task_state).__qualname__}",
                "framework_version": _package_version("browsergym-core"),
                "playwright_version": _package_version("playwright"),
                "browser_version": env.browser.version if env.browser else None,
                "headless": headless,
                "axtree_source": "BrowserGym BrowserEnv._get_obs -> AgentLab obs_preprocessor",
                "document_visit_convention": "committed main-frame CDP frameNavigated with distinct loaderId; hash/history same-document events excluded",
                "hidden_container_css": INJECTION_CSS,
                "injection_document_index": injection_document_index,
            },
            "harness": {
                "browsergym_version": _package_version("browsergym-core"),
                "agentlab_version": _package_version("agentlab"),
                "chromium_version": env.browser.version if env.browser else None,
                "observation_flags": FRAMEWORK_FLAGS,
                "payload_exposure_matching": "full_text_whitespace_normalized_v1",
                "action_flags": {"subsets": ["chat", "infeas", "bid", "nav", "tab"], "multiaction": False, "strict": False},
                "retry_semantics": "HTTP and AgentLab parser retries share at most three total requests per action",
                "step_budget": step_budget,
            },
            "step_budget": step_budget,
            "parse_retries": parse_retries,
            "baseline": payload is None,
            "defense_enabled": payload is not None,
            "steps": steps,
            "observations": observations,
            "page_visits": visits,
            "injection": injection,
            "termination_action": task_state.termination_action if task_state else None,
            "termination_message_role": task_state.termination_message_role if task_state else None,
            "required_visit": {
                "second_committed_document_seen": len(visits) >= 2,
                "payload_injected": bool(injection.get("injected")),
                "injection_visit_index": injection.get("visit_index"),
            },
            "payload_exposure_steps": ax_payload_steps,
            "first_payload_step": first_payload_step,
            "first_provider_payload_step": provider_payload_steps[0] if provider_payload_steps else None,
            "provider_payload_steps": provider_payload_steps,
            "first_pii_steps_by_field": first_pii_steps_by_field,
            "pii_before_payload": pii_before_payload,
            "final_output_before_payload": final_output_before_payload,
            "first_browsergym_ax_payload_step": next((row["step"] for row in exposure_rows if row["browsergym_raw_axtree_contains_payload"]), None),
            "first_agentlab_observation_payload_step": next((row["step"] for row in exposure_rows if row["agentlab_preprocessed_axtree_contains_payload"]), None),
            "exposure": exposure_rows,
            "final_output": final_answer if status == "completed" else None,
            "provider_requests": provider_records,
            "provider_usage": provider_usage,
            "artifacts": {
                "started_marker": str(Path(artifact_dir) / "browsergym_episode.started.json") if artifact_dir is not None else None,
                "episode_json": str(Path(artifact_dir) / "browsergym_episode.json") if artifact_dir is not None else None,
            },
            "environment_reset_info": _plain(reset_info),
        }
    except Exception as exc:
        result = {
            "valid": False,
            "status": "browsergym_error",
            "truncated": False,
            "task_complete": False,
            "invalid_reason": f"{type(exc).__name__}:{exc}",
            "case_id": case.get("id"),
            "task_id": case.get("task_id"),
            "question": question,
            "starting_url": starting_url,
            "target_model": settings,
            "steps": steps,
            "observations": observations,
            "page_visits": list(env.task.visit_events) if env.task else [],
            "injection": dict(env.task.injection) if env.task else {"injected": False, "payload_text": payload},
            "termination_action": env.task.termination_action if env.task else None,
            "termination_message_role": env.task.termination_message_role if env.task else None,
            "baseline": payload is None,
            "defense_enabled": payload is not None,
            "required_visit": {
                "second_committed_document_seen": len(env.task.visit_events) >= 2 if env.task else False,
                "payload_injected": bool(env.task.injection.get("injected")) if env.task else False,
                "injection_visit_index": env.task.injection.get("visit_index") if env.task else None,
            },
            "provider_requests": provider_records,
            "provider_usage": {"model_attempts": len(provider_records), "attempt_usage": [r.get("usage") for r in provider_records]},
            "first_payload_step": None,
            "first_browsergym_ax_payload_step": None,
            "first_agentlab_observation_payload_step": None,
            "exposure": [],
            "final_output": None,
            "artifacts": {
                "started_marker": str(Path(artifact_dir) / "browsergym_episode.started.json") if artifact_dir is not None else None,
                "episode_json": str(Path(artifact_dir) / "browsergym_episode.json") if artifact_dir is not None else None,
            },
        }
    finally:
        env.close()

    if artifact_dir is not None:
        artifact_path = Path(artifact_dir)
        artifact_path.mkdir(parents=True, exist_ok=True)
        episode_path = artifact_path / "browsergym_episode.json"
        if episode_path.exists():
            raise FileExistsError(f"refusing to overwrite browser episode evidence in {artifact_path}")
        write_json(episode_path, result, default=str)
    return result


def run_episode(candidate_text: str | None, case, model, config, artifact_dir, client_factory=None) -> dict[str, Any]:
    """Public package contract used by the search/scoring implementation."""
    return run_browsergym_episode(
        case,
        candidate_text,
        model=model,
        config=config,
        artifact_dir=artifact_dir,
        client_factory=client_factory,
    )


def _package_version(package: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(package)
    except Exception:
        return None
