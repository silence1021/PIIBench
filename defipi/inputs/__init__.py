"""Public input channels, independently selected from execution and defense."""
from . import html, url

INPUTS = {"html": html, "url": url}


def get_input(name):
    return INPUTS[name]
