"""Saved HTML input: preserve source bytes and filter before model execution."""
REPRESENTATION = "html"
RUNTIME_FORMAT = "html"
FILTER_STAGE = "html_before_render"


def validate_attack(attack):
    if attack == "url_prompt":
        raise ValueError("url_prompt requires web_injection_channel=url")


def normalize(config):
    representation = config.get("observation_representation", REPRESENTATION)
    if representation != REPRESENTATION or config.get("observation_format") not in (None, "html"):
        raise ValueError("Public HTML evaluation requires saved HTML input; AX rendering belongs to optimization")
    config["observation_format"] = "html"
    return representation


def observation(page, html):
    return html, None
