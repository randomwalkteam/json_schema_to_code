"""
Swift AST node definitions.

These nodes represent the structure of a Swift source file for code generation.
They are deliberately "dumb" data holders; all generation logic lives in
``swift_ast_backend.py`` and the rendering lives in ``swift_serializer.py``.

The Swift type model (see plan): JSON Schema objects become ``struct``s conforming
to ``Codable``; ``allOf`` inheritance is flattened into each struct; discriminated
polymorphic bases become a generated ``enum`` (``SwiftPolyEnum``) that switches on
the discriminator key in ``init(from:)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SwiftNode:
    """Base class for all Swift AST nodes."""

    pass


@dataclass
class SwiftProperty(SwiftNode):
    """A stored property of a struct.

    ``name`` is the Swift identifier (lowerCamelCase, already backtick-escaped if it
    is a reserved word). ``json_key`` is the original JSON property name, used for the
    ``CodingKeys`` mapping. ``default_value`` is a rendered Swift literal or ``None``.
    """

    name: str = ""
    type_name: str = ""
    json_key: str = ""
    is_optional: bool = False
    default_value: str | None = None
    comment: str | None = None
    # Policy decoding (``swift_policy_decoding``) only:
    is_required: bool = False  # the schema lists the key in ``required``
    is_const: bool = False  # a discriminator constant: a ``let`` with its value, never decoded
    element_type: str | None = None  # element type when the property is an array
    fallback: str | None = None  # literal a lenient decoder uses for a bad required value


@dataclass
class SwiftEnumCase(SwiftNode):
    """A case of a string-raw-value Codable enum."""

    name: str = ""  # Swift case identifier (lowerCamelCase, backtick-escaped if needed)
    raw_value: str = ""  # underlying JSON string value


@dataclass
class SwiftEnum(SwiftNode):
    """A string-raw-value ``Codable`` enum (Swift maps raw value <-> JSON automatically)."""

    name: str = ""
    raw_type: str = "String"
    cases: list[SwiftEnumCase] = field(default_factory=list)
    comment: str | None = None
    conformances: list[str] = field(default_factory=lambda: ["Codable"])
    nonisolated: bool = False


@dataclass
class SwiftStruct(SwiftNode):
    """A ``struct`` conforming to ``Codable`` (base fields flattened in)."""

    name: str = ""
    conformances: list[str] = field(default_factory=lambda: ["Codable"])
    nonisolated: bool = False
    properties: list[SwiftProperty] = field(default_factory=list)
    # Emit a custom init(from:) (in an extension) so property defaults are applied
    # for missing keys -- Swift's synthesized Codable ignores property defaults.
    needs_custom_decoder: bool = False
    # Decode through the consuming module's ``policyContainer`` (``swift_policy_decoding``).
    policy_decoding: bool = False
    # Emit ``encode(to:)``: a required nullable property must encode ``null``, which the
    # synthesized encoder (``encodeIfPresent``) leaves out.
    needs_custom_encoder: bool = False
    comment: str | None = None


@dataclass
class SwiftPolyEnumCase(SwiftNode):
    """One subtype of a discriminated polymorphic enum."""

    name: str = ""  # case identifier (lowerCamelCase of the concrete type)
    concrete_type: str = ""  # the struct type decoded for this case
    discriminator_value: str = ""  # JSON discriminator value selecting this case


@dataclass
class SwiftPolyEnum(SwiftNode):
    """A discriminated union: an enum with associated values, dispatched on a key.

    Replaces C#'s base-class + JsonSubtypes model. ``init(from:)`` reads
    ``discriminator_key`` and decodes the matching concrete struct; ``encode(to:)``
    re-encodes the associated value.
    """

    name: str = ""
    discriminator_key: str = "type"
    cases: list[SwiftPolyEnumCase] = field(default_factory=list)
    comment: str | None = None
    conformances: list[str] = field(default_factory=lambda: ["Codable"])
    nonisolated: bool = False
    # When set, an unknown discriminator decodes to ``unknown(type:payload:)`` with the
    # whole object kept as this JSON value type, instead of throwing.
    unknown_payload_type: str | None = None


@dataclass
class SwiftUnionEnumCase(SwiftNode):
    """One member of a union of primitive types."""

    name: str = ""  # case identifier (lowerCamelCase of the Swift type, e.g. ``int``)
    type_name: str = ""  # Swift primitive type (``Int``, ``String`` ...)


@dataclass
class SwiftUnionEnum(SwiftNode):
    """A union of primitive JSON types (e.g. ``string | integer``) as an enum with
    associated values, decoded by trying each member in turn (``IntOrString``)."""

    name: str = ""
    cases: list[SwiftUnionEnumCase] = field(default_factory=list)
    conformances: list[str] = field(default_factory=lambda: ["Codable"])
    nonisolated: bool = False


@dataclass
class SwiftFile(SwiftNode):
    """A complete Swift source file."""

    generation_comment: str = ""
    imports: list[str] = field(default_factory=lambda: ["Foundation"])
    enums: list[SwiftEnum] = field(default_factory=list)
    union_enums: list[SwiftUnionEnum] = field(default_factory=list)
    poly_enums: list[SwiftPolyEnum] = field(default_factory=list)
    structs: list[SwiftStruct] = field(default_factory=list)
