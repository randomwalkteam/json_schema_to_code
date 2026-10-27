"""
Tests for external base class import generation.

When a schema uses allOf with a $ref to an external schema, the generated
Python code should include the appropriate import for the base class.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from json_schema_to_code.pipeline import CodeGeneratorConfig, PipelineGenerator

TEST_DATA_DIR = Path(__file__).with_name("test_data") / "pipeline" / "external_base_class"
ALLOF_BASE_DIR = Path(__file__).with_name("test_data") / "pipeline" / "allof_external_base"
COMPLEX_TYPES_DIR = Path(__file__).with_name("test_data") / "pipeline" / "allof_complex_types"


def load_schema(name: str, directory: Path = TEST_DATA_DIR) -> dict:
    """Load a schema from a test data directory."""
    with open(directory / name) as f:
        return json.load(f)


def extract_imports(code: str) -> list[tuple[str, list[str]]]:
    """Extract import statements from Python code.

    Returns a list of (module, names) tuples.
    """
    tree = ast.parse(code)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [alias.name for alias in node.names]
            imports.append((node.module, names))
    return imports


def test_external_base_class_generates_import():
    """Test that external base class generates proper import."""
    schema = load_schema("child_schema.json")

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.external_ref_base_module = "explayn_dh_agent.barbara.db.app_object_definitions"

    gen = PipelineGenerator("QuizData", schema, config, "python")
    code = gen.generate()

    # Parse the code to extract imports
    imports = extract_imports(code)

    # Find imports from the external module
    external_imports = [(module, names) for module, names in imports if "base_dataclass" in module]

    # Should have imports for ActivityProblem, ActivityState, ActivityData
    imported_names = set()
    for module, names in external_imports:
        imported_names.update(names)

    # QuizProblem extends ActivityProblem, QuizState extends ActivityState,
    # QuizData extends ActivityData - all should be imported
    assert "ActivityProblem" in imported_names, f"ActivityProblem should be imported. Found imports: {external_imports}"
    assert "ActivityState" in imported_names, f"ActivityState should be imported. Found imports: {external_imports}"
    assert "ActivityData" in imported_names, f"ActivityData should be imported. Found imports: {external_imports}"


def test_external_base_class_generates_valid_python():
    """Test that generated code with external base classes is valid Python."""
    schema = load_schema("child_schema.json")

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.external_ref_base_module = "explayn_dh_agent.barbara.db.app_object_definitions"

    gen = PipelineGenerator("QuizData", schema, config, "python")
    code = gen.generate()

    # Should be valid Python syntax
    ast.parse(code)


def test_no_external_import_without_config():
    """Test that no external imports are generated without external_ref_base_module."""
    schema = load_schema("child_schema.json")

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    # external_ref_base_module is NOT set

    gen = PipelineGenerator("QuizData", schema, config, "python")
    code = gen.generate()

    # Parse the code to extract imports
    imports = extract_imports(code)

    # Should not have any imports from external modules
    external_imports = [(module, names) for module, names in imports if "base_dataclass" in module or "app_object" in module]

    assert len(external_imports) == 0, f"Should not have external imports without config. Found: {external_imports}"


def test_allof_external_base_csharp_constructor_includes_inherited_fields():
    """When an external base class uses allOf (e.g. ActivityUIData -> UIData),
    the C# constructor of the child class must include the inherited fields
    from the full allOf chain as base(...) call parameters.
    """
    schema = load_schema("child_schema.json", ALLOF_BASE_DIR)

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.csharp_namespace = "Test"
    config.schema_base_path = str(ALLOF_BASE_DIR)

    gen = PipelineGenerator("ClassifyUIData", schema, config, "cs")
    code = gen.generate()

    # The constructor should include inherited fields from ActivityUIData
    # (question, hint, is_complete) plus own fields (buckets, items)
    assert "string question" in code, f"Constructor should include inherited 'question' param.\nGenerated:\n{code}"
    assert "string hint" in code, f"Constructor should include inherited 'hint' param.\nGenerated:\n{code}"
    assert "bool is_complete" in code, f"Constructor should include inherited 'is_complete' param.\nGenerated:\n{code}"
    assert "base(" in code, f"Constructor should call base(...).\nGenerated:\n{code}"


def test_allof_external_base_python_generates_valid_code():
    """When an external base uses allOf, the generated Python code should still be valid."""
    schema = load_schema("child_schema.json", ALLOF_BASE_DIR)

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.schema_base_path = str(ALLOF_BASE_DIR)

    gen = PipelineGenerator("ClassifyUIData", schema, config, "python")
    code = gen.generate()

    ast.parse(code)


@pytest.mark.parametrize(
    "expected_fragment,description",
    [
        ("List<string> items", "array with string items -> List<string>"),
        ("Dictionary<string, int> links", "object with additionalProperties -> Dictionary<string, int>"),
        ("object image", "$ref|null anyOf -> object"),
        ("object action_template", "$ref without type -> object"),
        ("List<object> tags", "array without items type -> List<object>"),
        ("int? nullable_count", "nullable integer -> int?"),
    ],
    ids=lambda d: d if isinstance(d, str) and " " in d else None,
)
def test_allof_complex_types_csharp(expected_fragment, description):
    """External base class properties with complex types produce correct C# types."""
    schema = load_schema("child_schema.json", COMPLEX_TYPES_DIR)

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.csharp_namespace = "Test"
    config.schema_base_path = str(COMPLEX_TYPES_DIR)

    gen = PipelineGenerator("ChildUIData", schema, config, "cs")
    code = gen.generate()

    assert expected_fragment in code, f"{description}\n" f"Expected fragment: {expected_fragment!r}\n" f"Generated:\n{code}"


