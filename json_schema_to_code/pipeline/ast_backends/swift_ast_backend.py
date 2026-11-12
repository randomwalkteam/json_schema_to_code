"""
Swift AST-based code generation backend.

Generates Swift ``Codable`` structs/enums from the IR. Type model (see plan):
- JSON objects -> ``struct``s conforming to ``Codable``.
- ``allOf`` inheritance -> base fields flattened into each struct.
- Discriminated polymorphic bases -> a generated ``enum`` (``SwiftPolyEnum``) that
  switches on the discriminator in ``init(from:)``.
- String enums -> raw-value ``Codable`` enums (Swift maps raw value <-> JSON for free).

- Unions of primitive types (``string | integer``) -> a generated enum with one case
  per member (``IntOrString``).

The generated code references a JSON value type (``swift_any_type``, ``AnyCodable`` by
default) for schema ``object`` / untyped ``Any``; the consuming Swift module must
provide it once. With ``swift_policy_decoding`` it also calls the module's
``policyContainer(keyedBy:)`` (see CLAUDE.md).
"""

from __future__ import annotations

from typing import Any

from ..analyzer.ir_nodes import IR, ClassDef, FieldDef, TypeKind, TypeRef
from ..config import CodeGeneratorConfig
from .base import AstBackend
from .swift_ast_nodes import (
    SwiftEnum,
    SwiftEnumCase,
    SwiftFile,
    SwiftPolyEnum,
    SwiftPolyEnumCase,
    SwiftProperty,
    SwiftStruct,
    SwiftUnionEnum,
    SwiftUnionEnumCase,
)
from .swift_serializer import SwiftSerializer

# Swift reserved words that must be backtick-escaped when used as identifiers.
SWIFT_RESERVED_KEYWORDS = {
    "associatedtype",
    "class",
    "deinit",
    "enum",
    "extension",
    "fileprivate",
    "func",
    "import",
    "init",
    "inout",
    "internal",
    "let",
    "open",
    "operator",
    "private",
    "precedencegroup",
    "protocol",
    "public",
    "rethrows",
    "static",
    "struct",
    "subscript",
    "typealias",
    "var",
    "break",
    "case",
    "catch",
    "continue",
    "default",
    "defer",
    "do",
    "else",
    "fallthrough",
    "for",
    "guard",
    "if",
    "in",
    "repeat",
    "return",
    "throw",
    "switch",
    "where",
    "while",
    "as",
    "any",
    "await",
    "false",
    "is",
    "nil",
    "self",
    "Self",
    "super",
    "throws",
    "true",
    "try",
    "Type",
    "Protocol",
}


