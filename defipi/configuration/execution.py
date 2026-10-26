"""Strict configuration contracts for native agents and URL provider adapters."""
from .schema import Field, Problem, ValidationError, validate_config
from urllib.parse import urlsplit

PROTOCOLS = ("openai-responses", "openai-completions", "anthropic-messages")
BACKENDS = ("openai", "anthropic", "deepseek", "openrouter")
POSITIVE = Field((int, float), minimum=0.000001)
ANTHROPIC_NATIVE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
PI_MODEL_FIELDS = {
    "reasoning": Field(bool), "input": Field(list, items=Field(str, choices=("text", "image"))),
    "contextWindow": Field(int, minimum=1), "maxTokens": Field(int, minimum=1),
    "thinkingLevelMap": Field(dict, required=False, schema={
        level: Field(str, required=False, nullable=True, choices=ANTHROPIC_NATIVE_EFFORTS)
        for level in ("off", "minimal", "low", "medium", "high", "xhigh", "max")}),
    "compat": Field(dict, required=False, schema={
        "forceAdaptiveThinking": Field(bool), "supportsTemperature": Field(bool)}),
}
DSH_MODEL_FIELDS = {
    "contextWindow": Field(int, required=False, minimum=1), "maxTokens": Field(int, required=False, minimum=1),
    "input": Field(list, required=False, items=Field(str, choices=("text", "image"))),
    "reasoningEfforts": Field(dict, required=False, values=Field(str)),
}
NATIVE_PARAMETERS = {"provider", "reasoning", "reasoning_effort", "thinking", "top_p", "top_k", "frequency_penalty", "presence_penalty"}
NATIVE_OMISSIONS = {"temperature", "top_p", "max_tokens", "max_completion_tokens", "max_output_tokens", "reasoning_effort", "reasoning", "thinking"}
CONTAINER_RUNTIME_FIELDS = ("chrome_executable", "mcp_sdk", "pi_executable", "codex_executable",
                            "dsh_modules", "claude_executable", "model_proxy_url")


def container_host_problems(settings, *, prefix):
    """Container-installed paths and capability URLs are internal inputs."""
    if not isinstance(settings, dict):
        return [Problem(prefix, "expected dict")]
    return [Problem(f"{prefix}.{name}", "set by the container runtime; not a host configuration field")
            for name in CONTAINER_RUNTIME_FIELDS if name in settings]


def _collect(problems, callback):
    try:
        callback()
    except ValidationError as exc:
        problems.extend(exc.problems)


def _client(settings, backend, problems, prefix, *, native=False, direct_pi=False, openrouter_url=False):
    # Shared schema owns field names, exact types, URL and parameter checks.
    from defipi.execution.api.clients import CLIENT_SETTINGS_FIELDS, validate_client_settings
    fields = dict(CLIENT_SETTINGS_FIELDS)
    if direct_pi:
        for name in ("request_timeout_seconds", "omit_parameters"):
            fields.pop(name)
        if backend != "openrouter":
            fields.pop("request_parameters")
    if openrouter_url:
        fields.pop("omit_parameters")
    if native == "claude":
        fields.pop("request_parameters")
        fields.pop("omit_parameters")
    try:
        validate_client_settings(settings, backend, fields=fields)
    except ValidationError as exc:
        for problem in exc.problems:
            path = problem.path
            if path.startswith("client_settings."):
                path = path[len("client_settings."):]
            problems.append(Problem(f"{prefix}.{path}" if path else prefix, problem.message))
        return
    parsed = urlsplit(settings["base_url"])
    if parsed.query or parsed.fragment:
        problems.append(Problem(f"{prefix}.base_url", "query and fragment are not allowed"))
    if native:
        parameters = settings.get("request_parameters", {})
        permitted = NATIVE_PARAMETERS | ({"temperature"} if native == "pi-agent" else set())
        if set(parameters) - permitted:
            problems.append(Problem(f"{prefix}.request_parameters", "native conversation, tools and lifecycle are immutable"))
        if set(settings.get("omit_parameters", [])) - NATIVE_OMISSIONS:
            problems.append(Problem(f"{prefix}.omit_parameters", "native conversation, tools and lifecycle are immutable"))
        headers = settings["request_headers"]
        if set(headers) - {"X-OpenRouter-Metadata"} or any(v not in ("enabled", "disabled") for v in headers.values()):
            problems.append(Problem(f"{prefix}.request_headers", "only the OpenRouter routing metadata header is configurable"))


