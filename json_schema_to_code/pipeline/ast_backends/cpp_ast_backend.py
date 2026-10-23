"""
C++ AST-based code generation backend.

Generates a header of C++20 aggregate structs with nlohmann/json serialization from the
IR. Type model (same shape as the Swift backend):
- JSON objects -> ``struct``s with default member initializers and a defaulted ``operator==``.
- ``allOf`` inheritance -> base fields flattened into each struct.
- Discriminated polymorphic bases -> ``using Base = std::variant<Sub...>``, dispatched on the
  discriminator key in ``from_json``.
- Enums -> ``enum class`` with an explicit JSON mapping; unknown values throw.
- Nullable or non-required fields without a default -> ``std::optional``.
- Untyped values (schema ``object`` without properties, heterogeneous unions) -> ``nlohmann::json``.

The output is header-only and needs nlohmann/json (3.11+) on the include path.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..analyzer.ir_nodes import IR, ClassDef, FieldDef, TypeKind, TypeRef
from ..config import CodeGeneratorConfig
from .base import AstBackend
from .cpp_ast_nodes import (
    CppEnum,
    CppEnumMember,
    CppField,
    CppFieldKind,
    CppFile,
    CppStruct,
    CppVariant,
    CppVariantAlternative,
)
from .cpp_serializer import JSON, CppSerializer

# C++ keywords and alternative tokens that cannot be used as identifiers.
CPP_RESERVED_KEYWORDS = {
    "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand", "bitor", "bool", "break",
    "case", "catch", "char", "char8_t", "char16_t", "char32_t", "class", "compl", "concept",
    "const", "consteval", "constexpr", "constinit", "const_cast", "continue", "co_await",
    "co_return", "co_yield", "decltype", "default", "delete", "do", "double", "dynamic_cast",
    "else", "enum", "explicit", "export", "extern", "false", "float", "for", "friend", "goto",
    "if", "inline", "int", "long", "mutable", "namespace", "new", "noexcept", "not", "not_eq",
    "nullptr", "operator", "or", "or_eq", "private", "protected", "public", "register",
    "reinterpret_cast", "requires", "return", "short", "signed", "sizeof", "static",
    "static_assert", "static_cast", "struct", "switch", "template", "this", "thread_local",
    "throw", "true", "try", "typedef", "typeid", "typename", "union", "unsigned", "using",
    "virtual", "void", "volatile", "wchar_t", "while", "xor", "xor_eq",
}  # fmt: skip

# Standard header needed by each type or function the generated code may use.
STANDARD_INCLUDES = {
    "<cstdint>": "std::int64_t",
    "<map>": "std::map<",
    "<optional>": "std::optional<",
    "<stdexcept>": "std::invalid_argument",
    "<string>": "std::string",
    "<tuple>": "std::tuple<",
    "<variant>": "std::variant<",
    "<vector>": "std::vector<",
}

OPTIONAL_PREFIX = "std::optional<"


class CppAstBackend(AstBackend):
    """C++ code generation backend using a small C++ AST."""

    FILE_EXTENSION = "hpp"

    TYPE_MAP = {
        # JSON Schema primitive names
        "integer": "std::int64_t",
        "string": "std::string",
        "boolean": "bool",
        "number": "double",
        "null": "std::nullptr_t",
        "object": JSON,
        # Normalized/native spellings (base-class fields surface these, e.g. "int"/"bool")
        "int": "std::int64_t",
        "str": "std::string",
        "bool": "bool",
        "float": "double",
        "double": "double",
        "any": JSON,
        "Any": JSON,
    }

    def __init__(self, config: CodeGeneratorConfig):
        super().__init__(config)
        self.serializer = CppSerializer()
        # enum type name -> {json_value: enumerator}
        self.enum_value_to_member: dict[str, dict[Any, str]] = {}
        # struct name -> flattened fields in declaration order (for designated initializers)
        self.struct_fields: dict[str, list[FieldDef]] = {}
        self.type_aliases: dict[str, TypeRef | None] = {}

    # ------------------------------------------------------------------ generate

    def generate(self, ir: IR) -> str:
        """Generate a C++ header from the IR."""
        self.type_aliases = {alias.name: alias.target_type for alias in ir.type_aliases}
        self.enum_value_to_member = {}
        self.struct_fields = {}
        for class_def in ir.classes:
            if class_def.is_enum and class_def.enum_def:
                self.enum_value_to_member[class_def.name] = {value: self._enum_member_name(member) for member, value in class_def.enum_def.members.items()}
            elif not self._is_variant(class_def):
                self.struct_fields[class_def.name] = self._flatten_fields(class_def)

        file = CppFile(generation_comment=ir.generation_comment, namespace=self.config.cpp_namespace)
        nodes: dict[str, CppStruct | CppVariant] = {}
        dependencies: dict[str, set[str]] = {}
        for class_def in ir.classes:
            if class_def.is_enum:
                if class_def.enum_def:
                    file.enums.append(self._generate_enum(class_def))
            elif self._is_variant(class_def):
                nodes[class_def.name] = self._generate_variant(class_def)
                dependencies[class_def.name] = {name for name, _ in class_def.subclasses}
            else:
                nodes[class_def.name] = self._generate_struct(class_def)
                dependencies[class_def.name] = set().union(*(self._by_value_classes(f.type_ref) for f in self.struct_fields[class_def.name] if f.type_ref))
        file.types = [nodes[name] for name in self._dependency_order(list(nodes), dependencies)]

        # Includes depend on what the rendered code uses: render once to find out.
        body = self.serializer.serialize(file)
        file.includes = [include for include, marker in STANDARD_INCLUDES.items() if marker in body]
        file.includes.append(self.config.cpp_json_include)
        file.includes += self.config.cpp_additional_includes
        return self.serializer.serialize(file)

    @staticmethod
    def _is_variant(class_def: ClassDef) -> bool:
        return class_def.is_polymorphic_base and bool(class_def.subclasses)

    def _flatten_fields(self, class_def: ClassDef) -> list[FieldDef]:
        """Base fields + own fields; own fields override base fields by JSON key."""
        ordered: list[FieldDef] = []
        index_by_key: dict[str, int] = {}
        for field in class_def.base_fields + class_def.fields:
            if field.name in self.config.global_ignore_fields or not field.type_ref:
                continue
            if field.name in index_by_key:
                ordered[index_by_key[field.name]] = field
            else:
                index_by_key[field.name] = len(ordered)
                ordered.append(field)
        return ordered

    # -------------------------------------------------------------------- nodes

    def _generate_enum(self, class_def: ClassDef) -> CppEnum:
        enum_def = class_def.enum_def
        is_integer = enum_def.value_type == "integer"
        node = CppEnum(name=class_def.name, is_integer=is_integer)
        for member, value in enum_def.members.items():
            json_value = str(value) if is_integer else self._string_literal(str(value))
            node.members.append(CppEnumMember(name=self._enum_member_name(member), json_value=json_value))
        return node

    def _generate_variant(self, class_def: ClassDef) -> CppVariant:
        node = CppVariant(name=class_def.name, discriminator_key=class_def.discriminator_property or "type")
        for subtype_name, discriminator_value in class_def.subclasses:
            node.alternatives.append(CppVariantAlternative(concrete_type=subtype_name, discriminator_value=str(discriminator_value)))
        return node

    def _generate_struct(self, class_def: ClassDef) -> CppStruct:
        node = CppStruct(name=class_def.name)
        node.fields = [self._generate_field(field) for field in self.struct_fields[class_def.name]]
        return node

    def _generate_field(self, field: FieldDef) -> CppField:
        value_type, nullable = self._value_type(field.type_ref)
        initializer = None
        if field.has_default and field.default_value is not None:
            initializer = self.format_default_value(field.default_value, field.type_ref)

        # x-cpp-type: the client-provided type verbatim. A std::optional<...> override is
        # nullable; defaults are kept only for empty containers, which initialize as `{}`.
        override = field.type_ref.type_overrides.get("cpp")
        if override:
            nullable = override.startswith(OPTIONAL_PREFIX) and override.endswith(">")
            value_type = override[len(OPTIONAL_PREFIX) : -1] if nullable else override
            if initializer != "{}":
                initializer = None

        if nullable:
            kind = CppFieldKind.DEFAULTED_NULLABLE if initializer else (CppFieldKind.REQUIRED_NULLABLE if field.is_required else CppFieldKind.OPTIONAL)
        elif initializer is not None:
            kind = CppFieldKind.DEFAULTED
        elif field.is_required or field.is_const:
            kind = CppFieldKind.REQUIRED
        else:
            # Tolerant decoding: a non-required field without a default is optional
            # rather than a hard failure when absent.
            kind = CppFieldKind.OPTIONAL
            nullable = True

        return CppField(
            name=self._field_name(field),
            type_name=f"{OPTIONAL_PREFIX}{value_type}>" if nullable else value_type,
            value_type=value_type,
            json_key=field.original_name or field.name,
            kind=kind,
            initializer=initializer,
            comment=field.comment.strip().removeprefix("//").strip() if field.comment else None,
        )

    # ---------------------------------------------------------------- type xlate

    def translate_type(self, type_ref: TypeRef) -> str:
        value_type, nullable = self._value_type(type_ref)
        return f"{OPTIONAL_PREFIX}{value_type}>" if nullable else value_type

    def _value_type(self, type_ref: TypeRef) -> tuple[str, bool]:
        """The C++ type without its outer std::optional, and whether the value is nullable."""
        override = type_ref.type_overrides.get("cpp")
        if override:
            return override, False
        nullable = type_ref.is_nullable
        kind = type_ref.kind
        if kind == TypeKind.PRIMITIVE or kind == TypeKind.CONST:
            return self.TYPE_MAP.get(type_ref.name, type_ref.name), nullable
        if kind == TypeKind.CLASS:
            return type_ref.name, nullable
        if kind == TypeKind.ENUM:
            # Inline enum (no generated type) -> raw string, like the C# and Swift backends.
            return "std::string", nullable
        if kind == TypeKind.ARRAY:
            item = self.translate_type(type_ref.type_args[0]) if type_ref.type_args else JSON
            return f"std::vector<{item}>", nullable
        if kind == TypeKind.DICT:
            value = self.translate_type(type_ref.type_args[1]) if len(type_ref.type_args) == 2 else JSON
            return f"std::map<std::string, {value}>", nullable
        if kind == TypeKind.TUPLE:
            if not type_ref.type_args:
                return f"std::vector<{JSON}>", nullable
            return "std::tuple<" + ", ".join(self.translate_type(t) for t in type_ref.type_args) + ">", nullable
        if kind == TypeKind.OPTIONAL:
            inner, _ = self._value_type(type_ref.type_args[0]) if type_ref.type_args else (JSON, False)
            return inner, True
        if kind == TypeKind.UNION:
            non_null = [t for t in type_ref.type_args if not (t.kind == TypeKind.PRIMITIVE and t.name in ("null", "None"))]
            has_null = len(non_null) != len(type_ref.type_args)
            if len(non_null) == 1:
                inner, inner_nullable = self._value_type(non_null[0])
                return inner, nullable or has_null or inner_nullable
            # Heterogeneous union with no generated variant -> untyped JSON (null included).
            return JSON, False
        if kind == TypeKind.TYPE_ALIAS:
            target = self.type_aliases.get(type_ref.name)
            if target is not None:
                inner, inner_nullable = self._value_type(target)
                return inner, nullable or inner_nullable
        return JSON, False

    def _by_value_classes(self, type_ref: TypeRef, inside_vector: bool = False) -> set[str]:
        """Generated types this one holds by value, so they must be complete first.

        std::vector accepts an incomplete element type, which is what makes recursive
        schemas (a node with an array of child nodes) expressible.
        """
        if type_ref.type_overrides.get("cpp"):
            return set()
        if type_ref.kind == TypeKind.CLASS:
            return set() if inside_vector else {type_ref.name}
        if type_ref.kind == TypeKind.TYPE_ALIAS:
            target = self.type_aliases.get(type_ref.name)
            return self._by_value_classes(target, inside_vector) if target is not None else set()
        through_vector = type_ref.kind in (TypeKind.ARRAY,) or (type_ref.kind == TypeKind.TUPLE and not type_ref.type_args)
        names: set[str] = set()
        for arg in type_ref.type_args:
            names |= self._by_value_classes(arg, inside_vector or through_vector)
        return names

    @staticmethod
    def _dependency_order(names: list[str], dependencies: dict[str, set[str]]) -> list[str]:
        """Stable topological order: each type after the types it holds by value."""
        order: list[str] = []
        state: dict[str, str] = {}

        def visit(name: str, path: list[str]) -> None:
            if state.get(name) == "done":
                return
            if state.get(name) == "visiting":
                cycle = " -> ".join(path[path.index(name) :] + [name])
                raise ValueError(f"C++ cannot hold these types by value in a cycle: {cycle}. Use an array for the recursive reference or give it an x-cpp-type.")
            state[name] = "visiting"
            for dependency in sorted(dependencies.get(name, set()) & set(names), key=names.index):
                visit(dependency, path + [name])
            state[name] = "done"
            order.append(name)

        for name in names:
            visit(name, [])
        return order

    # ------------------------------------------------------------- default values

    def format_default_value(self, value: Any, type_ref: TypeRef | None) -> str:
        if value is None:
            return "std::nullopt"
        if type_ref is not None and self.translate_type(type_ref).removeprefix(OPTIONAL_PREFIX).removesuffix(">") == JSON:
            if value in ({}, []):
                return "{}"
            return f'{JSON}::parse(R"json({json.dumps(value, ensure_ascii=False)})json")'
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            enum_name = type_ref.name if type_ref else None
            if enum_name and value in self.enum_value_to_member.get(enum_name, {}):
                return f"{enum_name}::{self.enum_value_to_member[enum_name][value]}"
            return self._string_literal(value)
        if isinstance(value, (int, float)):
            enum_name = type_ref.name if type_ref else None
            if enum_name and value in self.enum_value_to_member.get(enum_name, {}):
                return f"{enum_name}::{self.enum_value_to_member[enum_name][value]}"
            return repr(value)
        if isinstance(value, list):
            if not value:
                return "{}"
            item_type = self._container_arg(type_ref, 0)
            return "{" + ", ".join(self.format_default_value(item, item_type) for item in value) + "}"
        if isinstance(value, dict):
            if type_ref is not None and type_ref.kind == TypeKind.CLASS:
                return self._designated_initializer(value, type_ref)
            if not value:
                return "{}"
            value_type = self._container_arg(type_ref, 1)
            items = [f"{{{self._string_literal(str(k))}, {self.format_default_value(v, value_type)}}}" for k, v in value.items()]
            return "{" + ", ".join(items) + "}"
        return str(value)

    def _container_arg(self, type_ref: TypeRef | None, index: int) -> TypeRef | None:
        if type_ref is None:
            return None
        if type_ref.kind == TypeKind.UNION:
            non_null = [t for t in type_ref.type_args if not (t.kind == TypeKind.PRIMITIVE and t.name in ("null", "None"))]
            return self._container_arg(non_null[0], index) if len(non_null) == 1 else None
        return type_ref.type_args[index] if len(type_ref.type_args) > index else None

    def _designated_initializer(self, value: dict, type_ref: TypeRef) -> str:
        fields = self.struct_fields.get(type_ref.name)
        if fields is None:
            raise ValueError(f"C++ cannot build a default for {type_ref.name}: only plain struct defaults are supported")
        items = []
        for field in fields:  # designators must follow declaration order
            key = field.original_name or field.name
            if key in value:
                items.append(f".{self._field_name(field)} = {self.format_default_value(value[key], field.type_ref)}")
        return f"{type_ref.name}{{" + ", ".join(items) + "}"

    # -------------------------------------------------------------------- helpers

    @staticmethod
    def _string_literal(text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    def _field_name(self, field: FieldDef) -> str:
        """The JSON key as a C++ identifier, so the struct reads like the payload."""
        name = re.sub(r"\W", "_", field.original_name or field.name)
        if name[:1].isdigit():
            name = f"_{name}"
        return f"{name}_" if name in CPP_RESERVED_KEYWORDS else name

    def _enum_member_name(self, member: str) -> str:
        # SCREAMING_SNAKE members (x-enum-members style) would PascalCase per letter.
        if re.fullmatch(r"[A-Z0-9_]+", member):
            member = member.lower()
        name = self._snake_to_pascal(member) or "Value"
        return f"V{name}" if name[0].isdigit() else name
