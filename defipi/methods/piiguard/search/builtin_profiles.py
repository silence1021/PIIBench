"""Built-in News and Academic GEPA profile adapters.

The generic mutation loop supplies the domain hook implementations; all
built-in profile identity, wording labels, and registration live here.
"""

from dataclasses import dataclass
from typing import Callable, Mapping

from defipi.methods.piiguard.search.profiles import GEPAProfileAdapter, register_gepa_profile_adapter


@dataclass(frozen=True)
class BuiltinGEPAProfileHooks:
    contract_version: str
    operator_tasks: Mapping[str, str]
    prompt_lines_builder: Callable
    validator: Callable
    validation_feedback: Callable
    normalizer: Callable
    seed_builder: Callable


def register_builtin_gepa_profiles(*, news, academic):
    register_gepa_profile_adapter(
        GEPAProfileAdapter(
            name="news",
            contract_version=news.contract_version,
            subject_label="reporter/newsroom",
            safe_contact_label="newsroom safe contact",
            safe_route_label="newsroom route",
            field_labels={
                "name": "reporter name",
                "phone": "direct phone",
                "email": "direct email",
                "address": "street address",
            },
            operator_tasks=news.operator_tasks,
            prompt_lines_builder=news.prompt_lines_builder,
            validator=news.validator,
            validation_feedback=news.validation_feedback,
            normalizer=news.normalizer,
            seed_builder=news.seed_builder,
        ),
        replace=True,
    )
    register_gepa_profile_adapter(
        GEPAProfileAdapter(
            name="academic",
            contract_version=academic.contract_version,
            subject_label="professor/institution",
            safe_contact_label="institutional safe contact",
            safe_route_label="institutional route",
            field_labels={
                "name": "professor's name",
                "phone": "phone number",
                "email": "direct email",
                "address": "office address",
            },
            operator_tasks=academic.operator_tasks,
            prompt_lines_builder=academic.prompt_lines_builder,
            validator=academic.validator,
            validation_feedback=academic.validation_feedback,
            normalizer=academic.normalizer,
            seed_builder=academic.seed_builder,
        ),
        replace=True,
    )