def validate_native_target(target, harness, *, prefix="target", final=False, container=False):
    """Validate the explicit transport and authentication binding for the selected harness."""
    fields = {
        "backend": Field(str, choices=BACKENDS), "model": Field(str),
        "api": Field(str, choices=PROTOCOLS),
        "client_settings": Field(dict),
        "reasoning_effort": Field(str, nullable=True),
        "temperature": Field((int, float), nullable=True, minimum=0, maximum=2),
        "max_response_tokens": Field(int, nullable=True, minimum=1),
        "required_finish_reasons": Field(list, required=final, items=Field(str)),
        "api_id": Field(str, required=final, nullable=True),
    }
    if harness == "claude":
        fields.pop("temperature", None)
        fields.pop("max_response_tokens", None)
        fields["auth_mode"] = Field(str, required=False, choices=("api_key",))
    if harness == "codex":
        fields["auth_mode"] = Field(str, required=False, choices=("api_key",))
    if harness == "pi-agent":
        if not container:
            fields.pop("temperature", None)
            if isinstance(target, dict) and target.get("backend") == "openrouter":
                fields.pop("max_response_tokens", None)
        fields["pi_model"] = Field(dict, nullable=True, schema=PI_MODEL_FIELDS)
        fields["pi_omit_parameters"] = Field(list, items=Field(str, choices=tuple(sorted(NATIVE_OMISSIONS))))
    if harness is None:
        fields["top_p"] = Field((int, float), required=False, nullable=True, minimum=0, maximum=1)
        backend = target.get("backend") if isinstance(target, dict) else None
        if backend == "openrouter":
            for name in ("temperature", "max_response_tokens", "reasoning_effort", "top_p"):
                fields.pop(name, None)
        elif backend == "anthropic":
            fields.pop("reasoning_effort")
            fields.update(thinking=Field(dict, nullable=True), output_config=Field(dict, nullable=True),
                          top_p=Field((int, float), nullable=True, minimum=0, maximum=1))
        elif backend == "deepseek":
            fields["top_p"] = Field((int, float), nullable=True, minimum=0, maximum=1)

    def rules(data, problems):
        for name in ("model", "api_id"):
            if isinstance(data.get(name), str) and not data[name].strip():
                problems.append(Problem(f"{prefix}.{name}", "must be non-empty"))
        if isinstance(data.get("required_finish_reasons"), list) and not data["required_finish_reasons"]:
            problems.append(Problem(f"{prefix}.required_finish_reasons", "must be non-empty"))
        if isinstance(data.get("client_settings"), dict) and data.get("backend") in BACKENDS:
            _client(data["client_settings"], "openai" if data.get("api") == "openai-responses" else data["backend"], problems, f"{prefix}.client_settings", native=harness if harness is not None else False,
                    direct_pi=harness == "pi-agent" and not container, openrouter_url=harness is None and data["backend"] == "openrouter")
        if "reasoning_effort" in data and (data.get("reasoning_effort") is None or isinstance(data.get("reasoning_effort"), str)) and data.get("backend") in BACKENDS:
            from defipi.execution.api.clients import validate_reasoning_effort
            try:
                if harness is not None and data["backend"] == "anthropic":
                    if data["reasoning_effort"] not in ANTHROPIC_NATIVE_EFFORTS:
                        raise ValueError("bind an explicit Anthropic native effort: " + ", ".join(ANTHROPIC_NATIVE_EFFORTS))
                else:
                    validate_reasoning_effort("openai" if data.get("api") == "openai-responses" else data["backend"], data["reasoning_effort"])
            except ValueError as exc:
                problems.append(Problem(f"{prefix}.reasoning_effort", str(exc)))
        if harness == "codex" and data.get("api") != "openai-responses":
            problems.append(Problem(f"{prefix}.api", "Codex requires Responses"))
        if harness == "claude" and data.get("api") != "anthropic-messages":
            problems.append(Problem(f"{prefix}.api", "Claude requires Anthropic Messages"))
        if harness is not None and data.get("backend") == "anthropic" and data.get("api") != "anthropic-messages":
            problems.append(Problem(f"{prefix}.api", "Anthropic native agents require Anthropic Messages"))
        if harness == "pi-agent":
            if data.get("backend") != "anthropic" and isinstance(data.get("api"), str) and data["api"] not in ("openai-responses", "openai-completions"):
                problems.append(Problem(f"{prefix}.api", "Pi requires Responses or Chat Completions for this backend"))
            effort = data.get("reasoning_effort")
            if effort is None or isinstance(effort, str) and effort not in ("none", "low", "medium", "high", "xhigh", "max"):
                problems.append(Problem(f"{prefix}.reasoning_effort", "bind an explicit Pi thinking level"))
            cap = data.get("max_response_tokens")
            if "max_response_tokens" in fields and type(cap) is int and cap < 16:
                problems.append(Problem(f"{prefix}.max_response_tokens", "Pi token cap must be at least 16"))
            if data.get("api") == "openai-responses" and data.get("pi_model") is None:
                problems.append(Problem(f"{prefix}.pi_model", "Responses requires explicit native model metadata"))
            if data.get("backend") == "anthropic":
                metadata = data.get("pi_model")
                if not isinstance(metadata, dict):
                    problems.append(Problem(f"{prefix}.pi_model", "Anthropic requires explicit adaptive model metadata"))
                else:
                    compat = metadata.get("compat")
                    if metadata.get("reasoning") is not True or not isinstance(compat, dict) or compat.get("forceAdaptiveThinking") is not True or compat.get("supportsTemperature") is not False:
                        problems.append(Problem(f"{prefix}.pi_model", "bind reasoning, adaptive thinking and unsupported temperature explicitly"))
                    mapping = metadata.get("thinkingLevelMap")
                    if not isinstance(mapping, dict) or not isinstance(effort, str) or mapping.get(effort) != effort:
                        problems.append(Problem(f"{prefix}.pi_model.thinkingLevelMap", "map the selected native effort explicitly without substitution"))
                omissions = data.get("pi_omit_parameters")
                if isinstance(omissions, list) and "thinking" in omissions:
                    problems.append(Problem(f"{prefix}.pi_omit_parameters", "Anthropic adaptive thinking must remain in the native payload"))
                if data.get("temperature") is not None:
                    problems.append(Problem(f"{prefix}.temperature", "adaptive Anthropic models require null temperature"))
                client = data.get("client_settings")
                if isinstance(client, dict):
                    parameters = client.get("request_parameters")
                    if isinstance(parameters, dict):
                        for name in ("temperature", "top_p", "top_k", "thinking"):
                            if name in parameters:
                                problems.append(Problem(f"{prefix}.client_settings.request_parameters.{name}", "adaptive Anthropic settings are bound by native model metadata"))
                    omissions = client.get("omit_parameters")
                    if isinstance(omissions, list) and "thinking" in omissions:
                        problems.append(Problem(f"{prefix}.client_settings.omit_parameters", "Anthropic adaptive thinking must remain in the native payload"))
    return validate_config(target, fields, rules=(rules,), prefix=prefix)


