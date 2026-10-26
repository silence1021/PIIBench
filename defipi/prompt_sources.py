"""Pinned third-party prompt sources reused by defense methods."""
import importlib.util
from defipi.paths import PROJECT_ROOT

UPSTREAM_PROMPTS_PATH = PROJECT_ROOT / "third_party/webcloak/prompts.py"

def load_upstream_prompt_source():
    if not UPSTREAM_PROMPTS_PATH.is_file():
        raise FileNotFoundError(f"Pinned WebCloak prompt source missing: {UPSTREAM_PROMPTS_PATH}")
    spec = importlib.util.spec_from_file_location("webcloak_pii_pinned_stage2_prompts", UPSTREAM_PROMPTS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
