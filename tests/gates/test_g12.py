"""Tests for G12 — Schema and Enum."""

import pytest
from pydantic import BaseModel, Field, ValidationError

from corpdisc.gates import check_g12_schema_enum


class ValidModel(BaseModel):
    """Valid Pydantic model for testing."""
    name: str = Field(min_length=1, max_length=100)
    value: int = Field(ge=0, le=100)


class TestG12SchemaEnum:
    """Tests for G12 — Schema and Enum."""

    def test_g12_valid_schema(self) -> None:
        """Valid schema should be allowed."""
        payload = ValidModel(name="Test", value=50)
        result = check_g12_schema_enum(payload)
        assert result.allowed is True

    def test_g12_invalid_name_too_short(self) -> None:
        """Name too short should be rejected with SCHEMA_ERROR."""
        # Use model_construct to bypass validation at construction
        payload = ValidModel.model_construct(name="", value=50)
        result = check_g12_schema_enum(payload)
        assert result.allowed is False
        assert result.reason == "SCHEMA_ERROR"
        assert "name" in result.detail

    def test_g12_invalid_value_too_high(self) -> None:
        """Value too high should be rejected with SCHEMA_ERROR."""
        payload = ValidModel.model_construct(name="Test", value=150)
        result = check_g12_schema_enum(payload)
        assert result.allowed is False
        assert result.reason == "SCHEMA_ERROR"
        assert "value" in result.detail