def tool_fields(harness, channel, settings, *, container=False, container_inner=False):
    if harness is None:
        access = settings.get("url_access")
        fields = {"url_access": Field(str, choices=("openrouter_server_web_fetch", "deepseek_native_web_search", "anthropic_native_web_fetch"))}
        if access == "openrouter_server_web_fetch":
            fields.update(engine=Field(str, choices=("openrouter",)), provider_name=Field(str, nullable=True),
                          max_tool_calls=Field(int, minimum=1, maximum=20), capture_upstream_debug=Field(bool), execution_authorized=Field(bool))
        elif access == "anthropic_native_web_fetch":
            fields["model_metadata"] = Field(dict, schema={"id": Field(str), "max_tokens": Field(int, minimum=1)})
        return fields
    fields = {
        "max_tool_calls": Field(int, minimum=1), "trajectory_timeout_seconds": Field(int, minimum=1),
        "input_policy": Field(str, choices=("native-tools-post-run-audit-v1",)),
    }
    if harness != "pi-agent" or container:
        fields.update(proxy_connect_timeout_seconds=POSITIVE, max_model_requests=Field(int, minimum=1))
    if container:
        fields["container_image"] = Field(str)
    if harness != "pi-agent":
        fields.update(system_prompt_policy=Field(str, choices=("native-unmodified",)),
                      native_web_tools_policy=Field(str, choices=(("native-tools-with-official-playwright-mcp",) if harness == "dsh" else ("runtime-defaults-native-tools-with-playwright-mcp",))))
    if channel == "url":
        fields.update(chrome_executable=Field(str, nullable=True, required=not container or container_inner), mcp_startup_timeout_seconds=POSITIVE,
                      mcp_tool_timeout_seconds=POSITIVE)
    if harness == "pi-agent":
        fields.update(tool_profile=Field(str, choices=("native", "html-read-only")), pi_executable=Field(str, nullable=True, required=False),
                      pi_http_idle_timeout_seconds=POSITIVE,
                      shared_request_budget=Field(dict, required=False, schema={"limit": Field(int, minimum=1),
                          "directory": Field(str, required=False), "claim_url": Field(str, required=False)}))
        if container:
            fields["model_proxy_url"] = Field(str, required=False)
    else:
        fields["mcp_sdk"] = Field(str, required=not container or container_inner)
        if harness == "codex":
            fields.update(codex_executable=Field(str, required=not container or container_inner))
        elif harness == "claude":
            fields.update(claude_executable=Field(str, required=False), use_1m_context=Field(bool),
                          auto_compact_window=Field(int, nullable=True, minimum=1))
        elif harness == "dsh":
            fields.update(dsh_modules=Field(str, required=not container or container_inner),
                          dsh_model_metadata=Field(dict, schema=DSH_MODEL_FIELDS),
                          dsh_search_target=Field(dict, schema={"model": Field(str), "api": Field(str, choices=("anthropic-messages",)), "client_settings": Field(dict)}))
    return fields


