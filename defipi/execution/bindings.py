"""Bind execution capabilities without interpreting research-question names."""
from .adapters import AdapterExecutor


def bind_adapter(config, name, container_image, max_model_requests):
    if name in {"pi-agent", "codex", "dsh", "claude"}:
        from .agents.container import adapter_config
        config["adapter"] = adapter_config(name, container_image, max_model_requests=max_model_requests,
            tool_config=config["tool_config"],
            timeout_seconds=config["tool_config"]["trajectory_timeout_seconds"])
    elif name == "openrouter-url":
        from .api.openrouter_url import adapter_config
        config["adapter"] = adapter_config(tool_config=config["tool_config"],
            timeout_seconds=config["adapter_timeout_seconds"])
    elif name == "anthropic-url":
        from .api.anthropic_url import adapter_config
        config["adapter"] = adapter_config(tool_config=config["tool_config"],
            timeout_seconds=config["adapter_timeout_seconds"])
    elif name is not None:
        raise ValueError(f"Unknown execution adapter: {name}")


def make_executor(dispatch, target, context, output, limiter, evaluator_class):
    if dispatch.get("adapter"):
        return AdapterExecutor(dispatch, target, context, output, limiter)
    evaluator = evaluator_class(
        model=target["model"], backend=target["backend"], temperature=target["temperature"],
        client_settings=target["client_settings"], max_retries=1, retry_backoff_seconds=0,
        observation_format="html", enable_filter=False, max_response_tokens=target["max_response_tokens"],
        required_finish_reasons=target["required_finish_reasons"],
        reasoning_effort=target.get("reasoning_effort"),
        request_limiter=limiter)
    evaluator.filter = None
    return evaluator
