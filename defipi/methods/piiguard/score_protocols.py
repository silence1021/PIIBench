"""Check supplied metric definitions against the registered privacy metric."""

from defipi.methods.common.privacy_metrics import (
    PrivacyMetricDefinition,
    get_privacy_metric,
)


def normalize_metric_definition(value):
    if isinstance(value, PrivacyMetricDefinition):
        return value
    if not isinstance(value, dict):
        raise TypeError(
            "metric_definition must be a PrivacyMetricDefinition or mapping"
        )
    definition = get_privacy_metric(value.get("profile"))
    expected = definition.to_dict()
    for field in (
        "contract_version",
        "protected_fields",
        "diagnostic_fields",
        "single_channel_reward_protocol",
    ):
        if field not in value:
            continue
        actual = value[field]
        if field.endswith("_fields"):
            actual = list(actual or [])
        if actual != expected[field]:
            raise ValueError(
                f"metric_definition.{field} does not match registered metric"
            )
    return definition
