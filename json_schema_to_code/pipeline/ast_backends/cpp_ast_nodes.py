"""
C++ AST node definitions.

These nodes represent the structure of a C++ header for code generation. They are
deliberately "dumb" data holders; all generation logic lives in ``cpp_ast_backend.py``
and the rendering lives in ``cpp_serializer.py``.

The C++ type model mirrors the Swift one: JSON Schema objects become aggregate
``struct``s, ``allOf`` inheritance is flattened into each struct, and discriminated
polymorphic bases become a ``std::variant`` alias dispatched on the discriminator key.
JSON (de)serialization uses nlohmann/json ``to_json`` / ``from_json`` free functions,
found by argument-dependent lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CppFieldKind(Enum):
    """How a field is read from and written to JSON."""

    REQUIRED = "required"  # key must be present
    DEFAULTED = "defaulted"  # absent key keeps the member's default initializer
    OPTIONAL = "optional"  # std::optional: absent (or null) key -> std::nullopt, omitted when empty
    REQUIRED_NULLABLE = "required_nullable"  # std::optional: key must be present, null <-> std::nullopt
    DEFAULTED_NULLABLE = "defaulted_nullable"  # std::optional with a non-null default


@dataclass
class CppNode:
    """Base class for all C++ AST nodes."""

    pass


@dataclass
class CppField(CppNode):
    """A data member of a struct.

    ``name`` is the C++ identifier (the JSON key, sanitized and keyword-escaped).
    ``type_name`` is the full member type; for nullable kinds it is ``std::optional<value_type>``.
    ``initializer`` is the rendered brace/equal initializer (without ``=``), or ``None``.
    """

    name: str = ""
    type_name: str = ""
    value_type: str = ""  # type inside std::optional for nullable kinds, else == type_name
    json_key: str = ""
    kind: CppFieldKind = CppFieldKind.REQUIRED
    initializer: str | None = None
    comment: str | None = None


@dataclass
class CppEnumMember(CppNode):
    name: str = ""  # C++ enumerator (PascalCase)
    json_value: str = ""  # rendered JSON literal: "\"video\"" or "3"


@dataclass
class CppEnum(CppNode):
    """A scoped enum with explicit JSON value mapping (unknown values throw)."""

    name: str = ""
    is_integer: bool = False
    members: list[CppEnumMember] = field(default_factory=list)
    comment: str | None = None


@dataclass
class CppStruct(CppNode):
    """An aggregate struct (base fields flattened in)."""

    name: str = ""
    fields: list[CppField] = field(default_factory=list)
    comment: str | None = None


@dataclass
class CppVariantAlternative(CppNode):
    concrete_type: str = ""
    discriminator_value: str = ""


@dataclass
class CppVariant(CppNode):
    """A discriminated union: ``using Name = std::variant<...>`` dispatched on a key."""

    name: str = ""
    discriminator_key: str = "type"
    alternatives: list[CppVariantAlternative] = field(default_factory=list)
    comment: str | None = None


@dataclass
class CppFile(CppNode):
    """A complete C++ header."""

    generation_comment: str = ""
    includes: list[str] = field(default_factory=list)  # rendered: "<vector>" or "\"foo.hpp\""
    namespace: str = ""
    enums: list[CppEnum] = field(default_factory=list)
    # Structs and variants, already in dependency order (a type precedes its by-value users).
    types: list[CppStruct | CppVariant] = field(default_factory=list)
