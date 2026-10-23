"""
AST-based code generation backends.

These backends generate code using language-native AST representations
instead of template-based string generation.
"""

from __future__ import annotations

from .base import AstBackend
from .cpp_ast_backend import CppAstBackend
from .csharp_ast_backend import CSharpAstBackend
from .python_ast_backend import PythonAstBackend
from .swift_ast_backend import SwiftAstBackend

__all__ = [
    "AstBackend",
    "PythonAstBackend",
    "CSharpAstBackend",
    "SwiftAstBackend",
    "CppAstBackend",
]
