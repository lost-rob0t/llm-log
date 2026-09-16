from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import unquote

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from referencing.exceptions import Unresolvable

_MAX_SCHEMA_DEPTH = 64
_MAX_SCHEMA_NODES = 4096
_MAX_VALIDATION_ERRORS = 256
_LOCAL_REF_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")


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


def _bounded_walk(schema: Any) -> list[Any] | None:
    nodes = 0
    walked: list[Any] = []
    stack: list[tuple[Any, int]] = [(schema, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_SCHEMA_NODES or depth > _MAX_SCHEMA_DEPTH:
            return None
        walked.append(value)

        if isinstance(value, Mapping):
            for child in value.values():
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            for child in value:
                stack.append((child, depth + 1))
    return walked


def _decode_pointer_token(token: str) -> str:
    return unquote(token).replace("~1", "/").replace("~0", "~")


def _resolve_local_reference(schema: Any, ref: str, walked: list[Any]) -> bool:
    if ref == "#":
        return True
    if ref.startswith("#/"):
        current = schema
        for raw_token in ref[2:].split("/"):
            token = _decode_pointer_token(raw_token)
            if isinstance(current, Mapping):
                if token not in current:
                    return False
                current = current[token]
                continue
            if isinstance(current, list):
                try:
                    index = int(token)
                except ValueError:
                    return False
                if index < 0 or index >= len(current):
                    return False
                current = current[index]
                continue
            return False
        return True
    if ref.startswith("#"):
        anchor = unquote(ref[1:])
        if not anchor:
            return True
        return any(
            isinstance(value, Mapping)
            and (
                value.get("$anchor") == anchor
                or value.get("$dynamicAnchor") == anchor
            )
            for value in walked
        )
    return False


def _schema_is_bounded_local_and_resolvable(schema: Any) -> bool:
    walked = _bounded_walk(schema)
    if walked is None:
        return False

    for value in walked:
        if not isinstance(value, Mapping):
            continue
        for key in _LOCAL_REF_KEYS:
            ref = value.get(key)
            if ref is None:
                continue
            if not isinstance(ref, str) or not ref.startswith("#"):
                return False
            if not _resolve_local_reference(schema, ref, walked):
                return False
    return True


def schema_is_usable(schema: Mapping[str, Any]) -> bool:
    """Return whether a request-supplied schema is safe to treat as a contract.

    This performs only bounded local checks. It never resolves network references.
    A false result is request-contract evidence, not model-behavior evidence.
    """
    if not _schema_is_bounded_local_and_resolvable(schema):
        return False
    try:
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
    except SchemaError:
        return False
    return True


def validate_instance(schema: Mapping[str, Any], instance: Any) -> SchemaValidationResult:
    """Validate INSTANCE against one bounded request-supplied schema.

    Remote references are deliberately unsupported. Invalid, oversized, or
    unresolvable request schemas are returned as ``unusable`` rather than being
    blamed on model output.
    """
    if not schema_is_usable(schema):
        return SchemaValidationResult(state="unusable")

    try:
        validator_class = validator_for(schema)
        validator = validator_class(schema)
        violations = []
        for index, error in enumerate(validator.iter_errors(instance)):
            if index >= _MAX_VALIDATION_ERRORS:
                break
            violations.append(error)
    except Unresolvable:
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
