"""Explicit API-key judge transport; constructed only during paid run/retry."""
from defipi.execution.api.clients import build_text_client


def build_judge_client(binding):
    provider = binding["provider"]
    settings = {"base_url": binding["base_url"], "api_key_env": binding["api_key_env"],
                "request_timeout_seconds": binding.get("request_timeout_seconds", 120),
                "request_headers": binding.get("request_headers", {}),
                "request_parameters": binding.get("request_parameters", {}),
                "omit_parameters": binding.get("omit_parameters", [])}
    client = build_text_client(binding["model"], backend=provider, client_settings=settings,
                               max_retries=1, retry_backoff_seconds=0)
    class JudgeClient:
        def generate(self, **kwargs):
            # Route/generation parameters are already frozen in the validated client settings.
            kwargs.pop("provider", None)
            for field in ("thinking", "output_config"):
                kwargs.pop(field, None)
            kwargs["reasoning_effort"] = None
            kwargs["temperature"] = None
            return client.generate(**kwargs)
    return JudgeClient()
