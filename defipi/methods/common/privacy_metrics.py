"""Registered privacy metrics shared by evaluation and GEPA."""

from dataclasses import dataclass


COMPLEMENT_LEAKAGE_REWARD_V1 = "complement_leakage_v1"
STRICT_ZERO_LEAKAGE_REWARD_V1 = "strict_zero_leakage_v1"
_SINGLE_CHANNEL_REWARD_PROTOCOLS = {
    COMPLEMENT_LEAKAGE_REWARD_V1,
    STRICT_ZERO_LEAKAGE_REWARD_V1,
}


def _normalize_fields(values):
    fields = []
    for value in values or ():
        field = str(value or "").strip().lower()
        if field and field not in fields:
            fields.append(field)
    return tuple(fields)


@dataclass(frozen=True)
class PrivacyMetricDefinition:
    name: str
    contract_version: str
    protected_fields: tuple[str, ...]
    diagnostic_fields: tuple[str, ...]
    single_channel_reward_protocol: str

    def __post_init__(self):
        name = str(self.name or "").strip().lower()
        version = str(self.contract_version or "").strip()
        protected = _normalize_fields(self.protected_fields)
        diagnostic = _normalize_fields(self.diagnostic_fields)
        reward_protocol = str(
            self.single_channel_reward_protocol or ""
        ).strip()
        if not name:
            raise ValueError("privacy metric name must not be empty")
        if not version:
            raise ValueError(
                "privacy metric contract_version must not be empty"
            )
        if not protected:
            raise ValueError(
                "privacy metric protected_fields must not be empty"
            )
        overlap = sorted(set(protected) & set(diagnostic))
        if overlap:
            raise ValueError(
                "privacy metric protected_fields and diagnostic_fields "
                f"must be disjoint: {overlap}"
            )
        if reward_protocol not in _SINGLE_CHANNEL_REWARD_PROTOCOLS:
            raise ValueError(
                "unsupported single-channel reward protocol: "
                f"{self.single_channel_reward_protocol}"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "contract_version", version)
        object.__setattr__(self, "protected_fields", protected)
        object.__setattr__(self, "diagnostic_fields", diagnostic)
        object.__setattr__(
            self,
            "single_channel_reward_protocol",
            reward_protocol,
        )

    def to_dict(self):
        return {
            "profile": self.name,
            "contract_version": self.contract_version,
            "protected_fields": list(self.protected_fields),
            "diagnostic_fields": list(self.diagnostic_fields),
            "single_channel_reward_protocol": (
                self.single_channel_reward_protocol
            ),
        }


_PRIVACY_METRICS = {}


def register_privacy_metric(definition, *, replace=False):
    if not isinstance(definition, PrivacyMetricDefinition):
        raise TypeError(
            "definition must be a PrivacyMetricDefinition"
        )
    if definition.name in _PRIVACY_METRICS and not replace:
        raise ValueError(
            f"privacy metric already registered: {definition.name}"
        )
    _PRIVACY_METRICS[definition.name] = definition
    return definition


def get_privacy_metric(name):
    normalized = str(name or "").strip().lower()
    try:
        return _PRIVACY_METRICS[normalized]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported privacy metric profile: {name}"
        ) from exc


def privacy_metric_names():
    return tuple(_PRIVACY_METRICS)


def reward_from_protected_leakage(leakage_ratio, metric_definition):
    definition = (
        metric_definition
        if isinstance(metric_definition, PrivacyMetricDefinition)
        else get_privacy_metric(
            (metric_definition or {}).get("profile")
        )
    )
    leakage = max(0.0, min(1.0, float(leakage_ratio)))
    if (
        definition.single_channel_reward_protocol
        == STRICT_ZERO_LEAKAGE_REWARD_V1
    ):
        return 1.0 if leakage == 0.0 else 0.0
    return 1.0 - leakage


register_privacy_metric(
    PrivacyMetricDefinition(
        name="academic_contact_strict",
        contract_version="academic_contact_strict_metric_v1",
        protected_fields=("email", "phone", "address"),
        diagnostic_fields=("name",),
        single_channel_reward_protocol=STRICT_ZERO_LEAKAGE_REWARD_V1,
    )
)


register_privacy_metric(
    PrivacyMetricDefinition(
        name="name_gated_contact",
        contract_version="name-gated-contact-v1",
        protected_fields=("email", "phone", "address"),
        diagnostic_fields=("name",),
        single_channel_reward_protocol=COMPLEMENT_LEAKAGE_REWARD_V1,
    )
)

PRIVACY_METRIC_PROFILES = privacy_metric_names()
