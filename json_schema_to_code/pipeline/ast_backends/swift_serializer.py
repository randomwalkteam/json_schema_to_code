"""
Swift AST serializer.

Converts Swift AST nodes to formatted Swift source code:
- 4-space indentation
- ``import Foundation``
- structs conforming to ``Codable`` with a nested ``CodingKeys`` mapping to JSON keys
- a custom ``init(from:)`` (in an extension) when any property has a default, so the
  default is applied for a missing key (Swift's synthesized Codable ignores defaults)
- string/int raw-value enums
- discriminated polymorphic ``enum``s with ``init(from:)`` / ``encode(to:)``
- primitive unions (``IntOrString``) decoded by trying each member in turn
- with policy decoding, every ``init(from:)`` reads through the consuming module's
  ``policyContainer(keyedBy:)``, which decides whether a schema mismatch throws
"""

from __future__ import annotations

from .swift_ast_nodes import (
    SwiftEnum,
    SwiftFile,
    SwiftPolyEnum,
    SwiftProperty,
    SwiftStruct,
    SwiftUnionEnum,
)


class SwiftSerializer:
    """Serializes Swift AST nodes to source code."""

    INDENT = "    "

    def serialize(self, file: SwiftFile) -> str:
        lines: list[str] = []

        if file.generation_comment:
            lines.append(file.generation_comment)
            lines.append("")

        for imp in file.imports:
            lines.append(f"import {imp}")
        if file.imports:
            lines.append("")

        blocks: list[list[str]] = []
        blocks.extend([self._serialize_enum(e)] for e in file.enums)
        blocks.extend([self._serialize_union_enum(u)] for u in file.union_enums)
        blocks.extend([self._serialize_poly_enum(pe)] for pe in file.poly_enums)
        blocks.extend([self._serialize_struct(s)] for s in file.structs)

        # Flatten with a blank line between top-level declarations.
        flat: list[str] = []
        for i, block in enumerate(blocks):
            if i > 0:
                flat.append("")
            flat.extend(block[0])

        lines.extend(flat)
        return "\n".join(lines).rstrip() + "\n"

    # ------------------------------------------------------------------- enums

    def _serialize_enum(self, enum: SwiftEnum) -> list[str]:
        lines: list[str] = []
        if enum.comment:
            lines.append(f"// {enum.comment}")
        prefix = "nonisolated " if enum.nonisolated else ""
        conformances = ", ".join(enum.conformances)
        lines.append(f"{prefix}enum {enum.name}: {enum.raw_type}, {conformances} {{")
        for case in enum.cases:
            raw = f'"{case.raw_value}"' if enum.raw_type == "String" else case.raw_value
            lines.append(f"{self.INDENT}case {case.name} = {raw}")
        lines.append("}")
        return lines

    def _serialize_union_enum(self, union: SwiftUnionEnum) -> list[str]:
        i1, i2, i3 = self.INDENT, self.INDENT * 2, self.INDENT * 3
        prefix = "nonisolated " if union.nonisolated else ""
        lines = [f"{prefix}enum {union.name}: {', '.join(union.conformances)} {{"]
        lines.extend(f"{i1}case {case.name}({case.type_name})" for case in union.cases)
        if self._decodes(union.conformances):
            lines.append("")
            lines.append(f"{i1}init(from decoder: Decoder) throws {{")
            lines.append(f"{i2}let container = try decoder.singleValueContainer()")
            for index, case in enumerate(union.cases):
                keyword = "if" if index == 0 else "} else if"
                lines.append(f"{i2}{keyword} let value = try? container.decode({case.type_name}.self) {{")
                lines.append(f"{i3}self = .{case.name}(value)")
            members = ", ".join(case.type_name for case in union.cases)
            lines.append(f"{i2}}} else {{")
            lines.append(f'{i3}throw DecodingError.typeMismatch({union.name}.self, DecodingError.Context(codingPath: decoder.codingPath, debugDescription: "Expected one of {members}"))')
            lines.append(f"{i2}}}")
            lines.append(f"{i1}}}")
        if self._encodes(union.conformances):
            lines.append("")
            lines.append(f"{i1}func encode(to encoder: Encoder) throws {{")
            lines.append(f"{i2}var container = encoder.singleValueContainer()")
            lines.append(f"{i2}switch self {{")
            lines.extend(f"{i2}case .{case.name}(let value): try container.encode(value)" for case in union.cases)
            lines.append(f"{i2}}}")
            lines.append(f"{i1}}}")
        lines.append("}")
        return lines

    @staticmethod
    def _decodes(conformances: list[str]) -> bool:
        return any(c in ("Codable", "Decodable") for c in conformances)

    @staticmethod
    def _encodes(conformances: list[str]) -> bool:
        return any(c in ("Codable", "Encodable") for c in conformances)

    def _serialize_poly_enum(self, poly: SwiftPolyEnum) -> list[str]:
        lines: list[str] = []
        if poly.comment:
            lines.append(f"// {poly.comment}")
        prefix = "nonisolated " if poly.nonisolated else ""
        conformances = ", ".join(poly.conformances)
        lines.append(f"{prefix}enum {poly.name}: {conformances} {{")
        for case in poly.cases:
            lines.append(f"{self.INDENT}case {case.name}({case.concrete_type})")
        if poly.unknown_payload_type:
            lines.append(f"{self.INDENT}/// A discriminator this client does not know, with the whole object kept.")
            lines.append(f"{self.INDENT}case unknown(type: String, payload: {poly.unknown_payload_type})")
        lines.append("")

        # Discriminator coding key
        lines.append(f"{self.INDENT}private enum DiscriminatorKeys: String, CodingKey {{")
        lines.append(f'{self.INDENT}{self.INDENT}case discriminator = "{poly.discriminator_key}"')
        lines.append(f"{self.INDENT}}}")
        lines.append("")

        # init(from:)
        lines.append(f"{self.INDENT}init(from decoder: Decoder) throws {{")
        lines.append(f"{self.INDENT}{self.INDENT}let container = try decoder.container(keyedBy: DiscriminatorKeys.self)")
        lines.append(f"{self.INDENT}{self.INDENT}let discriminator = try container.decode(String.self, forKey: .discriminator)")
        lines.append(f"{self.INDENT}{self.INDENT}switch discriminator {{")
        for case in poly.cases:
            lines.append(f'{self.INDENT}{self.INDENT}case "{case.discriminator_value}": self = .{case.name}(try {case.concrete_type}(from: decoder))')
        lines.append(f"{self.INDENT}{self.INDENT}default:")
        if poly.unknown_payload_type:
            lines.append(f"{self.INDENT}{self.INDENT}{self.INDENT}self = .unknown(type: discriminator, payload: try {poly.unknown_payload_type}(from: decoder))")
        else:
            lines.append(
                f"{self.INDENT}{self.INDENT}{self.INDENT}throw DecodingError.dataCorruptedError("
                "forKey: .discriminator, in: container, "
                f'debugDescription: "Unknown discriminator \\(discriminator) for {poly.name}")'
            )
        lines.append(f"{self.INDENT}{self.INDENT}}}")
        lines.append(f"{self.INDENT}}}")
        lines.append("")

        # encode(to:) — only when the type actually encodes
        if not any(c in ("Codable", "Encodable") for c in poly.conformances):
            lines.append("}")
            return lines
        lines.append(f"{self.INDENT}func encode(to encoder: Encoder) throws {{")
        lines.append(f"{self.INDENT}{self.INDENT}switch self {{")
        for case in poly.cases:
            lines.append(f"{self.INDENT}{self.INDENT}case .{case.name}(let value): try value.encode(to: encoder)")
        if poly.unknown_payload_type:
            lines.append(f"{self.INDENT}{self.INDENT}case .unknown(_, let payload): try payload.encode(to: encoder)")
        lines.append(f"{self.INDENT}{self.INDENT}}}")
        lines.append(f"{self.INDENT}}}")
        lines.append("}")
        return lines

    # ----------------------------------------------------------------- structs

    def _serialize_struct(self, struct: SwiftStruct) -> list[str]:
        lines: list[str] = []
        if struct.comment:
            lines.append(f"// {struct.comment}")
        prefix = "nonisolated " if struct.nonisolated else ""
        conformances = ", ".join(struct.conformances)
        lines.append(f"{prefix}struct {struct.name}: {conformances} {{")

        for prop in struct.properties:
            keyword = self._keyword(prop, struct.policy_decoding)
            decl = f"{self.INDENT}{keyword} {prop.name}: {prop.type_name}"
            if prop.default_value is not None:
                decl += f" = {prop.default_value}"
            if prop.comment:
                decl += prop.comment
            lines.append(decl)

        # CodingKeys (always emitted: JSON keys are snake_case, properties lowerCamelCase)
        if struct.properties:
            lines.append("")
            lines.append(f"{self.INDENT}enum CodingKeys: String, CodingKey {{")
            for prop in struct.properties:
                lines.append(f'{self.INDENT}{self.INDENT}case {prop.name} = "{prop.json_key}"')
            lines.append(f"{self.INDENT}}}")

        lines.append("}")

        # Custom decoder in an extension (keeps the synthesized memberwise init available).
        if struct.needs_custom_decoder:
            lines.append("")
            # nonisolated must be restated: an extension is its own isolation scope,
            # so under default-MainActor projects the decoder witness would otherwise
            # be @MainActor while the conformance is nonisolated.
            lines.append(f"{prefix}extension {struct.name} {{")
            lines.append(f"{self.INDENT}init(from decoder: Decoder) throws {{")
            if struct.policy_decoding:
                lines.append(f"{self.INDENT}{self.INDENT}let container = try decoder.policyContainer(keyedBy: CodingKeys.self)")
                lines.extend(self._policy_decode_statement(p) for p in struct.properties if not p.is_const)
            else:
                lines.append(f"{self.INDENT}{self.INDENT}let container = try decoder.container(keyedBy: CodingKeys.self)")
                for prop in struct.properties:
                    lines.append(self._decode_statement(prop))
            lines.append(f"{self.INDENT}}}")
            if struct.needs_custom_encoder:
                lines.append("")
                lines.extend(self._encoder(struct))
            lines.append("}")

        return lines

    @staticmethod
    def _keyword(prop: SwiftProperty, policy_decoding: bool) -> str:
        """``var`` for a property with a default; with policy decoding also for an
        optional one (its memberwise argument then defaults to nil), ``let`` for a
        discriminator constant and for the rest."""
        if policy_decoding and prop.is_const:
            return "let"
        if prop.default_value is not None:
            return "var"
        if policy_decoding and prop.is_optional and not prop.is_required:
            return "var"
        return "let"

    def _policy_decode_statement(self, prop: SwiftProperty) -> str:
        body = f"{self.INDENT}{self.INDENT}self.{prop.name} = try container."
        key = f"forKey: .{prop.name}"
        base_type = prop.type_name[:-1] if prop.type_name.endswith("?") else prop.type_name
        element = prop.element_type
        if prop.is_required:
            if prop.is_optional:
                return body + f"decodeRequired({prop.type_name}.self, {key}, fallback: nil)"
            if element is not None:
                fallback = "" if prop.fallback in (None, "[]") else f", fallback: {prop.fallback}"
                return body + f"decodeRequiredArray({element}.self, {key}{fallback})"
            fallback = f", fallback: {prop.fallback}" if prop.fallback is not None else ""
            return body + f"decodeRequired({base_type}.self, {key}{fallback})"
        default = f", default: {prop.default_value}" if prop.default_value is not None else ""
        if element is not None:
            return body + f"decodeOptionalArray({element}.self, {key}{default})"
        return body + f"decodeOptional({base_type}.self, {key}{default})"

    def _encoder(self, struct: SwiftStruct) -> list[str]:
        """``encode(to:)`` that writes every required key, ``null`` included."""
        i1, i2 = self.INDENT, self.INDENT * 2
        lines = [f"{i1}func encode(to encoder: Encoder) throws {{", f"{i2}var container = encoder.container(keyedBy: CodingKeys.self)"]
        for prop in struct.properties:
            method = "encode" if prop.is_required or not prop.is_optional else "encodeIfPresent"
            lines.append(f"{i2}try container.{method}({prop.name}, forKey: .{prop.name})")
        lines.append(f"{i1}}}")
        return lines

    def _decode_statement(self, prop) -> str:
        body = f"{self.INDENT}{self.INDENT}self.{prop.name} = "
        # Strip a trailing optional marker for the decoded type argument.
        base_type = prop.type_name[:-1] if prop.type_name.endswith("?") else prop.type_name
        if prop.default_value is not None:
            return body + f"try container.decodeIfPresent({base_type}.self, forKey: .{prop.name}) ?? {prop.default_value}"
        if prop.is_optional:
            return body + f"try container.decodeIfPresent({base_type}.self, forKey: .{prop.name})"
        return body + f"try container.decode({base_type}.self, forKey: .{prop.name})"
