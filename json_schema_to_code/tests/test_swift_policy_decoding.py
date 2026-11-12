"""Swift policy decoding (``swift_policy_decoding``): every decoder reads through the
consuming module's ``policyContainer``, primitive unions become generated enums, and
discriminated enums keep unknown subtypes.

Miniature of the dh-server message schema the mimir-ios app generates from.
"""

from json_schema_to_code.pipeline.config import CodeGeneratorConfig
from json_schema_to_code.pipeline.generator import PipelineGenerator
from json_schema_to_code.tests.test_swift_ast_roundtrip import _swiftc_typecheck

MESSAGES_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2019-09/schema",
    "$defs": {
        "Message": {
            "type": "object",
            "discriminator": {"propertyName": "type"},
            "properties": {
                "type": {"type": "string"},
                "id": {"type": "array", "items": [{"type": "string"}, {"type": ["string", "integer"]}], "minItems": 1, "maxItems": 2},
            },
            "required": ["type", "id"],
        },
        "MsgChatInfo": {
            "allOf": [
                {"$ref": "#/$defs/Message"},
                {"type": "object", "properties": {"type": {"type": "string", "const": "MsgChatInfo"}}, "required": ["type"]},
            ]
        },
        "MsgProfile": {
            "allOf": [
                {"$ref": "#/$defs/Message"},
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "MsgProfile"},
                        "age": {"type": ["string", "null"]},
                        "view": {"$ref": "#/$defs/View"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "parts": {"type": "array", "items": {"$ref": "#/$defs/View"}},
                        "nickname": {"type": "string"},
                        "volume": {"type": "number", "default": 1},
                        "payload": {"type": "object"},
                    },
                    "required": ["type", "age", "view", "parts"],
                },
            ]
        },
        "View": {
            "type": "object",
            "properties": {"language": {"type": "string", "default": "en-US"}, "count": {"type": "integer"}},
            "required": ["language", "count"],
        },
    },
}


def _generate(**overrides) -> str:
    config = CodeGeneratorConfig()
    config.swift_conformances = ["Codable", "Hashable", "Sendable"]
    config.swift_nonisolated = True
    config.swift_any_type = "JSONValue"
    config.swift_policy_decoding = True
    config.add_generation_comment = False
    for key, value in overrides.items():
        setattr(config, key, value)
    return PipelineGenerator("Messages", MESSAGES_SCHEMA, config, "swift").generate()


def test_primitive_union_becomes_a_generated_enum():
    code = _generate()
    assert "nonisolated enum IntOrString: Codable, Hashable, Sendable {" in code
    assert "case int(Int)" in code
    assert "case string(String)" in code
    # Int is tried before String, so a JSON integer never becomes .string.
    assert code.index("container.decode(Int.self)") < code.index("container.decode(String.self)")
    assert "let id: [IntOrString]" in code


def test_primitive_union_without_policy_decoding():
    # The enum does not depend on policy decoding: it replaces the opaque JSON value.
    code = _generate(swift_policy_decoding=False, swift_any_type="AnyCodable")
    assert "enum IntOrString" in code
    assert "AnyCodable" in code  # the untyped `payload` object


def test_ignored_union_enum_is_not_generated():
    # A module that already defines the enum leaves it out.
    code = _generate(ignore_classes=["IntOrString"])
    assert "enum IntOrString" not in code
    assert "let id: [IntOrString]" in code


def test_discriminated_enum_cases_drop_the_shared_prefix():
    code = _generate()
    assert "nonisolated enum Message: Codable, Hashable, Sendable {" in code
    assert "case chatInfo(MsgChatInfo)" in code
    assert "case profile(MsgProfile)" in code


def test_discriminated_enum_keeps_unknown_subtypes():
    code = _generate()
    assert "case unknown(type: String, payload: JSONValue)" in code
    assert "self = .unknown(type: discriminator, payload: try JSONValue(from: decoder))" in code
    assert "case .unknown(_, let payload): try payload.encode(to: encoder)" in code
    assert "Unknown discriminator" not in code


def test_decoder_reads_through_the_policy_container():
    code = _generate()
    assert "let container = try decoder.policyContainer(keyedBy: CodingKeys.self)" in code
    assert "try decoder.container(keyedBy: CodingKeys.self)" not in code


def test_discriminator_constant_is_a_let_and_never_decoded():
    code = _generate()
    assert 'let type: String = "MsgProfile"' in code
    assert "self.type =" not in code


