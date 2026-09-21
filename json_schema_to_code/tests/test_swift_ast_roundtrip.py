"""
Swift backend tests: generation, tree-sitter merge roundtrip, and (when a Swift
toolchain is present) a real ``swiftc -typecheck`` of the generated output.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from json_schema_to_code.pipeline import CodeGeneratorConfig, PipelineGenerator
from json_schema_to_code.pipeline.analyzer.ir_nodes import TypeKind, TypeRef
from json_schema_to_code.pipeline.ast_backends.swift_ast_backend import SwiftAstBackend
from json_schema_to_code.pipeline.config import MergeStrategy
from json_schema_to_code.pipeline.merger import SwiftAstMerger

# Minimal AnyCodable shim so generated code referencing it can be type-checked.
ANY_CODABLE = """
import Foundation

struct AnyCodable: Codable {
    let value: Any
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let v = try? c.decode(Bool.self) { value = v }
        else if let v = try? c.decode(Int.self) { value = v }
        else if let v = try? c.decode(Double.self) { value = v }
        else if let v = try? c.decode(String.self) { value = v }
        else if let v = try? c.decode([AnyCodable].self) { value = v }
        else if let v = try? c.decode([String: AnyCodable].self) { value = v }
        else { value = () }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch value {
        case let v as Bool: try c.encode(v)
        case let v as Int: try c.encode(v)
        case let v as Double: try c.encode(v)
        case let v as String: try c.encode(v)
        case let v as [AnyCodable]: try c.encode(v)
        case let v as [String: AnyCodable]: try c.encode(v)
        default: try c.encodeNil()
        }
    }
}
"""

BASIC_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "definitions": {
        "Color": {"type": "string", "enum": ["red", "green", "blue_ish"]},
        "TestClass": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "is_active": {"type": "boolean", "default": True},
                "nickname": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                "links": {"type": "object", "additionalProperties": {"type": "integer"}},
                "color": {"$ref": "#/definitions/Color", "default": "red"},
            },
            "required": ["id", "links"],
        },
    },
}

POLY_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "definitions": {
        "ImageWidget": {
            "type": "object",
            "properties": {"type": {"const": "image"}, "url": {"type": "string"}},
            "required": ["type", "url"],
        },
        "TextWidget": {
            "type": "object",
            "properties": {"type": {"const": "text"}, "text": {"type": "string"}},
            "required": ["type", "text"],
        },
        "Widget": {
            "oneOf": [{"$ref": "#/definitions/ImageWidget"}, {"$ref": "#/definitions/TextWidget"}],
            "discriminator": {"propertyName": "type"},
        },
    },
}


def _gen(schema, name, comment=False):
    cfg = CodeGeneratorConfig()
    cfg.use_inline_unions = True
    cfg.add_generation_comment = comment
    return PipelineGenerator(name, schema, cfg, "swift").generate()


def _swiftc_typecheck(*sources: str) -> None:
    """Type-check Swift sources; skip if no swiftc toolchain is available."""
    if shutil.which("swiftc") is None:
        pytest.skip("swiftc not available")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "AnyCodable.swift").write_text(ANY_CODABLE)
        for i, src in enumerate(sources):
            (d / f"Source{i}.swift").write_text(src)
        files = [str(p) for p in d.glob("*.swift")]
        result = subprocess.run(
            ["swiftc", "-typecheck", "-swift-version", "6", *files],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"swiftc failed:\n{result.stderr}"


def test_basic_struct_generates_expected_swift():
    code = _gen(BASIC_SCHEMA, "TestClass")
    assert "struct TestClass: Codable {" in code
    assert "var isActive: Bool = true" in code
    assert "let nickname: String?" in code
    assert 'case isActive = "is_active"' in code
    assert "decodeIfPresent(Bool.self, forKey: .isActive) ?? true" in code
    assert "enum Color: String, Codable {" in code


def test_discriminated_union_generates_enum():
    code = _gen(POLY_SCHEMA, "Widget")
    assert "enum Widget: Codable {" in code
    assert "case imageWidget(ImageWidget)" in code
    assert 'case "image": self = .imageWidget(try ImageWidget(from: decoder))' in code
    assert code.count("init(from decoder: Decoder)") >= 1


def test_generated_swift_typechecks():
    _swiftc_typecheck(_gen(BASIC_SCHEMA, "TestClass"), _gen(POLY_SCHEMA, "Widget"))


def test_merge_preserves_custom_code():
    generated = _gen(BASIC_SCHEMA, "TestClass")

    # Simulate user edits: custom import, custom member, custom extension, helper, marked section.
    insert_member = '\n    func describe() -> String { return "id=\\(id)" }\n'
    existing = generated.replace("import Foundation", "import Foundation\nimport Combine", 1)
    existing = existing.replace(
        "    enum CodingKeys: String, CodingKey {",
        insert_member + "    enum CodingKeys: String, CodingKey {",
        1,
    )
    existing += (
        "\nextension TestClass: Identifiable {\n    var customId: Int { id }\n}\n" "\nstruct Helper {\n    let x: Int\n}\n" "\n// CUSTOM CODE START\nlet globalConstant = 42\n// CUSTOM CODE END\n"
    )

    merged = SwiftAstMerger().merge_files(generated, existing, MergeStrategy.MERGE)

    assert "func describe()" in merged
    assert "extension TestClass: Identifiable {" in merged
    assert "struct Helper {" in merged
    assert "import Combine" in merged
    assert "globalConstant = 42" in merged
    # The generator-owned init(from:) extension must not be duplicated.
    assert merged.count("init(from decoder: Decoder)") == 1
    _swiftc_typecheck(merged)


def test_merge_preserves_custom_code_after_non_ascii_text():
    """Regression: tree-sitter offsets are UTF-8 bytes. Slicing the str with them
    shifted every custom declaration after a multi-byte character, turning
    ``nonisolated struct`` into ``ated struct`` and failing the merge."""
    generated = _gen(BASIC_SCHEMA, "TestClass")
    existing = generated.replace("import Foundation", "import Foundation\n\n/// Keys — bounded × 3 octaves; flats → sharps.", 1)
    existing += "\n/// Pitch class — sharp spelling.\n" 'nonisolated struct Helper: Hashable, Sendable {\n    let label = "é"\n}\n'

    merged = SwiftAstMerger().merge_files(generated, existing, MergeStrategy.MERGE)

    assert 'nonisolated struct Helper: Hashable, Sendable {\n    let label = "é"\n}' in merged
    _swiftc_typecheck(merged)


def test_native_primitive_spellings_map_to_swift():
    """Base-class fields surface native spellings (int/bool/float/str), not JSON Schema names."""
    backend = SwiftAstBackend(CodeGeneratorConfig())
    assert backend.translate_type(TypeRef(kind=TypeKind.PRIMITIVE, name="int")) == "Int"
    assert backend.translate_type(TypeRef(kind=TypeKind.PRIMITIVE, name="bool")) == "Bool"
    assert backend.translate_type(TypeRef(kind=TypeKind.PRIMITIVE, name="float")) == "Double"
    assert backend.translate_type(TypeRef(kind=TypeKind.PRIMITIVE, name="str")) == "String"
    # JSON Schema spellings still work too.
    assert backend.translate_type(TypeRef(kind=TypeKind.PRIMITIVE, name="integer")) == "Int"
    assert backend.translate_type(TypeRef(kind=TypeKind.PRIMITIVE, name="boolean")) == "Bool"


def test_anycodable_field_with_default_is_optional_without_literal():
    """A bare object/Any field with a default has no Swift literal: make it optional, drop default."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "definitions": {
            "TestClass": {
                "type": "object",
                "properties": {
                    "extra": {"type": "object", "default": {}},
                },
            }
        },
    }
    code = _gen(schema, "TestClass")
    assert "var extra: AnyCodable = [:]" not in code  # would not compile
    assert "extra: AnyCodable?" in code
    _swiftc_typecheck(code)


