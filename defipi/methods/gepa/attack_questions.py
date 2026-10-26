"""Read collection attack prompts and sample a question per evaluation."""

import copy
import json
from pathlib import Path
import random


SAMPLING_VERSION = "random-attack-per-repeat-v1"
COLLECTION_DOMAINS = ("academic", "news")


def _read_prompt_source(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Attack collection must be a JSON array")
    if not payload:
        raise ValueError(f"No attack prompts in {path}")
    if any(not isinstance(row, dict) for row in payload):
        raise ValueError("Every attack collection row must be an object")
    return payload


def _load_collection_prompts(records, domain):
    if domain not in COLLECTION_DOMAINS:
        raise ValueError(f"Unsupported attack domain: {domain}")
    selected, seen_ids = [], set()
    for row_number, record in enumerate(records, 1):
        row_domain = record.get("domain")
        if row_domain not in COLLECTION_DOMAINS:
            raise ValueError(f"Attack collection row {row_number} has invalid domain: {row_domain!r}")
        prompt_id = record.get("id")
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ValueError(f"Attack collection row {row_number} requires non-empty id")
        if prompt_id in seen_ids:
            raise ValueError(f"Duplicate attack prompt_id: {prompt_id}")
        seen_ids.add(prompt_id)
        for key in ("pair_id", "scenario_id", "base_request_id"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                raise ValueError(f"Attack collection row {row_number} requires non-empty {key}")
        if not isinstance(record.get("version"), (int, str)) or isinstance(record.get("version"), bool):
            raise ValueError(f"Attack collection row {row_number} requires version")
        text = record.get("prompt")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Attack collection row {row_number} requires non-empty prompt")
        if row_domain != domain:
            continue
        # Keep every source field and expose the runtime keys used by
        # sampling/evaluation. In this format, prompt text is already a question.
        selected.append({**record, "prompt_id": prompt_id, "scenario": row_domain,
                         "pattern_id": record["scenario_id"], "prompt_text": text,
                         "question": text})
    if not selected:
        raise ValueError(f"No {domain} attack prompts in collection")
    return selected


def load_attack_prompts(path, scenario, observation_format):
    if observation_format not in ("html", "axtree"):
        raise ValueError(f"Unsupported observation_format: {observation_format}")
    return _load_collection_prompts(_read_prompt_source(path), scenario)


def load_attack_pool(path, scenario, seed, observation_format):
    return {"version": SAMPLING_VERSION, "source_path": str(path), "scenario": scenario,
            "sampling_seed": seed, "observation_format": observation_format,
            "question_adaptation_version": "verbatim-question-v1",
            "prompts": load_attack_prompts(path, scenario, observation_format)}


def sample_attack(pool, draw_id):
    """Independent, with-replacement draws; replaying a logical draw is stable.

    Each stage/candidate/page/repeat has a different draw ID. A separate seeded
    stream per ID makes sampling independent of channel count and retry order.
    """
    seed = json.dumps([SAMPLING_VERSION, pool["sampling_seed"], pool["scenario"], draw_id], ensure_ascii=False)
    return copy.deepcopy(random.Random(seed).choice(pool["prompts"]))


def with_attack(record, prompt, draw_id):
    return {**record, "question": prompt["question"], "attack_prompt": copy.deepcopy(prompt),
            "attack_draw_id": draw_id}
