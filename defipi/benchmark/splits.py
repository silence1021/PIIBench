import json
import random

from defipi.pages.injection import split_paragraphs
from defipi.pages.reporter_info import (
    resolve_clean_reporter_info,
    resolve_rendered_reporter_info,
)
from defipi.pages.table_positions import build_table_position_key, derive_table_insert_index


SEARCH_SPLIT_VERSION = 2


def _hydrate_table_insert_index(record, table_seed):
    item = dict(record)
    if item.get("table_insert_index") is not None:
        return item
    context_text = item["context"]
    paragraphs = split_paragraphs(context_text)
    position_key = build_table_position_key(item, context_text)
    item["table_insert_index"] = derive_table_insert_index(
        len(paragraphs),
        table_seed,
        position_key,
    )
    return item


def load_seed_records(path, *, start_index, limit, table_seed):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if type(data) is not list or any(type(r) is not dict or "id" not in r or type(r.get("context")) is not str for r in data):
        raise ValueError("Seed records require a list of objects with id and context")
    if type(start_index) is not int or start_index < 0:
        raise ValueError("start_index must be a nonnegative integer")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be null or a nonnegative integer")
    if type(table_seed) is not int:
        raise ValueError("table_seed must be an integer")
    records = data
    records = [_hydrate_table_insert_index(r, table_seed=table_seed) for r in records]
    if start_index > 0:
        records = records[start_index:]
    if limit is not None:
        records = records[:limit]
    return records


def build_search_split(
    records,
    heldout_size,
    eval_pool_size,
    data_split_seed,
):
    """Reserve held-out records, then a contiguous search-scoring pool."""
    source_records = list(records)
    source_count = len(source_records)
    for name, value in (("heldout_size", heldout_size), ("eval_pool_size", eval_pool_size)):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if type(data_split_seed) is not int:
        raise ValueError("data_split_seed must be an integer")
    required_count = heldout_size + eval_pool_size
    if required_count > source_count:
        raise ValueError(
            "requested split exceeds source_count: "
            f"{required_count} > {source_count}"
        )

    shuffled_records = list(source_records)
    random.Random(data_split_seed).shuffle(shuffled_records)
    heldout_records = shuffled_records[:heldout_size]
    eval_pool_records = shuffled_records[heldout_size:required_count]
    unused_records = shuffled_records[required_count:]
    heldout_ids = {record.get("id") for record in heldout_records}
    eval_ids = {record.get("id") for record in eval_pool_records}
    if heldout_ids & eval_ids:
        raise ValueError("heldout and eval pool record IDs must be disjoint")

    return {
        "mode": "fixed_heldout_eval_pool",
        "data_split_version": SEARCH_SPLIT_VERSION,
        "data_split_seed": data_split_seed,
        "source_records": source_records,
        "optimize_records": eval_pool_records,
        "heldout_records": heldout_records,
        "eval_pool_records": eval_pool_records,
        "unused_records": unused_records,
        "disjoint_eval_pool": True,
        "counts": {
            "source_count": source_count,
            "heldout_count": len(heldout_records),
            "optimize_count": len(eval_pool_records),
            "eval_pool_count": len(eval_pool_records),
            "unused_count": len(unused_records),
        },
    }


def split_gepa_eval_pool(eval_pool_records, feedback_pool_size, pareto_pool_size):
    """Split an existing eval pool by stable order for GEPA search."""
    records = list(eval_pool_records)
    if type(feedback_pool_size) is not int or type(pareto_pool_size) is not int:
        raise ValueError("GEPA pool sizes must be integers")
    if feedback_pool_size < 1:
        raise ValueError("feedback_pool_size must be >= 1")
    if pareto_pool_size < 1:
        raise ValueError("pareto_pool_size must be >= 1")
    required = feedback_pool_size + pareto_pool_size
    if required > len(records):
        raise ValueError(
            "feedback_pool_size + pareto_pool_size exceeds eval pool: "
            f"{feedback_pool_size} + {pareto_pool_size} > {len(records)}"
        )

    feedback_records = records[:feedback_pool_size]
    pareto_records = records[feedback_pool_size:required]
    feedback_ids = {record.get("id") for record in feedback_records}
    pareto_ids = {record.get("id") for record in pareto_records}
    if feedback_ids & pareto_ids:
        raise ValueError("feedback and Pareto record IDs must be disjoint")
    return {
        "feedback_records": feedback_records,
        "pareto_records": pareto_records,
        "unused_records": records[required:],
        "feedback_record_ids": [record.get("id") for record in feedback_records],
        "pareto_record_ids": [record.get("id") for record in pareto_records],
    }


def build_context_records(records):
    context_records = []
    for record in records:
        context_records.append(
            {
                "id": record.get("id"),
                "context": record.get("context", ""),
                "question": record.get("question", ""),
                "ideal": record.get("ideal", []),
                "reporter_info": resolve_clean_reporter_info(record),
                "rendered_reporter_info": resolve_rendered_reporter_info(record),
            }
        )
    return context_records


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")


SPLIT_MANIFEST_VERSION = "piibench-split-v1"


def load_gepa_split(records, path, *, scenario):
    """Bind GEPA to the published split lists without sampling or reordering."""
    with open(path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SPLIT_MANIFEST_VERSION:
        raise ValueError("Unsupported benchmark split manifest")
    if manifest.get("scenario") != scenario:
        raise ValueError("Split manifest scenario does not match fragment_profile")

    def ids(value, label):
        if (not isinstance(value, list) or not value
                or any(not isinstance(item, str) or not item.strip() for item in value)
                or len(set(value)) != len(value)):
            raise ValueError(f"{label} must be a nonempty list of unique record IDs")
        return list(value)

    heldout = ids(manifest.get("heldout_ids"), "heldout_ids")
    development = ids(manifest.get("development_ids"), "development_ids")
    if set(heldout) & set(development):
        raise ValueError("Heldout and development IDs must be disjoint")
    methods = manifest.get("methods")
    if not isinstance(methods, dict) or not isinstance(methods.get("gepa"), dict):
        raise ValueError("Split manifest requires methods.gepa")
    method = methods["gepa"]
    developer = ids(method.get("developer_ids"), "methods.gepa.developer_ids")
    feedback = ids(method.get("feedback_ids"), "methods.gepa.feedback_ids")
    pareto = ids(method.get("pareto_ids"), "methods.gepa.pareto_ids")
    if not set(developer) <= set(development):
        raise ValueError("GEPA developer IDs must belong to development_ids")
    if set(feedback) & set(pareto):
        raise ValueError("GEPA feedback and Pareto IDs must be disjoint")
    if set(feedback) | set(pareto) != set(developer):
        raise ValueError("GEPA feedback and Pareto IDs must cover exactly developer_ids")
    source = list(records)
    indexed = {record["id"]: record for record in source}
    if len(indexed) != len(source):
        raise ValueError("Source records must have unique IDs")
    missing = (set(heldout) | set(development)) - set(indexed)
    if missing:
        raise ValueError(f"Split IDs missing from seed records: {sorted(missing)}")
    return {
        "schema_version": SPLIT_MANIFEST_VERSION,
        "source_records": source,
        "heldout_records": [indexed[key] for key in heldout],
        "eval_pool_records": [indexed[key] for key in developer],
        "feedback_records": [indexed[key] for key in feedback],
        "pareto_records": [indexed[key] for key in pareto],
        "feedback_record_ids": feedback,
        "pareto_record_ids": pareto,
    }