class SwiftAstBackend(AstBackend):
    """Swift code generation backend using a small Swift AST."""

    FILE_EXTENSION = "swift"

    TYPE_MAP = {
        # JSON Schema primitive names
        "integer": "Int",
        "string": "String",
        "boolean": "Bool",
        "number": "Double",
        "null": "Void",
        # Normalized/native spellings (base-class fields surface these, e.g. "int"/"bool")
        "int": "Int",
        "str": "String",
        "bool": "Bool",
        "float": "Double",
        "double": "Double",
    }

    # Decoding order of a primitive union: a JSON number that is an integer decodes as
    # Int before Double; Bool and String never overlap the others.
    UNION_MEMBER_ORDER = ["Bool", "Int", "Double", "String"]

    # Literal a lenient policy decoder falls back to for a bad required primitive.
    ZERO_VALUES = {"String": '""', "Int": "0", "Double": "0", "Bool": "false"}

    def __init__(self, config: CodeGeneratorConfig):
        super().__init__(config)
        self.serializer = SwiftSerializer()
        self.enum_names: set[str] = set()
        # enum type name -> {json_value: swift_case_name}
        self.enum_value_to_member: dict[str, dict[Any, str]] = {}
        # class name -> {json_key: FieldDef} (for struct-typed default initializers)
        self.class_field_map: dict[str, dict[str, FieldDef]] = {}
        # generated primitive unions, by name (filled while translating types)
        self.union_enums: dict[str, SwiftUnionEnum] = {}
        self.any_type = config.swift_any_type

    # ------------------------------------------------------------------ generate

    def generate(self, ir: IR) -> str:
        """Generate Swift code from the IR."""
        self.enum_names = {c.name for c in ir.classes if c.is_enum}
        self.enum_value_to_member = {}
        self.class_field_map = {}
        self.union_enums = {}
        for class_def in ir.classes:
            if class_def.is_enum and class_def.enum_def:
                self.enum_value_to_member[class_def.name] = {json_value: self._enum_case_name(member) for member, json_value in class_def.enum_def.members.items()}
            elif not (class_def.is_polymorphic_base and class_def.subclasses):
                self.class_field_map[class_def.name] = {f.original_name or f.name: f for f in (class_def.base_fields + class_def.fields)}

        file = SwiftFile()
        file.generation_comment = ir.generation_comment

        for class_def in ir.classes:
            if class_def.is_enum:
                node = self._generate_enum(class_def)
                if node:
                    file.enums.append(node)
            elif class_def.is_polymorphic_base and class_def.subclasses:
                file.poly_enums.append(self._generate_poly_enum(class_def))
            else:
                file.structs.append(self._generate_struct(class_def))
        file.union_enums = [u for name, u in sorted(self.union_enums.items()) if name not in self.config.ignore_classes]

        return self.serializer.serialize(file)

    # -------------------------------------------------------------------- nodes

    def _generate_enum(self, class_def: ClassDef) -> SwiftEnum | None:
        enum_def = class_def.enum_def
        if not enum_def:
            return None
        raw_type = "Int" if enum_def.value_type == "integer" else "String"
        node = SwiftEnum(
            name=class_def.name,
            raw_type=raw_type,
            conformances=list(self.config.swift_conformances),
            nonisolated=self.config.swift_nonisolated,
        )
        for member, json_value in enum_def.members.items():
            node.cases.append(
                SwiftEnumCase(
                    name=self._enum_case_name(member),
                    raw_value=str(json_value),
                )
            )
        return node

    def _generate_poly_enum(self, class_def: ClassDef) -> SwiftPolyEnum:
        disc_key = class_def.discriminator_property or "type"
        node = SwiftPolyEnum(
            name=class_def.name,
            discriminator_key=disc_key,
            conformances=list(self.config.swift_conformances),
            nonisolated=self.config.swift_nonisolated,
            unknown_payload_type=self.any_type if self.config.swift_policy_decoding else None,
        )
        prefix = self._common_word_prefix([name for name, _ in class_def.subclasses])
        for subtype_name, disc_value in class_def.subclasses:
            node.cases.append(
                SwiftPolyEnumCase(
                    name=self._swift_ident(self._lower_camel(subtype_name[len(prefix) :])),
                    concrete_type=subtype_name,
                    discriminator_value=str(disc_value),
                )
            )
        return node

    def _generate_struct(self, class_def: ClassDef) -> SwiftStruct:
        node = SwiftStruct(
            name=class_def.name,
            conformances=list(self.config.swift_conformances),
            nonisolated=self.config.swift_nonisolated,
            policy_decoding=self.config.swift_policy_decoding,
        )

        # Flatten base fields + own fields; own fields override base fields by JSON key.
        ordered: list[FieldDef] = []
        index_by_key: dict[str, int] = {}
        for field in class_def.base_fields + class_def.fields:
            if field.name in self.config.global_ignore_fields:
                continue
            key = field.name
            if key in index_by_key:
                ordered[index_by_key[key]] = field
            else:
                index_by_key[key] = len(ordered)
                ordered.append(field)

        needs_decoder = node.policy_decoding and self._conforms_to("Decodable")
        for field in ordered:
            prop = self._generate_property(field)
            if prop is None:
                continue
            node.properties.append(prop)
            if prop.default_value is not None:
                needs_decoder = True
            if node.policy_decoding and prop.is_required and prop.is_optional:
                node.needs_custom_encoder = self._conforms_to("Encodable")

        node.needs_custom_decoder = needs_decoder
        return node

    def _conforms_to(self, protocol: str) -> bool:
        return protocol in self.config.swift_conformances or "Codable" in self.config.swift_conformances

    def _generate_property(self, field: FieldDef) -> SwiftProperty | None:
        if not field.type_ref:
            return None
        swift_type = self.translate_type(field.type_ref)
        is_optional = field.type_ref.is_nullable
        default = None
        if field.has_default:
            default = self.format_default_value(field.default_value, field.type_ref)

        # x-swift-type on the property: the client-provided type verbatim. A trailing "?"
        # makes it optional (decoded via decodeIfPresent); defaults are kept only for
        # empty container literals, which format the same for any element type.
        override = field.type_ref.type_overrides.get("swift")
        if override:
            swift_type = override
            is_optional = swift_type.endswith("?")
            if is_optional:
                default = None
            elif default is not None and default not in ("[]", "[:]"):
                default = None

        # Bare `AnyCodable` (schema object/Any) has no compile-time literal default. Drop the
        # default and make the property optional so a missing key decodes to nil (tolerant).
        base_type = swift_type[:-1] if swift_type.endswith("?") else swift_type
        if base_type == self.any_type and default is not None:
            default = None
            is_optional = True
            if not swift_type.endswith("?"):
                swift_type += "?"

        # Tolerant decoding: a non-required field with neither a default nor
        # nullability must not hard-fail the whole payload when absent — decode
        # it as optional instead of `try container.decode`.
        if default is None and not is_optional and not field.is_required and not field.is_const:
            is_optional = True
            swift_type += "?"

        base_type = swift_type[:-1] if swift_type.endswith("?") else swift_type
        element_type = None
        if not override and field.type_ref.kind == TypeKind.ARRAY and base_type.startswith("[") and ":" not in base_type:
            element_type = base_type[1:-1]

        return SwiftProperty(
            name=self._swift_ident(self._lower_camel(field.name)),
            type_name=swift_type,
            json_key=field.original_name or field.name,
            is_optional=is_optional,
            default_value=default,
            comment=field.comment,
            is_required=field.is_required,
            is_const=field.is_const and default is not None,
            element_type=element_type,
            fallback=None if is_optional else self._fallback(base_type, default),
        )

    def _fallback(self, swift_type: str, default: str | None) -> str | None:
        """Literal a lenient decoder uses for a bad required value: the schema default,
        else the zero value of a primitive or container. None for a generated type: the
        policy container then decodes it from an empty object."""
        if default is not None:
            return default
        if swift_type in self.ZERO_VALUES:
            return self.ZERO_VALUES[swift_type]
        if swift_type.startswith("["):
            return "[:]" if ":" in swift_type else "[]"
        return None

    # ---------------------------------------------------------------- type xlate

    def translate_type(self, type_ref: TypeRef) -> str:
        result = self._translate_type_inner(type_ref)
        if type_ref.is_nullable and not result.endswith("?"):
            result = f"{result}?"
        return result

    def _translate_type_inner(self, type_ref: TypeRef) -> str:
        override = type_ref.type_overrides.get("swift")
        if override:
            return override
        kind = type_ref.kind
        if kind == TypeKind.PRIMITIVE:
            if type_ref.name in ("object", "any", "Any"):
                return self.any_type
            return self.TYPE_MAP.get(type_ref.name, type_ref.name)
        if kind == TypeKind.CLASS:
            return type_ref.name
        if kind == TypeKind.ANY:
            return self.any_type
        if kind == TypeKind.DICT:
            if len(type_ref.type_args) == 2:
                key = self.translate_type(type_ref.type_args[0])
                value = self.translate_type(type_ref.type_args[1])
                return f"[{key}: {value}]"
            return f"[String: {self.any_type}]"
        if kind == TypeKind.ARRAY:
            if type_ref.type_args:
                return f"[{self.translate_type(type_ref.type_args[0])}]"
            return f"[{self.any_type}]"
        if kind == TypeKind.TUPLE:
            # Swift tuples are not Codable; represent as a heterogeneous array.
            return f"[{self.any_type}]"
        if kind == TypeKind.UNION:
            types = [self.translate_type(t) for t in type_ref.type_args]
            non_null = [t for t in types if t not in ("Void", "null")]
            has_null = len(types) != len(non_null)
            if len(non_null) == 1:
                base = non_null[0]
                if has_null and not base.endswith("?"):
                    return f"{base}?"
                return base
            union = self._primitive_union(non_null)
            if union:
                return f"{union}?" if has_null else union
            # Heterogeneous union with no generated poly-enum -> the JSON value type.
            return self.any_type
        if kind == TypeKind.CONST:
            return self.TYPE_MAP.get(type_ref.name, type_ref.name)
        if kind == TypeKind.ENUM:
            # Inline enum (no generated type) -> raw String, like the C# backend.
            return "String"
        return self.any_type

    def _primitive_union(self, members: list[str]) -> str | None:
        """Name of the generated enum for a union of primitive types, registering it."""
        distinct = sorted(set(members))
        if len(distinct) < 2 or any(m not in self.UNION_MEMBER_ORDER for m in distinct):
            return None
        name = "Or".join(distinct)
        if name not in self.union_enums:
            self.union_enums[name] = SwiftUnionEnum(
                name=name,
                cases=[SwiftUnionEnumCase(name=self._swift_ident(t.lower()), type_name=t) for t in self.UNION_MEMBER_ORDER if t in distinct],
                conformances=list(self.config.swift_conformances),
                nonisolated=self.config.swift_nonisolated,
            )
        return name

    # ------------------------------------------------------------- default values

    def format_default_value(self, value: Any, type_ref: TypeRef | None) -> str:
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            type_name = type_ref.name if type_ref else None
            if type_name and type_name in self.enum_value_to_member:
                member = self.enum_value_to_member[type_name].get(value)
                if member:
                    return f".{member}"
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return self._format_list_default(value, type_ref)
        if isinstance(value, dict):
            return self._format_dict_default(value, type_ref)
        return str(value)

    def _format_list_default(self, value: list, type_ref: TypeRef | None) -> str:
        if not value:
            return "[]"
        element_type = type_ref.type_args[0] if type_ref and type_ref.type_args else None
        items = [self.format_default_value(item, element_type) for item in value]
        return "[" + ", ".join(items) + "]"

    def _format_dict_default(self, value: dict, type_ref: TypeRef | None) -> str:
        if type_ref and type_ref.kind == TypeKind.CLASS:
            return self._format_struct_initializer(value, type_ref)
        if not value:
            return "[:]"
        value_type = type_ref.type_args[1] if type_ref and len(type_ref.type_args) == 2 else None
        items = []
        for k, v in value.items():
            key_str = f'"{k}"' if isinstance(k, str) else str(k)
            items.append(f"{key_str}: {self.format_default_value(v, value_type)}")
        return "[" + ", ".join(items) + "]"

    def _format_struct_initializer(self, value: dict, type_ref: TypeRef) -> str:
        base_name = self.translate_type(type_ref).rstrip("?")
        field_map = self.class_field_map.get(type_ref.name, {})
        items = []
        for k, v in value.items():
            field_def = field_map.get(k)
            label = self._swift_ident(self._lower_camel(k))
            field_type = field_def.type_ref if field_def else None
            items.append(f"{label}: {self.format_default_value(v, field_type)}")
        return f"{base_name}(" + ", ".join(items) + ")"

    # -------------------------------------------------------------------- helpers

    def _enum_case_name(self, member: str) -> str:
        # SCREAMING_SNAKE members (x-enum-members style, e.g. TYPE_CHANGE) lower-camel
        # per letter otherwise ("aDD"); normalize them to snake first -> typeChange.
        import re

        if re.fullmatch(r"[A-Z0-9_]+", member):
            member = member.lower()
        return self._swift_ident(self._lower_camel(member))

    @staticmethod
    def _common_word_prefix(names: list[str]) -> str:
        """The leading CamelCase words every name shares, when each name keeps at least
        one word after it (``DHMChatInfo``, ``DHMError`` -> ``DHM``)."""
        import re

        if len(names) < 2:
            return ""
        split = [re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", n) for n in names]
        if any("".join(words) != n for words, n in zip(split, names)):
            return ""
        prefix: list[str] = []
        for words in zip(*split):
            if len(set(words)) != 1:
                break
            prefix.append(words[0])
        if any(len(words) <= len(prefix) for words in split):
            prefix = prefix[: min(len(w) for w in split) - 1]
        return "".join(prefix)

    def _lower_camel(self, text: str) -> str:
        pascal = self._snake_to_pascal(text)
        return pascal[0].lower() + pascal[1:] if pascal else pascal

    def _swift_ident(self, name: str) -> str:
        return f"`{name}`" if name in SWIFT_RESERVED_KEYWORDS else name
