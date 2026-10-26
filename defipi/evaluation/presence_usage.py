"""Offline retained-receipt usage normalization."""
import math

def _usage_dict(value):
    plain = value
    if isinstance(plain, dict):
        return plain
    return vars(value) if hasattr(value, "__dict__") else {}


def _usage_number(value, *, integer=False):
    if type(value) is int:
        return value if value >= 0 else None
    if not integer and type(value) is float and math.isfinite(value) and value >= 0:
        return value
    return None


def _normalize_usage(prompt_tokens=None, completion_tokens=None, total_tokens=None, *, usage_details=None):
    prompt = _usage_number(prompt_tokens, integer=True)
    completion = _usage_number(completion_tokens, integer=True)
    total = _usage_number(total_tokens, integer=True)
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    result = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    usage = _usage_dict(usage_details)
    for field in ("prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"):
        detail = {key: valid for key, value in _usage_dict(usage.get(field)).items()
                  if (valid := _usage_number(value, integer=True)) is not None}
        if detail:
            result[field] = detail
    for key, sources in {
        "reasoning_tokens": ("completion_tokens_details", "output_tokens_details"),
        "cached_tokens": ("prompt_tokens_details", "input_tokens_details"),
        "cache_write_tokens": ("prompt_tokens_details", "input_tokens_details"),
    }.items():
        value = _usage_number(usage.get(key), integer=True)
        if value is None:
            value = next((result[field][key] for field in sources if key in result.get(field, {})), None)
        if value is not None:
            result[key] = value
    cost = _usage_number(usage.get("cost"))
    if cost is not None:
        result["cost"] = cost
    cost_details = {key: valid for key, value in _usage_dict(usage.get("cost_details")).items()
                    if (valid := _usage_number(value)) is not None}
    if cost_details:
        result["cost_details"] = cost_details
    return result