def test_decode_calls_follow_the_schema():
    code = _generate()
    # Required: a primitive falls back to its zero value or schema default, a
    # generated type to the value decoded from an empty object.
    assert "self.count = try container.decodeRequired(Int.self, forKey: .count, fallback: 0)" in code
    assert 'self.language = try container.decodeRequired(String.self, forKey: .language, fallback: "en-US")' in code
    assert "self.view = try container.decodeRequired(View.self, forKey: .view)" in code
    # Required but nullable: the key must be there, null is a value.
    assert "self.age = try container.decodeRequired(String?.self, forKey: .age, fallback: nil)" in code
    # Arrays decode element by element.
    assert "self.id = try container.decodeRequiredArray(IntOrString.self, forKey: .id)" in code
    assert "self.parts = try container.decodeRequiredArray(View.self, forKey: .parts)" in code
    assert "self.tags = try container.decodeOptionalArray(String.self, forKey: .tags)" in code
    # Optional, with and without a schema default.
    assert "self.nickname = try container.decodeOptional(String.self, forKey: .nickname)" in code
    assert "self.volume = try container.decodeOptional(Double.self, forKey: .volume, default: 1)" in code
    assert "self.payload = try container.decodeOptional(JSONValue.self, forKey: .payload)" in code


def test_property_keywords():
    code = _generate()
    assert "let age: String?" in code  # required: the memberwise init asks for it
    assert "let view: View" in code
    assert "var nickname: String?" in code  # optional: the memberwise argument defaults to nil
    assert "var volume: Double = 1" in code


def test_required_nullable_property_encodes_null():
    code = _generate()
    assert "try container.encode(age, forKey: .age)" in code
    assert "try container.encodeIfPresent(nickname, forKey: .nickname)" in code
    assert "try container.encode(type, forKey: .type)" in code
    # A struct without a required nullable property keeps the synthesized encoder.
    view = code[code.index("nonisolated struct View") :]
    assert "func encode(to encoder: Encoder)" not in view


# The API the generated code calls, as a consuming module provides it (the mimir-ios
# app's PolicyDecoding.swift, without its build-configuration default).
POLICY_CONTAINER = """
import Foundation

enum DecodingPolicy: Sendable { case strict, lenient }

extension Decoder {
    func policyContainer<Key: CodingKey>(keyedBy type: Key.Type) throws -> PolicyDecodingContainer<Key> {
        PolicyDecodingContainer(base: try container(keyedBy: type), policy: .lenient)
    }
}

struct PolicyDecodingContainer<Key: CodingKey> {
    let base: KeyedDecodingContainer<Key>
    let policy: DecodingPolicy

    func decodeRequired<T: Decodable>(_ type: T.Type, forKey key: Key, fallback: @autoclosure () -> T) throws -> T {
        policy == .strict ? try base.decode(type, forKey: key) : ((try? base.decode(type, forKey: key)) ?? fallback())
    }
    func decodeRequired<T: Decodable>(_ type: T.Type, forKey key: Key) throws -> T {
        try base.decode(type, forKey: key)
    }
    func decodeOptional<T: Decodable>(_ type: T.Type, forKey key: Key) throws -> T? {
        try base.decodeIfPresent(type, forKey: key)
    }
    func decodeOptional<T: Decodable>(_ type: T.Type, forKey key: Key, default value: @autoclosure () -> T) throws -> T {
        try decodeOptional(type, forKey: key) ?? value()
    }
    func decodeRequiredArray<T: Decodable>(_ type: T.Type, forKey key: Key, fallback: @autoclosure () -> [T] = []) throws -> [T] {
        try base.decode([T].self, forKey: key)
    }
    func decodeOptionalArray<T: Decodable>(_ type: T.Type, forKey key: Key) throws -> [T]? {
        try base.decodeIfPresent([T].self, forKey: key)
    }
    func decodeOptionalArray<T: Decodable>(_ type: T.Type, forKey key: Key, default value: @autoclosure () -> [T]) throws -> [T] {
        try decodeOptionalArray(type, forKey: key) ?? value()
    }
}

enum JSONValue: Codable, Hashable, Sendable {
    case string(String)
    case null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        self = c.decodeNil() ? .null : .string(try c.decode(String.self))
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
}
"""


def test_generated_swift_typechecks_against_the_policy_container():
    _swiftc_typecheck(POLICY_CONTAINER, _generate())