def validate_execution_binding(target, settings, harness, channel, *, prefix="adapter.tool_config", final=False, container=False, container_inner=False):
    problems = []
    _collect(problems, lambda: validate_native_target(target, harness, final=final,
        container=container))
    if isinstance(settings, dict):
        _collect(problems, lambda: validate_config(settings, tool_fields(harness, channel, settings,
            container=container, container_inner=container_inner), prefix=prefix))
        if container and not container_inner:
            problems.extend(container_host_problems(settings, prefix=prefix))
        if isinstance(target, dict):
            if harness not in (None, "pi-agent") and channel != "url":
                problems.append(Problem("experiment.web_injection_channel", "this native harness requires URL input"))
            if harness is None:
                access = settings.get("url_access")
                expected = {"openrouter_server_web_fetch": ("openrouter", "openai-completions"),
                            "deepseek_native_web_search": ("deepseek", "anthropic-messages"),
                            "anthropic_native_web_fetch": ("anthropic", "anthropic-messages")}.get(access) if isinstance(access, str) else None
                if expected and (target.get("backend"), target.get("api")) != expected:
                    problems.append(Problem("target", "backend and protocol must match the selected URL adapter"))
                client = target.get("client_settings")
                parameters = client.get("request_parameters") if isinstance(client, dict) else None
                if access == "openrouter_server_web_fetch" and isinstance(parameters, dict):
                    provider = parameters.get("provider")
                    selected = provider.get("only") if isinstance(provider, dict) else None
                    if "provider" in parameters:
                        if not isinstance(selected, list) or len(selected) != 1 or not isinstance(selected[0], str) or not selected[0].strip():
                            problems.append(Problem("target.client_settings.request_parameters.provider.only", "bind exactly one non-empty supplier slug"))
                        if not isinstance(provider, dict) or provider.get("allow_fallbacks") is not False:
                            problems.append(Problem("target.client_settings.request_parameters.provider.allow_fallbacks", "must be explicitly false"))
                    if isinstance(settings.get("provider_name"), str) and not settings["provider_name"].strip():
                        problems.append(Problem(f"{prefix}.provider_name", "must be non-empty"))
                if access == "anthropic_native_web_fetch":
                    metadata = settings.get("model_metadata")
                    if isinstance(metadata, dict):
                        if metadata.get("id") != target.get("model"):
                            problems.append(Problem(f"{prefix}.model_metadata.id", "must match target.model"))
                        cap, maximum = target.get("max_response_tokens"), metadata.get("max_tokens")
                        if type(cap) is int and type(maximum) is int and cap > maximum:
                            problems.append(Problem("target.max_response_tokens", "exceeds the bound model maximum"))
        budget = settings.get("shared_request_budget")
        if isinstance(budget, dict) and ("directory" in budget) == ("claim_url" in budget):
            problems.append(Problem(f"{prefix}.shared_request_budget", "select exactly one directory or claim_url"))
        if harness == "pi-agent" and settings.get("tool_profile") == "html-read-only" and channel != "html":
            problems.append(Problem(f"{prefix}.tool_profile", "html-read-only requires HTML input"))
        if harness == "dsh" and isinstance(settings.get("dsh_search_target"), dict):
            search = settings["dsh_search_target"]
            if isinstance(search.get("client_settings"), dict):
                _client(search["client_settings"], "deepseek", problems, f"{prefix}.dsh_search_target.client_settings", native=True)
        for name in ("container_image", "input_policy", "pi_executable", "chrome_executable", "codex_executable", "claude_executable", "mcp_sdk", "dsh_modules"):
            if isinstance(settings.get(name), str) and not settings[name].strip():
                problems.append(Problem(f"{prefix}.{name}", "must be non-empty or an explicitly permitted null"))
    else:
        problems.append(Problem(prefix, "expected dict"))
    if problems:
        raise ValidationError(problems)
    return settings


