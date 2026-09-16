from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from referencing.exceptions import Unresolvable

_MAX_SCHEMA_DEPTH = 64
_MAX_SCHEMA_NODES = 4096
_MAX_VALIDATION_ERRORS = 256


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    validator: str
    instance_path: str


@dataclass(frozen=True, slots=True)
class SchemaValidationResult:
    state: str
    violation: SchemaViolation | None = None


def _json_pointer(path: object) -> str:
    parts: list[str] = []
    for raw in path:  # type: ignore[union-attr]
        value = str(raw).replace("~", "~0").replace("/", "~1")
        parts.append(value)
    return "" if not parts else "/" + "/".join(parts)


def _schema_is_bounded_and_local(schema: Any) -> bool:
    nodes = 0
    stack: list[tuple[Any, int]] = [(schema, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_SCHEMA_NODES or depth > _MAX_SCHEMA_DEPTH:
            return False

        if isinstance(value, Mapping):
            ref = value.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                return False
            for child in value.values():
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            for child in value:
                stack.append((child, depth + 1))
    return True


def validate_instance(schema: Mapping[str, Any], instance: Any) -> SchemaValidationResult:
    """Validate INSTANCE against one bounded request-supplied schema.

    Remote references are deliberately unsupported. Invalid, oversized, or
    unresolvable request schemas are returned as ``unusable`` rather than being
    blamed on model output.
    """
    if not _schema_is_bounded_and_local(schema):
        return SchemaValidationResult(state="unusable")

    try:
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        validator = validator_class(schema)
        violations = []
        for index, error in enumerate(validator.iter_errors(instance)):
            if index >= _MAX_VALIDATION_ERRORS:
                break
            violations.append(error)
    except (SchemaError, Unresolvable):
        return SchemaValidationResult(state="unusable")

    if not violations:
        return SchemaValidationResult(state="valid")

    error = min(
        violations,
        key=lambda item: (
            tuple(str(part) for part in item.absolute_path),
            str(item.validator or "unknown"),
        ),
    )
    validator_name = str(error.validator or "unknown")
    return SchemaValidationResult(
        state="violation",
        violation=SchemaViolation(
            validator=validator_name,
            instance_path=_json_pointer(error.absolute_path),
        ),
    )
