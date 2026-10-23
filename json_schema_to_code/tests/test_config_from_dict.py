"""`CodeGeneratorConfig.from_dict`: the nested `output` and `formatter` blocks.

Both sub-configs are dataclasses, so the generic `setattr(config, k, v)`
fallback would happily replace them with the raw dict from the JSON config
file. `formatter` used to take exactly that path, which only bites later, in
`generate_to_file`, as an `AttributeError` on `config.formatter.enabled` —
hence the end-to-end test at the bottom.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from json_schema_to_code.pipeline import CodeGeneratorConfig, PipelineGenerator
from json_schema_to_code.pipeline.config import (
    ClassDefaultStrategy,
    FormatterConfig,
    MergeStrategy,
    OutputConfig,
    OutputMode,
)

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2019-09/schema",
    "$ref": "#/$defs/Point",
    "$defs": {
        "Point": {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        }
    },
}


def test_formatter_block_populates_the_dataclass():
    config = CodeGeneratorConfig.from_dict(
        {
            "formatter": {
                "enabled": False,
                "line_length": 120,
                "target_version": "py313",
                "string_normalization": False,
                "magic_trailing_comma": False,
            }
        }
    )

    assert isinstance(config.formatter, FormatterConfig)
    assert config.formatter.enabled is False
    assert config.formatter.line_length == 120
    assert config.formatter.target_version == "py313"
    assert config.formatter.string_normalization is False
    assert config.formatter.magic_trailing_comma is False


def test_formatter_block_is_a_partial_override():
    config = CodeGeneratorConfig.from_dict({"formatter": {"line_length": 88}})

    assert config.formatter.line_length == 88
    # Untouched keys keep their defaults rather than being reset.
    assert config.formatter.enabled is True
    assert config.formatter.magic_trailing_comma is True


def test_formatter_unknown_keys_are_ignored_with_a_warning():
    with pytest.warns(UserWarning, match="no_such_option"):
        config = CodeGeneratorConfig.from_dict({"formatter": {"line_length": 120, "no_such_option": "boom"}})

    assert config.formatter.line_length == 120
    assert not hasattr(config.formatter, "no_such_option")


def test_output_block_populates_every_field():
    config = CodeGeneratorConfig.from_dict(
        {
            "output": {
                "mode": "overwrite",
                "merge_strategy": "delete",
                "output_path": "generated/point.py",
                "validate_before_write": False,
            }
        }
    )

    assert isinstance(config.output, OutputConfig)
    assert config.output.mode is OutputMode.OVERWRITE
    assert config.output.merge_strategy is MergeStrategy.DELETE
    assert config.output.output_path == "generated/point.py"
    assert config.output.validate_before_write is False


def test_output_block_is_a_partial_override():
    config = CodeGeneratorConfig.from_dict({"output": {"output_path": "out.py"}})

    assert config.output.output_path == "out.py"
    assert config.output.mode is OutputMode.MERGE
    assert config.output.merge_strategy is MergeStrategy.ERROR
    assert config.output.validate_before_write is True


def test_top_level_keys_are_still_applied():
    with pytest.warns(UserWarning, match="unknown_top_level_key"):
        config = CodeGeneratorConfig.from_dict(
            {
                "use_inline_unions": True,
                "quoted_types_for_python": ["Point"],
                "formatter": {"line_length": 120},
                "unknown_top_level_key": "ignored",
            }
        )

    assert config.use_inline_unions is True
    assert config.quoted_types_for_python == ["Point"]
    assert config.formatter.line_length == 120
    assert not hasattr(config, "unknown_top_level_key")


def test_generate_to_file_accepts_a_json_config_with_a_formatter_block(tmp_path: Path):
    """The regression: a dict `config.formatter` blows up on `.enabled`."""
    config = CodeGeneratorConfig.from_dict({"formatter": {"line_length": 120}, "output": {"mode": "overwrite"}})

    output_path = tmp_path / "point.py"
    PipelineGenerator("Point", SCHEMA, config, "python").generate_to_file(output_path)

    assert "class Point" in output_path.read_text(encoding="utf-8")


def _fully_populated() -> CodeGeneratorConfig:
    """Every field set away from its default, nested blocks included."""
    config = CodeGeneratorConfig(
        ignore_classes=["Temp"],
        global_ignore_fields=["_internal"],
        order_classes=["Base", "Child"],
        ignoreSubClassOverrides=True,
        drop_min_max_items=True,
        use_array_of_super_type_for_variable_length_tuple=False,
        use_tuples=False,
        use_inline_unions=True,
        add_generation_comment=False,
        swift_conformances=["Decodable", "Sendable"],
        swift_nonisolated=True,
        cpp_namespace="montage::model",
        cpp_json_include='"json.hpp"',
        cpp_additional_includes=['"MediaTime.hpp"'],
        quoted_types_for_python=["Node"],
        use_future_annotations=False,
        exclude_default_value_from_json=True,
        class_default_strategy=ClassDefaultStrategy.CONSTRUCTIBLE,
        optional_field_helper_module="app.helpers",
        add_validation=True,
        external_ref_base_module="app.schemas",
        external_ref_schema_to_module={"quiz_schema": "app.quiz"},
        csharp_namespace="App.Models",
        csharp_additional_usings=["System.Linq"],
        schema_base_path="/schemas",
        output=OutputConfig(mode=OutputMode.OVERWRITE, merge_strategy=MergeStrategy.DELETE, output_path="out.py", validate_before_write=False),
        formatter=FormatterConfig(enabled=False, line_length=120, target_version="py313", string_normalization=False, magic_trailing_comma=False, sort_imports=False),
    )
    defaults = CodeGeneratorConfig()
    for f in fields(CodeGeneratorConfig):
        assert getattr(config, f.name) != getattr(defaults, f.name), f"{f.name} is still at its default -- extend _fully_populated"
    return config


def test_to_dict_covers_every_field_and_round_trips():
    config = _fully_populated()
    as_dict = config.to_dict()

    assert set(as_dict) == {f.name for f in fields(CodeGeneratorConfig)}
    # JSON-ready: enums as their values, nested configs as dicts
    assert as_dict["output"] == {"mode": "overwrite", "merge_strategy": "delete", "output_path": "out.py", "validate_before_write": False}
    assert set(as_dict["formatter"]) == {f.name for f in fields(FormatterConfig)}
    assert CodeGeneratorConfig.from_dict(as_dict) == config
