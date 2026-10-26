import json
import os
from typing import Any, Dict

import yaml


def load_system_prompt(prompt_path: str) -> str:
    """Load a system prompt string from a YAML file.

    Always returns a non-empty string; falls back to a safe default when missing.
    """
    try:
        if yaml is None:
            raise RuntimeError("yaml module not available")
        with open(prompt_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        sp = data.get("system") if isinstance(data, dict) else None
        if isinstance(sp, str) and sp.strip():
            return sp
        return "Empty System Prompt"
    except Exception:
        return "Empty System Prompt"
    
def extract_json_object(raw_text: str) -> Dict[str, Any]:
    """Extract the first JSON object from a raw text string.

    Uses simple heuristics to find the first {...} substring and parse it as JSON.
    Returns an empty dict if no valid JSON object is found.
    """
    # Ditch thinking <think></think> tags:
    raw_text = raw_text.split("</think>")[-1]

    # The model is instructed to return pure JSON, but be robust.
    raw_text = (raw_text or "").strip()

    # Fast path: direct JSON
    try:
        obj = json.loads(raw_text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # Heuristic: find first {...} block
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = raw_text[start : end + 1]
        try:
            obj = json.loads(snippet)
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None