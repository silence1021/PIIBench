"""Synthetic persona data models and manifest validation.

This package has no dependency on page generation, evaluation, defenses, or
sanitization. Consumers receive immutable, validated data objects.
"""

from .models import (
    NewsItem,
    Project,
    Publication,
    SyntheticPersona,
    SyntheticPersonaManifest,
    TeachingItem,
    iter_persona_text,
    load_persona_manifest,
)

__all__ = [
    "NewsItem",
    "Project",
    "Publication",
    "SyntheticPersona",
    "SyntheticPersonaManifest",
    "TeachingItem",
    "iter_persona_text",
    "load_persona_manifest",
]
