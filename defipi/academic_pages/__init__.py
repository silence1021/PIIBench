"""Generate paired, defense-free academic homepages from synthetic personas."""

from .backends import AcademicPagesBackend, AcademicTemplateBackend, AlFolioBackend
from .curated import CuratedScriptedBuilder, validate_build
from .generator import generate_academic_pages, prepare_templates

__all__ = [
    "AcademicPagesBackend",
    "AcademicTemplateBackend",
    "AlFolioBackend",
    "CuratedScriptedBuilder",
    "generate_academic_pages",
    "prepare_templates",
    "validate_build",
]
