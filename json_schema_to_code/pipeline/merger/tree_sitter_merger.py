"""
Shared scaffolding for the tree-sitter based mergers (C#, Swift).

Each language merger names its grammar; this class owns the parser and its error
reporting, the node lookups, the ``// CUSTOM CODE START/END`` raw sections and the
position at which custom imports are re-inserted after the generated ones.
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any

from .base import AstMerger, CodeMergeError


@lru_cache(maxsize=8)
def _char_offsets(code: str) -> tuple[int, ...]:
    """Character index of every UTF-8 byte offset of ``code`` (and of its end)."""
    offsets: list[int] = []
    for index, char in enumerate(code):
        offsets.extend([index] * len(char.encode("utf8")))
    offsets.append(len(code))
    return tuple(offsets)


class TreeSitterMerger(AstMerger):
    """A merger over a tree-sitter grammar."""

    # Human name for messages, and the grammar's import / pip names.
    LANGUAGE_NAME = ""
    GRAMMAR_MODULE = ""
    GRAMMAR_PACKAGE = ""

    CUSTOM_CODE_START = "// CUSTOM CODE START"
    CUSTOM_CODE_END = "// CUSTOM CODE END"

    def __init__(self):
        try:
            from tree_sitter import Language, Parser

            grammar = importlib.import_module(self.GRAMMAR_MODULE).language()
        except ImportError as e:
            raise CodeMergeError(f"tree-sitter and {self.GRAMMAR_PACKAGE} are required for {self.LANGUAGE_NAME} merging. Install with: pip install tree-sitter {self.GRAMMAR_PACKAGE}") from e
        self._parser = Parser(Language(grammar))

    def parse(self, code: str) -> Any:
        """Parse into a tree-sitter tree, raising CodeMergeError at the first syntax error."""
        tree = self._parser.parse(bytes(code, "utf8"))
        if tree.root_node.has_error:
            errors = self._find_nodes(tree.root_node, "ERROR")
            if errors:
                first = errors[0]
                raise CodeMergeError(f"Failed to parse {self.LANGUAGE_NAME} code at line {first.start_point[0] + 1}: syntax error near {self._text(first, code)[:50]!r}")
        return tree

    # -------------------------------------------------------------- tree helpers

    def _find_nodes(self, node: Any, node_type: str) -> list[Any]:
        """Every node of ``node_type`` under ``node``, in source order."""
        out = []
        if node.type == node_type:
            out.append(node)
        for child in node.children:
            out.extend(self._find_nodes(child, node_type))
        return out

    def _top_level(self, root: Any, node_type: str) -> list[Any]:
        return [c for c in root.children if c.type == node_type]

    # Tree-sitter positions are UTF-8 byte offsets; ``code`` is a str indexed by
    # character. Index ``code`` only through these, never with ``start_byte`` /
    # ``end_byte`` directly: they drift past every multi-byte character.

    def _start(self, node: Any, code: str) -> int:
        return _char_offsets(code)[node.start_byte]

    def _end(self, node: Any, code: str) -> int:
        return _char_offsets(code)[node.end_byte]

    def _text(self, node: Any, code: str) -> str:
        return code[self._start(node, code) : self._end(node, code)]

    def _last_line(self, root: Any, node_type: str) -> int | None:
        """Zero-based line on which the last ``node_type`` node ends, or None if there is none.

        Used to place custom imports right after the generated ones: the node knows
        where an import ends whatever attributes or import kind precede its name.
        """
        nodes = self._find_nodes(root, node_type)
        return nodes[-1].end_point[0] if nodes else None

    # ------------------------------------------------------------ raw sections

    def _extract_marked_sections(self, code: str) -> list[str]:
        """The bodies of ``// CUSTOM CODE START`` ... ``// CUSTOM CODE END`` blocks."""
        sections: list[str] = []
        current: list[str] = []
        in_section = False
        for line in code.split("\n"):
            stripped = line.strip()
            if stripped == self.CUSTOM_CODE_START:
                in_section = True
                current = []
            elif stripped == self.CUSTOM_CODE_END:
                if in_section and current:
                    sections.append("\n".join(current))
                in_section = False
                current = []
            elif in_section:
                current.append(line)
        return sections