def test_merge_preserves_attributed_imports():
    """An attributed import survives regeneration.

    A generated file that lands in a TEST target has to reach the app module's
    internal types, which needs `@testable import`. Matching only a bare
    `import ` prefix dropped it on every merge, so the file stopped compiling.
    """
    generated = _gen(BASIC_SCHEMA, "TestClass")

    for attributed in ("@testable import HostApp", "@_exported import Shared"):
        existing = generated.replace("import Foundation", f"import Foundation\n{attributed}", 1)
        merged = SwiftAstMerger().merge_files(generated, existing, MergeStrategy.MERGE)
        assert attributed in merged, f"{attributed} was dropped by the merge"


def test_merge_preserves_import_kinds_and_places_them_after_the_generated_imports():
    """`import struct Foundation.Date` names Foundation.Date, not Foundation: it is a
    custom import and must survive; every custom import lands right after the last
    generated one, whatever precedes its name."""
    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    generated = PipelineGenerator(
        "Card", {"$defs": {"Card": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}}, "$ref": "#/$defs/Card"}, config, "swift"
    ).generate()
    existing = generated.replace("import Foundation", "import Foundation\n@testable import HostApp\nimport struct Foundation.Date", 1)

    merged = SwiftAstMerger().merge_files(generated, existing, MergeStrategy.MERGE)

    lines = merged.splitlines()
    assert lines[lines.index("import Foundation") + 1 :][:2] == ["@testable import HostApp", "import struct Foundation.Date"]
    assert merged.count("import Foundation\n") == 1