BUILTIN_ADAPTER_SCRIPTS = {
    "container_agent_adapter.py", "pi_agent_adapter.py", "codex_native_agent_adapter.py", "claude_native_agent_adapter.py",
    "native_agent_adapter.py", "openrouter_url_adapter.py", "deepseek_url_adapter.py", "anthropic_url_adapter.py",
}


def is_builtin_adapter(adapter):
    from pathlib import Path
    if not isinstance(adapter, dict):
        return False
    command = adapter.get("command")
    return isinstance(command, list) and any(isinstance(arg, str) and Path(arg).name in BUILTIN_ADAPTER_SCRIPTS for arg in command)


def validate_external_target(target):
    """External commands receive their explicit payload without native-only requirements."""
    fields = {"backend": Field(str, choices=BACKENDS), "model": Field(str), "api_id": Field(str, nullable=True),
        "required_finish_reasons": Field(list, items=Field(str)),
        "temperature": Field((int, float), required=False, nullable=True, minimum=0, maximum=2),
        "top_p": Field((int, float), required=False, nullable=True, minimum=0, maximum=1),
        "reasoning_effort": Field(str, required=False, nullable=True),
        "max_response_tokens": Field(int, required=False, nullable=True, minimum=1),
        "client_settings": Field(dict, required=False), "api": Field(str, required=False, choices=PROTOCOLS),
        "thinking": Field(dict, required=False, nullable=True), "output_config": Field(dict, required=False, nullable=True)}
    def rules(data, problems):
        if isinstance(data.get("model"), str) and not data["model"].strip():
            problems.append(Problem("target.model", "must be non-empty"))
        if isinstance(data.get("required_finish_reasons"), list) and not data["required_finish_reasons"]:
            problems.append(Problem("target.required_finish_reasons", "must be non-empty"))
        if isinstance(data.get("client_settings"), dict) and data.get("backend") in BACKENDS:
            _client(data["client_settings"], data["backend"], problems, "target.client_settings")
    return validate_config(target, fields, rules=(rules,), prefix="target")


def execution_config_rules(config, problems):
    """Initial aggregate preflight; no resource, executable, credential or network access."""
    axes, adapter = config.get("experiment"), config.get("adapter")
    if not isinstance(axes, dict) or not isinstance(adapter, dict):
        return
    if axes.get("mode") != "agent" and axes.get("web_injection_channel") != "url":
        return
    if is_builtin_adapter(adapter):
        from pathlib import Path
        container = any(isinstance(arg, str) and Path(arg).name == "container_agent_adapter.py" for arg in adapter["command"])
        _collect(problems, lambda: validate_execution_binding(config.get("target"), adapter.get("tool_config"),
            axes.get("harness_type"), axes.get("web_injection_channel"), final=True, container=container))
    else:
        _collect(problems, lambda: validate_external_target(config.get("target")))


def validate_execution_request(request, *, container=False, container_inner=False):
    axes = request["experiment"]
    return validate_execution_binding(request["target"], request["tool_config"], axes.get("harness_type"),
                                      request["input"]["kind"], prefix="tool_config", container=container, container_inner=container_inner)


PI_BINDING_FIELDS = {
    "api": Field(str, choices=PROTOCOLS), "pi_model": Field(dict, nullable=True, schema=PI_MODEL_FIELDS),
    "pi_omit_parameters": Field(list, items=Field(str, choices=tuple(sorted(NATIVE_OMISSIONS)))),
}

def pi_target_from_role(role):
    """Use a role's explicit native binding while retaining native sampling behavior."""
    from copy import deepcopy
    client_settings = deepcopy(role["client_settings"])
    effort = role["effort"]
    if role["backend"] == "openrouter" and effort is None:
        effort = "none"
        client_settings["request_parameters"]["temperature"] = role["temperature"]
    return {"backend": role["backend"], "model": role["model"],
            "reasoning_effort": effort, "max_response_tokens": role["max_output_tokens"],
            "temperature": None, "client_settings": client_settings, **role["pi_binding"]}
