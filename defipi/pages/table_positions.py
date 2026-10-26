from defipi.jsonio import content_key


def build_table_position_key(record, context_text=""):
    record = record or {}
    for key in ("table_position_key", "slug", "id", "url"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    fallback = str(context_text or "").strip()
    if fallback:
        return fallback
    raise ValueError("Unable to derive table_position_key from record or context.")


def derive_table_insert_index(paragraph_count, seed, position_key):
    paragraph_count = int(paragraph_count or 0)
    if paragraph_count < 0:
        raise ValueError(f"paragraph_count must be >= 0, got {paragraph_count}.")

    slot_count = paragraph_count + 1
    try:
        normalized_seed = int(seed)
    except (TypeError, ValueError):
        raise ValueError(f"seed must be an integer, got {seed!r}.") from None

    normalized_key = str(position_key or "").strip()
    if not normalized_key:
        raise ValueError("position_key must not be empty.")

    return int(content_key(f"{normalized_seed}|{paragraph_count}|{normalized_key}")[:16], 16) % slot_count
