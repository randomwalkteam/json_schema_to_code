# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.6] - 2026-09-21

### Fixed

- **Swift and C# merges survive non-ASCII text.** Tree-sitter reports UTF-8 byte offsets, but the mergers sliced and spliced the Python `str` with them, so every position after a multi-byte character (`—`, `×`, `é`, …, in a comment or a string) landed that many characters too late. A Swift file with such a comment above a hand-written declaration failed to merge (`nonisolated struct` came out as `ated struct` — "syntax error near 'ated'"); a C# custom member after one was silently dropped. All offsets now go through `TreeSitterMerger._start` / `_end`, which map bytes to characters.

## [1.1.5] - 2026-09-09

### Fixed

- **`__version__` no longer drifts from the packaged version.** It was a hand-maintained string that had fallen three releases behind (`1.1.0` while `pyproject.toml` said `1.1.4`), so every file the generator wrote stamped the wrong version into its header — the one place that provenance is recorded. It now comes from the installed distribution's metadata (`importlib.metadata`), leaving `pyproject.toml` as the single source. Note this reports the version actually *installed*: after bumping, refresh an editable install (`pip install -e . --no-deps`) or it will keep reporting the previous one.

## [1.1.4] - 2026-09-09

### Fixed

- **`x-python-imports` stands on its own.** A field whose `x-python-type` names a class living elsewhere needs that import even when it declares no constructor default; the imports were previously registered only alongside `x-python-default` / `x-python-default-code`, so a plain type override emitted a module that raised `NameError` at import time. Imports are now registered whenever present (the sets dedupe with the default path), and the README documents the pairing.

## [1.1.3] - 2026-08-28

### Changed

- **`x-python-default: {}` renders as the empty-instance factory** — `field(default_factory=lambda: X())`, the spelling a non-required class field already gets — instead of `X.from_dict({})`. A non-empty dict still builds through `from_dict`. On a nullable field `{}` wins over `None`; on an enum it is a schema error.

### Fixed

