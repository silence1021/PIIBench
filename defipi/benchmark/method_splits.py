"""Read a benchmark development pool or an explicit method subset."""
import json
from pathlib import Path

def load_method_split(path, method=None):
    """Use the common development pool unless a method subset is requested."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != "piibench-split-v1":
        raise ValueError("Expected piibench-split-v1")
    heldout, development = data["heldout_ids"], data["development_ids"]
    ids = development if method is None else data["methods"][method]["developer_ids"]
    for values in (heldout, development, ids):
        if (not isinstance(values, list) or not values or
                any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values)):
            raise ValueError("Split IDs must be non-empty unique string lists")
    if set(heldout) & set(development) or not set(ids) <= set(development):
        raise ValueError("Method development membership must exclude heldout pages")
    return {"heldout_record_ids": heldout, "eval_record_ids": ids}
