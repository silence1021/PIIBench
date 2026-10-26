import unicodedata


_OBSOLETE_REPORTER_KEYS = {"clean_reporter_info", "reporter_info_clean", "reporter_info_rendered"}


def strip_invisible_format_chars(value):
    if value is None:
        return None
    return "".join(ch for ch in str(value) if unicodedata.category(ch) != "Cf")


def resolve_clean_reporter_info(record=None, *, fallback=None):
    for source in (record, fallback):
        if not isinstance(source, dict):
            continue
        if _OBSOLETE_REPORTER_KEYS & source.keys():
            raise ValueError("Use reporter_info and rendered_reporter_info canonical fields")
        if "reporter_info" in source:
            if type(source["reporter_info"]) is not dict:
                raise ValueError("reporter_info must be an object")
            return dict(source["reporter_info"])
    return {}


def resolve_rendered_reporter_info(record=None, *, fallback=None):
    for source in (record, fallback):
        if not isinstance(source, dict):
            continue
        if _OBSOLETE_REPORTER_KEYS & source.keys():
            raise ValueError("Use reporter_info and rendered_reporter_info canonical fields")
        if "rendered_reporter_info" in source:
            if type(source["rendered_reporter_info"]) is not dict:
                raise ValueError("rendered_reporter_info must be an object")
            return dict(source["rendered_reporter_info"])
    return resolve_clean_reporter_info(record, fallback=fallback)
