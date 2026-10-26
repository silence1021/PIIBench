"""Profile adapters for the GEPA structured-fragment pipeline."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping


class MutationRequestBudgetExceeded(RuntimeError):
    """Raised before an outbound mutation request would exceed its budget."""


@dataclass(frozen=True)
class GEPAProfileAdapter:
    name: str
    contract_version: str
    subject_label: str
    safe_contact_label: str
    safe_route_label: str
    field_labels: Mapping[str, str]
    operator_tasks: Mapping[str, str]
    prompt_lines_builder: Callable
    validator: Callable
    validation_feedback: Callable
    normalizer: Callable
    seed_builder: Callable

    def __post_init__(self):
        name = str(self.name or "").strip().lower()
        version = str(self.contract_version or "").strip()
        if not name:
            raise ValueError("GEPA profile adapter name must not be empty")
        if not version:
            raise ValueError(
                "GEPA profile adapter contract_version must not be empty"
            )
        for field in (
            "prompt_lines_builder",
            "validator",
            "validation_feedback",
            "normalizer",
            "seed_builder",
        ):
            if not callable(getattr(self, field)):
                raise TypeError(f"GEPA profile adapter {field} must be callable")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "contract_version", version)
        object.__setattr__(
            self,
            "field_labels",
            MappingProxyType(dict(self.field_labels or {})),
        )
        object.__setattr__(
            self,
            "operator_tasks",
            MappingProxyType(dict(self.operator_tasks or {})),
        )


_PROFILE_ADAPTERS = {}


def register_gepa_profile_adapter(adapter, *, replace=False):
    if not isinstance(adapter, GEPAProfileAdapter):
        raise TypeError("adapter must be a GEPAProfileAdapter")
    name = adapter.name
    if name in _PROFILE_ADAPTERS and not replace:
        raise ValueError(f"GEPA profile adapter already registered: {name}")
    _PROFILE_ADAPTERS[name] = adapter
    return adapter


def get_gepa_profile_adapter(name):
    normalized = str(name or "").strip().lower()
    try:
        return _PROFILE_ADAPTERS[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported GEPA profile: {name}") from exc


def gepa_profile_names():
    return tuple(_PROFILE_ADAPTERS)
