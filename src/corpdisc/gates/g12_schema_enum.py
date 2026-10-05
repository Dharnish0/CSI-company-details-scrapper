"""G12 — Schema and Enum: validate Pydantic schemas (ARCHITECTURE.md 22.15).

Validate schema, enums, required fields, bounded fields, cross-field constraints.
Reuses existing Pydantic validators on the payload.

Uses Pydantic's model_validate to trigger all validators.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from corpdisc.gates import GateResult

name: str = "G12_SCHEMA_ENUM"
description: str = "Validate Pydantic schemas, enums, required fields, bounded fields, and cross-field constraints."


def check_g12_schema_enum(payload: BaseModel) -> GateResult:
    """Validate Pydantic schema.

    Args:
        payload: A Pydantic model instance to validate.

    Returns:
        GateResult with allowed=True if validation passes, or
        allowed=False with reason="SCHEMA_ERROR" and detail containing error message.
    """
    try:
        # Re-validate to trigger all validators
        payload.model_validate(payload.model_dump())
        return GateResult(True)
    except ValidationError as exc:
        # Format error message
        errors = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err.get("msg", "validation error")
            errors.append(f"{loc}: {msg}")
        detail = "; ".join(errors) if errors else str(exc)
        return GateResult(False, "SCHEMA_ERROR", detail)
