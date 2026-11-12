#!/usr/bin/env python3

import pytest

from json_schema_to_code.cli_utils import reconstruct_command_line


def get_click_command():
    """Helper to get Click command for testing"""
    try:
        from json_schema_to_code.json_schema_to_code import json_schema_to_code

        return json_schema_to_code
    except ImportError:
        return None


class TestCliUtils:
    """Test cases for CLI utilities"""

    def test_reconstruct_command_line_without_context(self):
        """Test command reconstruction without active Click context (fallback)"""
        click_cmd = get_click_command()
        if not click_cmd:
            pytest.skip("Click command not available")

        # Since there's no active Click context in tests, this should return fallback
        result = reconstruct_command_line(click_cmd)
        assert result == "json_schema_to_code"

    def test_reconstruct_command_line_function_exists(self):
        """Test that the function exists and is callable"""
        click_cmd = get_click_command()
        if not click_cmd:
            pytest.skip("Click command not available")

        # Should not raise an exception
        result = reconstruct_command_line(click_cmd)
        assert isinstance(result, str)
        assert "json_schema_to_code" in result


def test_header_names_files_the_same_on_first_and_later_runs(tmp_path):
    """The header written into a new output file equals the one a regeneration writes:
    file paths show as file names, whether or not the file existed."""
    from click.testing import CliRunner

    from json_schema_to_code.json_schema_to_code import json_schema_to_code

    schema = tmp_path / "schemas" / "point.json"
    schema.parent.mkdir()
    schema.write_text('{"$defs": {"Point": {"type": "object", "properties": {"x": {"type": "integer"}}}}}')
    output = tmp_path / "out" / "Point.swift"
    output.parent.mkdir()
    runner = CliRunner()
    headers = []
    for _ in range(2):
        result = runner.invoke(json_schema_to_code, [str(schema), str(output), "-l", "swift"])
        assert result.exit_code == 0, result.output
        headers.append(output.read_text().splitlines()[0])
    assert headers[0] == headers[1]
    assert headers[0].endswith(": json_schema_to_code point.json Point.swift --language swift")


if __name__ == "__main__":
    pytest.main([__file__])