- **An inherited constructor default now travels through an intermediate redeclaration.** `ActivityData.state` (`x-python-default: {}`) → `QuizData` narrows `state` without restating it → `StatementQuizData` narrows it again: the grandchild lost the default (the cross-file chain flattened properties with `dict.update`, the in-file chain replaced the ancestor's field outright) and came out with a bare required field, so `StatementQuizData(problem=...)` raised. Both chains now carry the ancestor's `x-<lang>-default` / `-default-code` / `-imports` onto a redeclaration that declares none for that language.

## [1.1.2] - 2026-08-28

### Added

- **A redeclared property inherits the base's constructor default.** An allOf subclass that narrows a property's type (`state: SubState` over `state: BaseState`) keeps the base's `x-python-default` / `x-python-default-code` unless it declares its own for that language — across files too. Base-class properties now carry their `x-<lang>-*` keys into the IR.

## [1.1.1] - 2026-08-28

### Fixed

- **`x-python-default` on an optional scalar** kept the analyzer's `T | None` widening (`keyword: str | None = ""`); a non-null constructor default now clears it exactly as a schema `default` does.

## [1.1.0] - 2026-08-28

### Added

- **Swift backend**: `Codable` structs / discriminated enums with lenient decoding, `swift_conformances`, `swift_nonisolated`, `x-swift-type`, `x-swift-known-subtypes`; a tree-sitter based `SwiftAstMerger`.
- **`x-python-type` / `x-csharp-type` / `x-swift-type`** are one mechanism: every schema node's `x-<language>-type` keys are collected once into `TypeRef.type_overrides`, and each backend reads its own key in `translate_type`. `x-python-type` now works on every node kind (a bare primitive emitted `str` before); `x-csharp-type` is honoured for the first time.
- **`x-omit-when-default`** (Python): per-property counterpart of `exclude_default_value_from_json`, through the same `dataclasses_json` `config(exclude=...)` mechanism (or the configured helper). A class-typed field is compared against a freshly built default instance, so an all-default sub-object is omitted.
- **`x-python-default` / `x-python-default-code` / `x-python-imports`** (Python): a constructor-level default that leaves `required` untouched — the wire contract and the constructor's convenience are answered separately. JSON values render like `default` (`null` widens to `X | None`), code is emitted verbatim with its imports.
- **`formatter.sort_imports`** (default `true`): run ruff's isort rules over the output with the destination path as `--stdin-filename`, so the *consuming* project's first-party grouping applies and regeneration stops churning imports.
- Tests that execute the generated module for every runtime-only behaviour above.

### Changed

- **Python defaults** come from one rule: a non-required class-typed field defaults to an empty instance only when the class is empty-constructible (every field, inherited ones included, has a default; enums never do), otherwise to `None` with the annotation widened. A dict `default` on a class-typed field builds the instance through `from_dict` instead of leaving a raw dict in the field. The backend no longer mutates the IR's nullability.
- **Python imports** are emitted `__future__` first, then alphabetically; grouping is ruff's job (see `sort_imports`). The formatter passes `--line-length` only when no destination path is known — with one, the consuming project's configuration decides.
- **Python merge** treats module-level type aliases and class decorators as schema-owned, like bases and fields: an existing alias is updated in place (moving below a member defined further down), generated decorators replace their namesakes while hand-added ones survive. A base generated undecorated (see 1.0.1 → 1.1.0 fix below) heals on the next merge.
- **`CodeGeneratorConfig.from_dict` / `to_dict`** are driven by `dataclasses.fields()`: enum fields coerce, nested blocks recurse, `from_dict(to_dict(c)) == c`, unknown keys warn.
- **Mergers**: `AstMerger.merge_files` is the single abstract entry; C# and Swift share a `TreeSitterMerger` base (parser, error reporting, node lookups, `// CUSTOM CODE` sections, import placement from the last import node rather than a text scan). `CustomCode` lost the five fields no merger wrote.
- Swift `_import_module` reads the declaration's `identifier` child, so `@testable import X`, `@_exported import X` and `import struct Foundation.Date` are all recognised.
- Test suite: the root duplicates of the `v3/` suites (`test_functional.py`, `test_merge.py`, `test_pipeline_integration.py`) and the data-less `v3/test_code_merge_roundtrip.py` are gone, their unique tests folded into `v3/`; `test_csharp_ast_roundtrip.py` points at its data again. No test is permanently skipped.

### Fixed

- Polymorphic base classes were emitted without `@dataclass_json` / `@dataclass`, silently dropping inherited fields from subclasses' `__init__` and `to_dict`.
- `x-python-type` was parsed but never applied.
- A new union alias was dropped by the merge, or would have been placed above the classes it unions (a `NameError`, since `A | B` evaluates eagerly).
- `from_dict` ignored `formatter` and most `output` keys, leaving `config.formatter` a raw dict.
- A non-required `$ref` to an enum defaulted to `E()`, which raises; an optional field that stopped being optional kept its `= None` across merges.
- Attributed Swift imports were lost on regeneration.
- A `$ref` to a class with required fields defaulted to `X()`, which raises at construction; a `$ref` with a dict default held a dict.
- `class_default_strategy` config option (default `schema`): a non-required class-typed field keeps the schema's type and gets `field(default_factory=lambda: X())`; `constructible` widens non-constructible ones to `X | None = None` (the 1.1.0 behaviour before this option). Enums always default to `None`; a self-referential optional reference is an error under `schema` (the schema must declare it nullable) and widened under `constructible`.

## [1.0.1] - 2024-12-19

### Added

#### Quoted Types for Python
- **New config option**: `quoted_types_for_python: list[str] = []` - List of type names to quote in Python type references to handle circular type definitions
- **Forward reference support**: Types listed in this config are automatically quoted (e.g., `List["Node"]` instead of `List[Node]`)
- **Circular dependency resolution**: Solves Python circular import issues for recursive/self-referential types
- **Example**: Configure `"quoted_types_for_python": ["Node", "Tree"]` to generate `parent: "Node"` instead of `parent: Node`

#### Import Optimization for Python & C#
- **Smart import detection**: Only imports modules that are actually used in the generated code
- **Reduces linting errors**: No more unused import warnings for `Enum`, `Literal`, `Any`, `List`, `Tuple`, or `ABC`
- **Context-aware imports**: Imports are added based on actual feature usage (arrays need `List`, const values need `Literal`, inheritance needs `ABC`, etc.)
- **Language-agnostic**: New `register_import_needed()` function abstracts import registration for both Python and C#
- **Type-safe design**: Uses `ImportType` enum instead of strings for better IDE support and error checking
- **Centralized mapping**: Abstract import types (e.g., `ImportType.LIST`) map to language-specific imports via dedicated dictionaries
- **Maintainable architecture**: Separate `PYTHON_IMPORT_MAP` and `CS_IMPORT_MAP` for clean language-specific mappings
- **Security features**: `ValueError` exceptions for unsupported import types, complete mapping coverage with `None` for language-specific features
- **Robust error handling**: Clear error messages for debugging, prevents silent failures and missing imports
- **Example**: Simple classes only import basic dependencies, complex schemas only import what they need

## [1.0.0] - 2024-12-19

### Added

#### Generation Comment Feature
- **New config option**: `add_generation_comment: bool = True` - Controls whether to add a generation command comment at the top of generated files
- **Automatic command line tracking**: Generated files now include a comment showing the simplified command used to create them
- **Smart path simplification**: Long file paths are reduced to just filenames for cleaner comments
- **Example**: `# Generated by json_schema_to_code v1.0.0 : json_schema_to_code schema.json output.py -c config.json -l python`

#### Union Type Support
- **anyOf support**: JSON Schema `anyOf` patterns are now converted to Python union types (e.g., `int | str`)
- **oneOf support**: JSON Schema `oneOf` patterns are now converted to Python union types
- **Configurable union style**: New `use_inline_unions: bool = False` config option
  - `True`: Generate inline unions like `int | str`
  - `False`: Generate type aliases like `IntOrStr = int | str`
- **Dynamic type alias generation**: Type aliases are automatically collected and added to file prefix
- **Consistent sorting**: Union types are sorted alphabetically for consistent output

#### Comment Field Filtering
- **Schema comment support**: Fields starting with `_comment` are now filtered out during code generation
- **String field filtering**: String values in schema definitions are automatically ignored
- **Cleaner generated code**: Removes documentation/comment artifacts from schemas

#### Class-level Union Handling
- **Inheritance patterns**: `anyOf` and `oneOf` at class definition level now properly handled
- **Non-object type support**: Classes with non-object types are converted to appropriate union types

### Enhanced

#### Template System
- **Dynamic type aliases**: Python prefix template now dynamically includes generated type aliases
- **Conditional generation comment**: Template conditionally includes generation comment based on config

#### Error Handling
- **Robust type checking**: Added proper checks for missing `type` fields in schema properties
- **Better schema validation**: Improved handling of malformed or incomplete schema definitions

#### Test Coverage
- **Comprehensive test suite**: Added 15+ new test cases covering all new features
- **JSON-driven tests**: Test cases defined in JSON files for easier maintenance and extension
- **Self-contained tests**: Reorganized test directory structure to be independent of personal file paths
- **Multiple test categories**:
  - Union type generation (inline vs type aliases)
  - Comment field filtering
  - Generation comment functionality
  - Real-world schema handling (UI hierarchy)

### Fixed

#### Code Generation
- **Output path construction**: Fixed double-appending of filenames in output paths
- **Type alias scope**: Type aliases are now properly scoped and collected before template rendering
- **Schema preprocessing**: Improved handling of non-dictionary schema elements

#### Module Execution
- **Import resolution**: Fixed relative import issues by using proper module execution (`python -m`)
- **Path handling**: Improved handling of absolute vs relative paths in various contexts

### Changed

#### Test Organization
- **Directory restructure**: Moved `tests/schemas/` to `tests/test_data/schemas/` for better organization
- **Reference file updates**: Updated test reference files to match new consistent sorting behavior
- **Removed personal paths**: Eliminated references to user-specific file paths in test suite

#### Code Architecture
- **Refactored union handling**: Consolidated union type generation logic into single `union_type` method
- **Removed redundancy**: Eliminated `_handle_union_types` wrapper method
- **Enhanced configuration**: Extended `CodeGeneratorConfig` with new options and better defaults

### Technical Details

#### New Configuration Options
```python
class CodeGeneratorConfig:
    use_inline_unions: bool = False          # Control union type style
    add_generation_comment: bool = True      # Add generation command comment
```

#### Supported Union Patterns
- `{"anyOf": [{"type": "string"}, {"type": "number"}]}` → `str | float` or `StrOrFloat`
- `{"oneOf": [{"type": "integer"}, {"type": "string"}]}` → `int | str` or `IntOrStr`
- Class-level unions for inheritance patterns

#### Generated Comment Format
```python
# Generated by json_schema_to_code v1.0.0 : simplified_command with_simplified_paths
```

### Compatibility

- **Backward compatible**: All existing functionality preserved
- **Default behavior**: Generation comments enabled by default, can be disabled
- **Existing schemas**: All previously supported schemas continue to work
- **Template compatibility**: Changes to templates are additive only

## Version History

This release represents a major enhancement to the json_schema_to_code package, introducing robust union type support, generation tracking, and comprehensive test coverage. The version 1.0.0 reflects the maturity and stability of these core features.

[1.0.0]: https://github.com/randomwalkteam/json_schema_to_code/releases/tag/v1.0.0
