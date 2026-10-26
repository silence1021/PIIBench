"""URL input: let the selected API or agent own page access and tool returns."""
REPRESENTATION = "backend_native"
RUNTIME_FORMAT = "backend_native"


def validate_attack(attack):
    if attack == "filter":
        raise ValueError("URL conditions have no external Filter")


def normalize(config):
    representation = config.get("observation_representation", REPRESENTATION)
    if representation != REPRESENTATION:
        raise ValueError("URL observation is backend_native; record actual tool returns in the trace")
    if config.get("observation_format") not in (None, "backend_native"):
        raise ValueError("URL cannot use a static observation_format")
    config["observation_format"] = RUNTIME_FORMAT
    return representation


def observation(page, html):
    return page["url"], None
