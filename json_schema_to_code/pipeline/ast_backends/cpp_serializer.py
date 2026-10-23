"""
C++ AST serializer.

Converts C++ AST nodes to a self-contained header:
- ``#pragma once``, standard includes and the nlohmann/json include
- scoped enums with explicit ``to_json`` / ``from_json`` (unknown values throw)
- aggregate structs with default member initializers and a defaulted ``operator==``
- ``std::variant`` aliases for discriminated unions, dispatched on the discriminator key
- ``to_json`` / ``from_json`` free functions, declared before any definition so that
  types can reference each other in any order
"""

from __future__ import annotations

from .cpp_ast_nodes import CppEnum, CppField, CppFieldKind, CppFile, CppStruct, CppVariant

JSON = "nlohmann::json"


class CppSerializer:
    """Serializes C++ AST nodes to source code."""

    INDENT = "    "

    def serialize(self, file: CppFile) -> str:
        lines: list[str] = []
        if file.generation_comment:
            lines += [file.generation_comment, ""]
        lines += ["#pragma once", ""]
        lines += [f"#include {include}" for include in file.includes]
        lines.append("")

        if file.namespace:
            lines += [f"namespace {file.namespace} {{", ""]

        blocks: list[list[str]] = []
        for enum in file.enums:
            blocks.append(self._enum_declaration(enum))
            blocks.append(self._enum_functions(enum))
        structs = [t for t in file.types if isinstance(t, CppStruct)]
        if structs:
            blocks.append([f"struct {s.name};" for s in structs])
        for node in file.types:
            if isinstance(node, CppStruct):
                blocks.append(self._struct_declaration(node))
            else:
                blocks.append(self._variant_declaration(node))
        if file.types:
            declarations: list[str] = []
            for node in file.types:
                declarations += [f"inline {self._to_json_signature(node.name)};", f"inline {self._from_json_signature(node.name)};"]
            blocks.append(declarations)
        for node in file.types:
            if isinstance(node, CppStruct):
                blocks.append(self._struct_functions(node))
            else:
                blocks.append(self._variant_functions(node))

        for i, block in enumerate(blocks):
            if i > 0:
                lines.append("")
            lines += block

        if file.namespace:
            lines += ["", f"}} // namespace {file.namespace}"]
        return "\n".join(lines).rstrip() + "\n"

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _to_json_signature(type_name: str, unused: bool = False) -> str:
        maybe = "[[maybe_unused]] " if unused else ""
        return f"void to_json({maybe}{JSON}& j, {maybe}const {type_name}& v)"

    @staticmethod
    def _from_json_signature(type_name: str, unused: bool = False) -> str:
        maybe = "[[maybe_unused]] " if unused else ""
        return f"void from_json({maybe}const {JSON}& j, {maybe}{type_name}& v)"

    @staticmethod
    def _comment(comment: str | None) -> list[str]:
        return [f"// {line}" for line in comment.splitlines()] if comment else []

    # -------------------------------------------------------------------- enums

    def _enum_declaration(self, enum: CppEnum) -> list[str]:
        lines = self._comment(enum.comment)
        underlying = " : std::int64_t" if enum.is_integer else ""
        lines.append(f"enum class {enum.name}{underlying} {{")
        lines += [f"{self.INDENT}{member.name}," for member in enum.members]
        lines.append("};")
        return lines

    def _enum_functions(self, enum: CppEnum) -> list[str]:
        i1, i2 = self.INDENT, self.INDENT * 2
        lines = [f"inline {self._to_json_signature(enum.name)} {{", f"{i1}switch (v) {{"]
        lines += [f"{i1}case {enum.name}::{m.name}: j = {m.json_value}; return;" for m in enum.members]
        lines += [f"{i1}}}", f'{i1}throw std::invalid_argument("invalid {enum.name} value");', "}", ""]
        lines.append(f"inline {self._from_json_signature(enum.name)} {{")
        for m in enum.members:
            lines += [f"{i1}if (j == {m.json_value}) {{", f"{i2}v = {enum.name}::{m.name};", f"{i2}return;", f"{i1}}}"]
        lines += [f'{i1}throw std::invalid_argument("unknown {enum.name} value: " + j.dump());', "}"]
        return lines

    # ------------------------------------------------------------------ structs

    def _struct_declaration(self, struct: CppStruct) -> list[str]:
        lines = self._comment(struct.comment)
        lines.append(f"struct {struct.name} {{")
        for f in struct.fields:
            lines += [f"{self.INDENT}{line}" for line in self._comment(f.comment)]
            lines.append(f"{self.INDENT}{self._member(f)}")
        if struct.fields:
            lines.append("")
        lines.append(f"{self.INDENT}bool operator==(const {struct.name}&) const = default;")
        lines.append("};")
        return lines

    @staticmethod
    def _member(f: CppField) -> str:
        if f.initializer is None:
            nullable = f.kind in (CppFieldKind.OPTIONAL, CppFieldKind.REQUIRED_NULLABLE)
            return f"{f.type_name} {f.name};" if nullable else f"{f.type_name} {f.name}{{}};"
        if f.initializer == "{}":
            return f"{f.type_name} {f.name}{{}};"
        return f"{f.type_name} {f.name} = {f.initializer};"

    def _struct_functions(self, struct: CppStruct) -> list[str]:
        i1 = self.INDENT
        unused = not struct.fields
        lines = [f"inline {self._to_json_signature(struct.name, unused)} {{", f"{i1}j = {JSON}::object();"]
        lines += [f"{i1}{line}" for f in struct.fields for line in self._write_field(f)]
        lines += ["}", "", f"inline {self._from_json_signature(struct.name, unused)} {{"]
        lines += [f"{i1}{line}" for f in struct.fields for line in self._read_field(f)]
        lines.append("}")
        return lines

    @staticmethod
    def _write_field(f: CppField) -> list[str]:
        key = f'j["{f.json_key}"]'
        if f.kind == CppFieldKind.OPTIONAL:
            return [f"if (v.{f.name}) {key} = *v.{f.name};"]
        if f.kind in (CppFieldKind.REQUIRED_NULLABLE, CppFieldKind.DEFAULTED_NULLABLE):
            return [f"{key} = v.{f.name} ? {JSON}(*v.{f.name}) : {JSON}(nullptr);"]
        return [f"{key} = v.{f.name};"]

    def _read_field(self, f: CppField) -> list[str]:
        i1 = self.INDENT
        key, member, value_type = f'"{f.json_key}"', f"v.{f.name}", f.value_type
        find = f"const auto it = j.find({key}); it != j.end()"
        if f.kind == CppFieldKind.REQUIRED:
            return [f"j.at({key}).get_to({member});"]
        if f.kind == CppFieldKind.DEFAULTED:
            return [f"if ({find}) it->get_to({member});"]
        if f.kind == CppFieldKind.OPTIONAL:
            return [
                f"if ({find} && !it->is_null()) {{",
                f"{i1}{member} = it->get<{value_type}>();",
                "} else {",
                f"{i1}{member}.reset();",
                "}",
            ]
        if f.kind == CppFieldKind.REQUIRED_NULLABLE:
            return [
                f"if (const auto& value = j.at({key}); value.is_null()) {{",
                f"{i1}{member}.reset();",
                "} else {",
                f"{i1}{member} = value.get<{value_type}>();",
                "}",
            ]
        # DEFAULTED_NULLABLE: absent keeps the default, explicit null clears it.
        return [
            f"if ({find}) {{",
            f"{i1}if (it->is_null()) {{",
            f"{i1}{i1}{member}.reset();",
            f"{i1}}} else {{",
            f"{i1}{i1}{member} = it->get<{value_type}>();",
            f"{i1}}}",
            "}",
        ]

    # ----------------------------------------------------------------- variants

    def _variant_declaration(self, variant: CppVariant) -> list[str]:
        lines = self._comment(variant.comment)
        alternatives = ", ".join(a.concrete_type for a in variant.alternatives)
        lines.append(f"using {variant.name} = std::variant<{alternatives}>;")
        return lines

    def _variant_functions(self, variant: CppVariant) -> list[str]:
        i1, i2 = self.INDENT, self.INDENT * 2
        lines = [
            f"inline {self._to_json_signature(variant.name)} {{",
            f"{i1}std::visit([&j](const auto& alternative) {{ j = alternative; }}, v);",
            "}",
            "",
            f"inline {self._from_json_signature(variant.name)} {{",
            f'{i1}const auto& discriminator = j.at("{variant.discriminator_key}");',
        ]
        for a in variant.alternatives:
            lines += [
                f'{i1}if (discriminator == "{a.discriminator_value}") {{',
                f"{i2}v = j.get<{a.concrete_type}>();",
                f"{i2}return;",
                f"{i1}}}",
            ]
        lines += [f'{i1}throw std::invalid_argument("unknown discriminator " + discriminator.dump() + " for {variant.name}");', "}"]
        return lines
