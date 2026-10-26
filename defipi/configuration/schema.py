"""Small, strict validation primitives; experiment rules belong to their modules."""

from dataclasses import dataclass
from math import isfinite
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def __str__(self):
        return f"{self.path or '<root>'}: {self.message}"


class ValidationError(ValueError):
    """All configuration problems found in one validation pass."""

    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("Invalid configuration:\n" + "\n".join(f"- {p}" for p in self.problems))


@dataclass(frozen=True)
class Field:
    """A field contract, without default values or type conversion.

    Use ``schema`` for a closed object, ``items`` for a list, and ``values``
    for an open dictionary whose values all obey the same contract. Conditional
    requirements and relationships between fields belong in module-owned rules.
    ``required=False`` permits absence; ``nullable=True`` separately permits null.
    """

    types: type | tuple[type, ...]
    required: bool = True
    nullable: bool = False
    choices: Sequence[Any] | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    schema: Mapping[str, "Field"] | None = None
    items: "Field | None" = None
    values: "Field | None" = None

    def __post_init__(self):
        types = self.types if isinstance(self.types, tuple) else (self.types,)
        if not types or any(not isinstance(t, type) for t in types):
            raise TypeError("Field.types must be a type or nonempty tuple of types")
        if self.schema is not None and self.values is not None:
            raise ValueError("Use either schema or values for a dictionary")
        if (self.schema is not None or self.values is not None) and dict not in types:
            raise ValueError("schema and values require a dict field")
        if self.items is not None and list not in types:
            raise ValueError("items requires a list field")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Field.minimum cannot exceed Field.maximum")


def _path(prefix, name):
    return f"{prefix}.{name}" if prefix else str(name)


def _validate_field(value, field, path, problems):
    if value is None:
        if not field.nullable:
            problems.append(Problem(path, "null is not allowed"))
        return
    types = field.types if isinstance(field.types, tuple) else (field.types,)
    # Exact types keep bool distinct from int, and preserve the original value.
    if type(value) not in types:
        expected = " or ".join(t.__name__ for t in types)
        problems.append(Problem(path, f"expected {expected}, got {type(value).__name__}"))
        return
    if field.choices is not None and not any(type(value) is type(choice) and value == choice for choice in field.choices):
        problems.append(Problem(path, f"must be one of {tuple(field.choices)!r}"))
    if type(value) in (int, float):
        if type(value) is float and not isfinite(value):
            problems.append(Problem(path, "must be finite"))
        else:
            if field.minimum is not None and value < field.minimum:
                problems.append(Problem(path, f"must be >= {field.minimum}"))
            if field.maximum is not None and value > field.maximum:
                problems.append(Problem(path, f"must be <= {field.maximum}"))
    if field.schema is not None and type(value) is dict:
        _validate_object(value, field.schema, path, problems)
    if field.values is not None and type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                problems.append(Problem(_path(path, repr(key)), "dictionary keys must be strings"))
            _validate_field(item, field.values, _path(path, key), problems)
    if field.items is not None and type(value) is list:
        for index, item in enumerate(value):
            _validate_field(item, field.items, f"{path}[{index}]", problems)


def _validate_object(data, fields, prefix, problems):
    for name in data:
        if name not in fields:
            problems.append(Problem(_path(prefix, name), "unknown field"))
    for name, field in fields.items():
        path = _path(prefix, name)
        if name not in data:
            if field.required:
                problems.append(Problem(path, "required field is missing"))
        else:
            _validate_field(data[name], field, path, problems)


def validate_config(data, fields: Mapping[str, Field], *, rules: Sequence[Callable] = (), prefix=""):
    """Validate a closed config object and return that same object.

    Rules receive ``(data, problems)`` and append :class:`Problem` instances.
    They run even when fields fail, so must guard any fields they inspect. Rules
    can express conditional requirements without adding defaults or side effects.
    Nested-object/list errors and rule failures are raised together.
    """
    problems = []
    if type(data) is not dict:
        problems.append(Problem(prefix, f"expected dict, got {type(data).__name__}"))
    else:
        _validate_object(data, fields, prefix, problems)
        for rule in rules:
            rule(data, problems)
    if problems:
        raise ValidationError(problems)
    return data