def test_allof_complex_types_csharp_no_bare_any():
    """C# output should never contain the literal 'any' type."""
    schema = load_schema("child_schema.json", COMPLEX_TYPES_DIR)

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.csharp_namespace = "Test"
    config.schema_base_path = str(COMPLEX_TYPES_DIR)

    gen = PipelineGenerator("ChildUIData", schema, config, "cs")
    code = gen.generate()

    import re

    matches = re.findall(r"\bany\b", code)
    assert not matches, f"C# code should not contain 'any' type. Found in:\n{code}"


PASSTHROUGH_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "definitions": {
        "BaseUIData": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "hint": {"type": "string"},
                "is_complete": {"type": "boolean"},
            },
            "required": ["question", "hint", "is_complete"],
        },
        "ChildUIData": {
            "allOf": [
                {"$ref": "#/definitions/BaseUIData"},
                {"type": "object", "properties": {}},
            ]
        },
        "ChildWithFields": {
            "allOf": [
                {"$ref": "#/definitions/BaseUIData"},
                {
                    "type": "object",
                    "properties": {"extra": {"type": "string"}},
                    "required": ["extra"],
                },
            ]
        },
    },
}

DISCRIMINATOR_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "definitions": {
        "Constraint": {
            "type": "object",
            "properties": {"type": {"type": "string"}},
            "required": ["type"],
        },
        "ConstraintFixed": {
            "allOf": [
                {"$ref": "#/definitions/Constraint"},
                {
                    "type": "object",
                    "properties": {
                        "type": {"const": "fixed"},
                        "value": {"type": "number"},
                    },
                    "required": ["value"],
                },
            ]
        },
        "ConstraintEmpty": {
            "allOf": [
                {"$ref": "#/definitions/Constraint"},
                {
                    "type": "object",
                    "properties": {"type": {"const": "empty"}},
                },
            ]
        },
    },
}


def test_csharp_skips_passthrough_constructor():
    """Subclass that adds no fields and no const overrides should have no constructor."""
    config = CodeGeneratorConfig()
    config.add_generation_comment = False

    gen = PipelineGenerator("Test", PASSTHROUGH_SCHEMA, config, "cs")
    code = gen.generate()

    lines = code.split("\n")
    in_child = False
    child_body_lines = []
    for line in lines:
        if "class ChildUIData" in line:
            in_child = True
            continue
        if in_child:
            child_body_lines.append(line)
            if line.strip() == "}" and len([x for x in child_body_lines if "{" in x]) <= len([x for x in child_body_lines if "}" in x]):
                break

    child_body = "\n".join(child_body_lines)
    assert "ChildUIData(" not in child_body, f"Pass-through subclass should not have a constructor.\nChild body:\n{child_body}"


