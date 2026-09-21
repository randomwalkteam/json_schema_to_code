"""
C# AST merger implementation.

Uses tree-sitter and tree-sitter-c-sharp to parse, extract custom code,
and merge generated code with existing files.
"""

from __future__ import annotations

from typing import Any

from ..config import MergeStrategy
from .base import CodeMergeError, CustomCode
from .tree_sitter_merger import TreeSitterMerger


class CSharpAstMerger(TreeSitterMerger):
    """Merger for C# source files using tree-sitter."""

    LANGUAGE_NAME = "C#"
    GRAMMAR_MODULE = "tree_sitter_c_sharp"
    GRAMMAR_PACKAGE = "tree-sitter-c-sharp"

    # Standard using statements that are always generated
    STANDARD_USINGS = {
        "System",
        "System.Collections.Generic",
        "Newtonsoft.Json",
        "JsonSubTypes",
    }

    NO_MERGE_MARKER = "// jstc-no-merge"

    def merge_files(
        self,
        generated_code: str,
        existing_code: str,
        merge_strategy: MergeStrategy = MergeStrategy.ERROR,
    ) -> str:
        custom_code, no_merge_overrides = self._extract_all(existing_code, generated_code, merge_strategy)
        if custom_code.is_empty() and not no_merge_overrides:
            return generated_code
        merged = self.merge(generated_code, custom_code) if not custom_code.is_empty() else generated_code
        if no_merge_overrides:
            merged = self._apply_no_merge_overrides(merged, no_merge_overrides)
        if custom_code.member_leading_comments:
            merged = self._inject_member_comments(merged, custom_code.member_leading_comments)
        self.validate(merged)
        return merged

    def extract_custom_code(self, existing_code: str, generated_code: str) -> CustomCode:
        custom, _ = self._extract_all(existing_code, generated_code, MergeStrategy.ERROR)
        return custom

    def _extract_all(
        self,
        existing_code: str,
        generated_code: str,
        merge_strategy: MergeStrategy,
    ) -> tuple[CustomCode, dict[str, list[tuple[str, str, str]]]]:
        """Single-pass extraction of custom code and no-merge overrides.

        Returns:
            Tuple of (custom_code, no_merge_overrides).
            no_merge_overrides maps class_name -> [(member_type, member_name, full_source)].
        """
        existing_tree = self.parse(existing_code)
        generated_tree = self.parse(generated_code)

        custom = CustomCode()
        overrides: dict[str, list[tuple[str, str, str]]] = {}

        generated_usings = self._extract_usings(generated_tree.root_node, generated_code)
        generated_types = self._extract_type_names(generated_tree.root_node, generated_code)
        generated_members = self._extract_class_members(generated_tree.root_node, generated_code)
        generated_value_members = self._extract_class_value_members(generated_tree.root_node, generated_code)
        gen_ctor_counts = self._get_all_constructor_param_counts(generated_tree.root_node, generated_code)

        root = existing_tree.root_node
        file_namespace = self._extract_file_namespace(generated_tree.root_node, generated_code)

        # Custom using statements
        for using in self._find_nodes(root, "using_directive"):
            using_text = self._text(using, existing_code)
            namespace = self._extract_namespace_from_using(using_text)
            if namespace and namespace not in generated_usings:
                if file_namespace and namespace == file_namespace:
                    continue
                custom.custom_imports.append(using_text)

        # Class members
        for class_node in self._find_nodes(root, "class_declaration"):
            class_name = self._get_class_name(class_node, existing_code)
            if not class_name or class_name not in generated_types:
                continue

            gen_members = generated_members.get(class_name, set())

            body = None
            for child in class_node.children:
                if child.type == "declaration_list":
                    body = child
                    break
            if not body:
                continue

            # Validate removed value members
            existing_value_members = self._extract_value_members_from_class_body(body, existing_code)
            generated_value_names = generated_value_members.get(class_name, set())
            for member_name, member_node in existing_value_members.items():
                if member_name in generated_value_names:
                    continue
                if merge_strategy == MergeStrategy.ERROR:
                    line = member_node.start_point[0] + 1
                    column = member_node.start_point[1] + 1
                    raise CodeMergeError(
                        "Merge aborted: existing value member is not generated anymore. "
                        "Use --merge-strategy merge to keep it, "
                        "or --merge-strategy delete to remove it. "
                        f"Location: class '{class_name}', member '{member_name}' "
                        f"at line {line}, column {column}."
                    )

            class_ctor_counts = gen_ctor_counts.get(class_name, set())
            prev_attr_nodes: list = []
            prev_comment_nodes: list = []

            for member in body.children:
                if member.type == "comment":
                    prev_comment_nodes.append(member)
                    continue
                if member.type == "attribute_list":
                    prev_attr_nodes.append(member)
                    continue

                has_marker = self._has_no_merge_marker(member, existing_code)
                is_custom = False

                if member.type == "method_declaration":
                    method_name = self._get_method_name(member, existing_code)
                    if method_name and method_name not in gen_members:
                        is_custom = True
                        custom.class_methods.setdefault(class_name, []).append(self._get_text_with_preceding_comments(prev_comment_nodes, member, existing_code))

                elif member.type == "property_declaration":
                    prop_name = self._get_property_name(member, existing_code)
                    if prop_name and has_marker and prop_name in gen_members:
                        full_text = self._get_text_with_preceding_attributes(prev_attr_nodes, member, existing_code)
                        overrides.setdefault(class_name, []).append(("property", prop_name, full_text))
                    elif prop_name and prop_name not in gen_members and not has_marker:
                        if merge_strategy == MergeStrategy.DELETE:
                            prev_attr_nodes = []
                            prev_comment_nodes = []
                            continue
                        full_text = self._get_text_with_preceding_attributes(prev_attr_nodes, member, existing_code)
                        is_custom = True
                        custom.class_attributes.setdefault(class_name, []).append(full_text)

                elif member.type == "constructor_declaration":
                    if has_marker:
                        full_text = self._get_text_with_preceding_attributes(prev_attr_nodes, member, existing_code)
                        overrides.setdefault(class_name, []).append(("constructor", class_name, full_text))
                    elif self._count_constructor_params(member) not in class_ctor_counts:
                        is_custom = True
                        custom.class_methods.setdefault(class_name, []).append(self._get_text_with_preceding_comments(prev_comment_nodes, member, existing_code))

                if not is_custom and prev_comment_nodes:
                    key = self._member_key(member, existing_code, class_name)
                    if key:
                        comments = [self._text(c, existing_code) for c in prev_comment_nodes]
                        custom.member_leading_comments.setdefault(class_name, {})[key] = comments

                prev_attr_nodes = []
                prev_comment_nodes = []

        custom.raw_sections = self._extract_marked_sections(existing_code)
        return custom, overrides

    def merge(self, generated_code: str, custom_code: CustomCode) -> str:
        """Merge custom code into generated C# code."""
        lines = generated_code.split("\n")
        result_lines = []

        current_class = None
        class_end_indices: dict[str, int] = {}

        tree = self.parse(generated_code)
        # Custom usings go right after the last generated using directive.
        last_using_line = self._last_line(tree.root_node, "using_directive")
        generated_members = self._extract_class_members(tree.root_node, generated_code)
        for class_node in self._find_nodes(tree.root_node, "class_declaration"):
            class_name = self._get_class_name(class_node, generated_code)
            if class_name:
                class_end_indices[class_name] = class_node.end_point[0]

        for i, line in enumerate(lines):
            stripped = line.strip()

            if "class " in stripped and stripped.endswith("{") or ("class " in stripped and i + 1 < len(lines) and lines[i + 1].strip() == "{"):
                for class_name in class_end_indices:
                    if f"class {class_name}" in stripped:
                        current_class = class_name
                        break

            if current_class and current_class in class_end_indices:
                if i == class_end_indices[current_class]:
                    indent = "    "
                    gen_members = generated_members.get(current_class, set())

                    if current_class in custom_code.class_attributes:
                        for attr in custom_code.class_attributes[current_class]:
                            attr_prop_name = self._get_property_name_from_source(attr)
                            if attr_prop_name and attr_prop_name in gen_members:
                                continue
                            result_lines.append("")
                            for attr_line in attr.split("\n"):
                                result_lines.append(indent + attr_line)

                    if current_class in custom_code.class_methods:
                        for method in custom_code.class_methods[current_class]:
                            result_lines.append("")
                            for method_line in method.split("\n"):
                                result_lines.append(indent + method_line)

                    current_class = None

            result_lines.append(line)
            if i == last_using_line:
                result_lines.extend(custom_code.custom_imports)

        if custom_code.raw_sections:
            for i in range(len(result_lines) - 1, -1, -1):
                if result_lines[i].strip() == "}":
                    for section in custom_code.raw_sections:
                        result_lines.insert(i, "")
                        for section_line in section.split("\n"):
                            result_lines.insert(i, "    " + section_line)
                    break

        return "\n".join(result_lines)

    def validate(self, code: str) -> None:
        """Validate that merged C# code is syntactically correct."""
        self.parse(code)

        if "class " not in code and "enum " not in code:
            raise CodeMergeError("Merged C# code has no type definitions")

    # -- Extraction helpers --

    def _extract_usings(self, root: Any, code: str) -> set[str]:
        usings = set()
        for using in self._find_nodes(root, "using_directive"):
            text = self._text(using, code)
            namespace = self._extract_namespace_from_using(text)
            if namespace:
                usings.add(namespace)
        return usings

    def _extract_file_namespace(self, root: Any, code: str) -> str | None:
        for ns_node in self._find_nodes(root, "namespace_declaration"):
            for child in ns_node.children:
                if child.type in ("identifier", "qualified_name"):
                    return self._text(child, code)
        for ns_node in self._find_nodes(root, "file_scoped_namespace_declaration"):
            for child in ns_node.children:
                if child.type in ("identifier", "qualified_name"):
                    return self._text(child, code)
        return None

    def _extract_namespace_from_using(self, using_text: str) -> str | None:
        text = using_text.strip()
        if text.startswith("using ") and text.endswith(";"):
            return text[6:-1].strip()
        return None

    def _extract_type_names(self, root: Any, code: str) -> set[str]:
        names = set()
        for node in self._find_nodes(root, "class_declaration"):
            name = self._get_class_name(node, code)
            if name:
                names.add(name)
        for node in self._find_nodes(root, "enum_declaration"):
            name = self._get_enum_name(node, code)
            if name:
                names.add(name)
        return names

    def _extract_class_members(self, root: Any, code: str) -> dict[str, set[str]]:
        members = {}
        for class_node in self._find_nodes(root, "class_declaration"):
            class_name = self._get_class_name(class_node, code)
            if not class_name:
                continue

            class_members = set()

            for child in class_node.children:
                if child.type == "declaration_list":
                    for member in child.children:
                        if member.type == "property_declaration":
                            prop_name = self._get_property_name(member, code)
                            if prop_name:
                                class_members.add(prop_name)
                        elif member.type == "method_declaration":
                            method_name = self._get_method_name(member, code)
                            if method_name:
                                class_members.add(method_name)
                        elif member.type == "constructor_declaration":
                            class_members.add(class_name)

            members[class_name] = class_members

        return members

    def _extract_class_value_members(self, root: Any, code: str) -> dict[str, set[str]]:
        members: dict[str, set[str]] = {}
        for class_node in self._find_nodes(root, "class_declaration"):
            class_name = self._get_class_name(class_node, code)
            if not class_name:
                continue

            body = None
            for child in class_node.children:
                if child.type == "declaration_list":
                    body = child
                    break
            if body is None:
                members[class_name] = set()
                continue

            value_members = self._extract_value_members_from_class_body(body, code)
            members[class_name] = set(value_members.keys())
        return members

    def _extract_value_members_from_class_body(self, body: Any, code: str) -> dict[str, Any]:
        members: dict[str, Any] = {}
        for member in body.children:
            if member.type == "property_declaration":
                prop_name = self._get_property_name(member, code)
                if prop_name:
                    members[prop_name] = member
                continue
            if member.type != "field_declaration":
                continue
            for variable in self._find_nodes(member, "variable_declarator"):
                name_node = variable.child_by_field_name("name")
                if not name_node:
                    continue
                field_name = self._text(name_node, code)
                if field_name:
                    members[field_name] = member
        return members

    def _get_all_constructor_param_counts(self, root: Any, code: str) -> dict[str, set[int]]:
        """Get constructor parameter counts for all classes in the tree."""
        result: dict[str, set[int]] = {}
        for class_node in self._find_nodes(root, "class_declaration"):
            name = self._get_class_name(class_node, code)
            if not name:
                continue
            counts: set[int] = set()
            for child in class_node.children:
                if child.type != "declaration_list":
                    continue
                for member in child.children:
                    if member.type == "constructor_declaration":
                        counts.add(self._count_constructor_params(member))
            result[name] = counts
        return result

    # -- Node name helpers --

    def _get_class_name(self, node: Any, code: str) -> str | None:
        for child in node.children:
            if child.type == "identifier":
                return self._text(child, code)
        return None

    def _get_enum_name(self, node: Any, code: str) -> str | None:
        for child in node.children:
            if child.type == "identifier":
                return self._text(child, code)
        return None

    def _get_property_name(self, node: Any, code: str) -> str | None:
        name_node = node.child_by_field_name("name")
        if name_node:
            return self._text(name_node, code)
        return None

    def _get_property_name_from_source(self, attr_source: str) -> str | None:
        """Extract property name from a property declaration source fragment."""
        wrapped = f"namespace __ {{ class __ {{{attr_source}}} }}"
        try:
            tree = self.parse(wrapped)
            for prop in self._find_nodes(tree.root_node, "property_declaration"):
                name = self._get_property_name(prop, wrapped)
                if name:
                    return name
        except CodeMergeError:
            pass
        return None

    def _get_method_name(self, node: Any, code: str) -> str | None:
        for child in node.children:
            if child.type == "identifier":
                return self._text(child, code)
        return None

    # -- Member key and comment helpers --

    def _member_key(self, member_node: Any, code: str, class_name: str) -> str | None:
        """Compute a stable key for a class member node, used to match members between existing and merged code."""
        if member_node.type == "constructor_declaration":
            return f"ctor_{self._count_constructor_params(member_node)}"
        elif member_node.type == "method_declaration":
            name = self._get_method_name(member_node, code)
            return f"method_{name}" if name else None
        elif member_node.type == "property_declaration":
            name = self._get_property_name(member_node, code)
            return f"prop_{name}" if name else None
        elif member_node.type == "field_declaration":
            for var in self._find_nodes(member_node, "variable_declarator"):
                name_node = var.child_by_field_name("name")
                if name_node:
                    return f"field_{self._text(name_node, code)}"
        return None

    def _get_text_with_preceding_comments(self, comment_nodes: list, member_node: Any, code: str) -> str:
        """Get member source text including preceding comment nodes."""
        if not comment_nodes:
            return self._text(member_node, code)
        return code[self._start(comment_nodes[0], code) : self._end(member_node, code)]

    # -- No-merge and attribute helpers --

    def _has_no_merge_marker(self, node: Any, code: str) -> bool:
        """Check if any source line of the node contains the jstc-no-merge marker.

        Tree-sitter nodes don't include trailing comments in their byte range,
        so we check the full source lines.
        """
        source_lines = code.splitlines()
        for line_idx in range(node.start_point[0], node.end_point[0] + 1):
            if line_idx < len(source_lines) and self.NO_MERGE_MARKER in source_lines[line_idx]:
                return True
        return False

    def _get_text_with_preceding_attributes(self, prev_attr_nodes: list, member_node: Any, code: str) -> str:
        """Get member source text including preceding attribute_list nodes and trailing comments."""
        start = self._start(prev_attr_nodes[0] if prev_attr_nodes else member_node, code)
        line_end = code.find("\n", self._end(member_node, code))
        end = line_end if line_end != -1 else len(code)
        return code[start:end]

    def _count_constructor_params(self, ctor_node: Any) -> int:
        for child in ctor_node.children:
            if child.type == "parameter_list":
                return len([c for c in child.children if c.type == "parameter"])
        return 0

    # -- No-merge override application --

    def _apply_no_merge_overrides(self, merged_code: str, overrides: dict[str, list[tuple[str, str, str]]]) -> str:
        """Replace generated members with no-merge override versions."""
        tree = self.parse(merged_code)
        root = tree.root_node
        replacements: list[tuple[int, int, str]] = []

        for class_node in self._find_nodes(root, "class_declaration"):
            class_name = self._get_class_name(class_node, merged_code)
            if not class_name or class_name not in overrides:
                continue

            class_overrides = overrides[class_name]

            body = None
            for child in class_node.children:
                if child.type == "declaration_list":
                    body = child
                    break
            if not body:
                continue

            prop_overrides = {name: text for mtype, name, text in class_overrides if mtype == "property"}
            ctor_overrides = [text for mtype, _, text in class_overrides if mtype == "constructor"]

            prev_attr_nodes: list = []
            for member in body.children:
                if member.type == "attribute_list":
                    prev_attr_nodes.append(member)
                    continue

                if member.type == "property_declaration":
                    name = self._get_property_name(member, merged_code)
                    if name and name in prop_overrides:
                        start = self._start(prev_attr_nodes[0] if prev_attr_nodes else member, merged_code)
                        replacements.append((start, self._end(member, merged_code), prop_overrides[name]))

                elif member.type == "constructor_declaration" and ctor_overrides:
                    if self._count_constructor_params(member) > 0:
                        replacements.append((self._start(member, merged_code), self._end(member, merged_code), ctor_overrides.pop(0)))

                prev_attr_nodes = []

        for start, end, text in sorted(replacements, key=lambda r: r[0], reverse=True):
            merged_code = merged_code[:start] + text + merged_code[end:]

        return merged_code

    def _inject_member_comments(self, merged_code: str, member_comments: dict[str, dict[str, list[str]]]) -> str:
        """Inject preserved leading comments before matching members in the merged code."""
        tree = self.parse(merged_code)
        insertions: list[tuple[int, str]] = []

        for class_node in self._find_nodes(tree.root_node, "class_declaration"):
            class_name = self._get_class_name(class_node, merged_code)
            if not class_name or class_name not in member_comments:
                continue

            class_comments = member_comments[class_name]
            body = None
            for child in class_node.children:
                if child.type == "declaration_list":
                    body = child
                    break
            if not body:
                continue

            prev_attr_nodes: list = []
            for member in body.children:
                if member.type in ("{", "}", "comment"):
                    continue
                if member.type == "attribute_list":
                    prev_attr_nodes.append(member)
                    continue

                key = self._member_key(member, merged_code, class_name)
                if key and key in class_comments:
                    insert_before = prev_attr_nodes[0] if prev_attr_nodes else member
                    insert_at = self._start(insert_before, merged_code)
                    line_start = merged_code.rfind("\n", 0, insert_at)
                    indent = ""
                    if line_start >= 0:
                        raw = merged_code[line_start + 1 : insert_at]
                        if raw.isspace() or raw == "":
                            indent = raw

                    comment_lines = "\n".join(indent + c.strip() for c in class_comments[key]) + "\n"
                    insertions.append((insert_at, comment_lines))

                prev_attr_nodes = []

        for offset, text in sorted(insertions, key=lambda t: t[0], reverse=True):
            merged_code = merged_code[:offset] + text + merged_code[offset:]

        return merged_code
