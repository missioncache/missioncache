"""Tests for helper functions - no I/O, no mocking."""

from pathlib import Path

import pytest

from mcp_orbit.errors import ValidationError
from mcp_orbit.helpers import _validate_path


class TestValidatePath:
    def test_empty_path(self):
        """Empty string raises ValidationError."""
        with pytest.raises(ValidationError, match="cannot be empty"):
            _validate_path("")

    def test_null_bytes(self):
        """Path with null bytes raises ValidationError."""
        with pytest.raises(ValidationError, match="null bytes"):
            _validate_path("/some/path\x00evil")

    def test_valid_path(self):
        """Valid path returns resolved Path object."""
        result = _validate_path("/tmp/test")
        assert isinstance(result, Path)
        assert result == Path("/tmp/test").resolve()

    def test_must_be_under_pass(self, tmp_path):
        """Path within required root passes validation."""
        child = tmp_path / "sub" / "file.txt"
        result = _validate_path(str(child), must_be_under=tmp_path)
        assert result == child.resolve()

    def test_must_be_under_fail(self, tmp_path):
        """Path outside required root raises ValidationError."""
        outside = Path("/tmp/outside_dir/file.txt")
        with pytest.raises(ValidationError, match="must be within"):
            _validate_path(str(outside), must_be_under=tmp_path)