def test_csharp_keeps_constructor_with_own_fields():
    """Subclass that adds its own fields should keep the constructor."""
    config = CodeGeneratorConfig()
    config.add_generation_comment = False

    gen = PipelineGenerator("Test", PASSTHROUGH_SCHEMA, config, "cs")
    code = gen.generate()

    assert "ChildWithFields(" in code, f"Subclass with own fields should have a constructor.\nGenerated:\n{code}"
    assert "base(" in code, f"Subclass constructor should call base(...).\nGenerated:\n{code}"


def test_csharp_keeps_constructor_with_discriminator_override():
    """Subclass that overrides a base field as const should keep the constructor."""
    config = CodeGeneratorConfig()
    config.add_generation_comment = False

    gen = PipelineGenerator("Test", DISCRIMINATOR_SCHEMA, config, "cs")
    code = gen.generate()

    assert "ConstraintFixed(" in code, f"Subclass with own fields + const override should have a constructor.\nGenerated:\n{code}"
    assert "ConstraintEmpty(" in code, f"Subclass with const override (no own fields) should have a constructor.\nGenerated:\n{code}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


CHAIN_DIR = Path(__file__).with_name("test_data") / "pipeline" / "allof_external_chain"


def test_required_travels_a_two_hop_external_allof_chain():
    """`required` is a constraint on the composed object: `ChildData` narrows `problem`
    two schema files away from the `ActivityData` that requires it, and must not get a
    default for it (the activity Data classes of explayn_main are exactly this shape)."""
    schema = load_schema("child_schema.json", CHAIN_DIR)

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.schema_base_path = str(CHAIN_DIR)
    config.external_ref_base_module = "generated_chain"

    code = PipelineGenerator("ChildData", schema, config, "python").generate()
    ast.parse(code)
    child = code.split("class ChildData")[1]
    assert re.search(r"^\s+problem: ChildProblem\s*$", child, re.M), code
    assert "problem: ChildProblem = " not in child, code


def test_a_constructor_default_travels_a_two_hop_external_allof_chain():
    """`ActivityData.state` carries `x-python-default: {}` two schema files up;
    `MidData` narrows `state` without restating the default and `ChildData` narrows
    it again — both must get the default, rebuilt for the narrowed type (the
    activity Data classes of explayn_main: ActivityData -> QuizData -> StatementQuizData)."""
    schema = load_schema("child_schema.json", CHAIN_DIR)

    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.schema_base_path = str(CHAIN_DIR)
    config.external_ref_base_module = "generated_chain"

    code = PipelineGenerator("ChildData", schema, config, "python").generate()
    ast.parse(code)
    child = code.split("class ChildData")[1]
    assert "state: ChildState = field(default_factory=lambda: ChildState())" in child, code
    assert re.search(r"^\s+problem: ChildProblem\s*$", child, re.M), code


OVERRIDDEN_EXTERNAL_UNION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2019-09/schema",
    "$ref": "#/$defs/Exercise",
    "$defs": {
        "Exercise": {
            "type": "object",
            "properties": {
                # The schema accepts the drawing or a reference to one; Python only
                # ever sees the resolved drawing.
                "drawing": {
                    "oneOf": [
                        {"$ref": "/graphics/drawing_schema#/$defs/Drawing"},
                        {"$ref": "/graphics/drawing_schema#/$defs/ObjectRef"},
                    ],
                    "x-python-type": "Drawing",
                },
                "call": {"$ref": "/graphics/drawing_schema#/$defs/DrawingCall"},
            },
            "required": ["drawing", "call"],
        },
    },
}


def test_an_overridden_type_imports_only_the_classes_it_names():
    """`x-python-type: "Drawing"` over `oneOf [Drawing, ObjectRef]`: the annotation
    names Drawing, so ObjectRef must not be imported (an unused import a linter's fix
    strips, which the next regeneration writes back), while Drawing and the other
    field's DrawingCall still are."""
    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    config.external_ref_base_module = "generated"

    code = PipelineGenerator("Exercise", OVERRIDDEN_EXTERNAL_UNION_SCHEMA, config, "python").generate()
    module = "generated.graphics.drawing_dataclass"
    imported = {name for mod, names in extract_imports(code) if mod == module for name in names}
    assert imported == {"Drawing", "DrawingCall"}, code
    assert re.search(r"^\s+drawing: Drawing\s*$", code, re.M), code
