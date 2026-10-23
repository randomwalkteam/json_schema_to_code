# CLAUDE.md -- json_schema_to_code context for AI agents

## Project overview

**json_schema_to_code** generates strongly-typed classes from JSON Schema definitions for Python, C#, Swift and C++. It uses a five-phase AST-based pipeline: parse, analyze, generate AST, serialize, format/merge.

- **Stack**: Python 3.12+, Click (CLI), ruff (formatting), tree-sitter (C# / Swift merge parsing)
- **Entry point**: `json_schema_to_code` CLI command (defined in `pyproject.toml`)

## Key paths

| Area | Path |
|------|------|
| CLI entry point | `json_schema_to_code/json_schema_to_code.py` |
| Pipeline config | `json_schema_to_code/pipeline/config.py` |
| Main generator | `json_schema_to_code/pipeline/generator.py` |
| Schema AST nodes | `json_schema_to_code/pipeline/schema_ast/nodes.py` |
| Schema parser | `json_schema_to_code/pipeline/schema_ast/parser.py` |
| IR nodes | `json_schema_to_code/pipeline/analyzer/ir_nodes.py` |
| Schema analyzer | `json_schema_to_code/pipeline/analyzer/analyzer.py` |
| Reference resolver | `json_schema_to_code/pipeline/analyzer/reference_resolver.py` |
| Name resolver | `json_schema_to_code/pipeline/analyzer/name_resolver.py` |
| Backend base class | `json_schema_to_code/pipeline/ast_backends/base.py` |
| Python backend | `json_schema_to_code/pipeline/ast_backends/python_ast_backend.py` |
| C# backend | `json_schema_to_code/pipeline/ast_backends/csharp_ast_backend.py` |
| C# serializer | `json_schema_to_code/pipeline/ast_backends/csharp_serializer.py` |
| Swift backend | `json_schema_to_code/pipeline/ast_backends/swift_ast_backend.py` |
| C++ backend | `json_schema_to_code/pipeline/ast_backends/cpp_ast_backend.py` |
| C++ serializer | `json_schema_to_code/pipeline/ast_backends/cpp_serializer.py` |
| Merger base | `json_schema_to_code/pipeline/merger/base.py` |
| Python merger | `json_schema_to_code/pipeline/merger/python_merger.py` |
| Tree-sitter merger base | `json_schema_to_code/pipeline/merger/tree_sitter_merger.py` |
| C# merger | `json_schema_to_code/pipeline/merger/csharp_merger.py` |
| Swift merger | `json_schema_to_code/pipeline/merger/swift_merger.py` |
| Atomic writer | `json_schema_to_code/pipeline/merger/atomic_writer.py` |
| Ruff formatter | `json_schema_to_code/pipeline/formatters/ruff_formatter.py` |
| Tests | `json_schema_to_code/tests/` |
| Test data | `json_schema_to_code/tests/test_data/` |

## CLI usage

```bash
json_schema_to_code <schema.json> <output_file> [options]

# Options:
#   --name, -n        Root class name (default: schema filename stem)
#   --config, -c      JSON config file path
#   --language, -l    Target language: "cs" (default), "python", "swift" or "cpp"
#   --add-validation  Add runtime validation code
#   --merge-strategy  "error" | "merge" | "delete"
```

## Pipeline architecture

### Phase 1: Schema Parsing (`schema_ast/parser.py`)

`SchemaParser.parse()` converts raw JSON Schema dict into a `SchemaAST` tree. No references are resolved at this stage.

**Schema AST node types** (in `schema_ast/nodes.py`):

| Node | Description |
|------|-------------|
| `PrimitiveNode` | `string`, `integer`, `number`, `boolean`, `null`, `object` |
| `ConstNode` | Const literal values |
| `EnumNode` | Enum types (with optional `x-enum-members`) |
| `RefNode` | Unresolved `$ref` references |
| `ArrayNode` | Array types (single item or tuple items) |
| `ObjectNode` | Object types with `PropertyDef` list |
| `UnionNode` | `oneOf` / `anyOf` unions |
| `AllOfNode` | Inheritance via `allOf` (base_ref + extension) |
| `DefinitionNode` | Schema definition entry (`$defs` / `definitions`) |

### Phase 2: Analysis (`analyzer/analyzer.py`)

`SchemaAnalyzer.analyze()` transforms the Schema AST into an IR (Intermediate Representation). Resolves `$ref` references, builds inheritance graph, handles discriminated unions.

**IR node types** (in `analyzer/ir_nodes.py`):

| IR Node | Description |
|---------|-------------|
| `IR` | Root container: classes, enums, type aliases, imports |
| `ClassDef` | Class with fields, base_class, subclasses, discriminator_property |
| `FieldDef` | Field with name, type_ref, is_required, default_value, is_const |
| `EnumDef` | Enum with members (name -> value mapping) |
| `TypeAlias` | Union type alias |
| `TypeRef` | Resolved type reference with kind, name, type_args, nullable, `type_overrides` (`x-<language>-type`, language -> verbatim type) |
| `ImportDef` | Import statement |

**`TypeKind` enum values**: `PRIMITIVE`, `CLASS`, `ARRAY`, `TUPLE`, `DICT`, `UNION`, `OPTIONAL`, `ENUM`, `CONST`, `ANY`, `TYPE_ALIAS`

### Phase 3: AST Backend Generation (`ast_backends/`)

`AstBackend.generate(ir)` converts IR to language-native AST. Backends inherit from `AstBackend` and implement:
- `generate(ir: IR) -> str`
- `translate_type(type_ref: TypeRef) -> str`
- `format_default_value(value: Any, type_ref: TypeRef) -> str`

**Backends**: `PythonAstBackend`, `CSharpAstBackend`, `SwiftAstBackend`, `CppAstBackend`. Each reads its own key from `TypeRef.type_overrides` (`python`, `csharp`, `swift`, `cpp`) in `translate_type`. Swift and C++ have no inheritance in their model: the analyzer flattens `allOf` bases for both (`FLATTENING_LANGUAGES`), and discriminated bases become a Swift enum / a C++ `std::variant`.

The Python backend decides defaults from the IR alone: a non-required class-typed field gets `field(default_factory=lambda: X())` only when `X` is empty-constructible (every field, inherited ones included, has a default; enums never are), else `None` with the annotation widened to `X | None`. The IR is not mutated for this.

### Phase 4: Serialization

- Python: `ast.unparse()` (standard library)
- C#: custom `CSharpSerializer` in `csharp_serializer.py`

### Phase 5: Formatting & Merging

- **Formatting**: `RuffFormatter` for Python (configurable via `FormatterConfig`)
- **Merging**: `PythonAstMerger` (stdlib `ast`, order-preserving walk) and `CSharpAstMerger` / `SwiftAstMerger` (both on `TreeSitterMerger`, which owns the parser, node lookups, `// CUSTOM CODE` sections and import placement). `AstMerger.merge_files` is the single entry point
- **Atomic writes**: `AtomicWriter` writes to temp file then renames

## Configuration

`CodeGeneratorConfig` (in `pipeline/config.py`), loaded from JSON via `--config`:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `ignore_classes` | `list[str]` | `[]` | Classes to skip |
| `global_ignore_fields` | `list[str]` | `[]` | Fields to exclude globally |
| `order_classes` | `list[str]` | `[]` | Class generation order |
| `ignoreSubClassOverrides` | `bool` | `false` | Skip redundant subclass field overrides (keeps const overrides) |
| `drop_min_max_items` | `bool` | `false` | Ignore array length constraints |
| `use_array_of_super_type_for_variable_length_tuple` | `bool` | `true` | Use arrays for variable-length tuples |
| `use_tuples` | `bool` | `true` | Generate tuple types |
| `use_inline_unions` | `bool` | `false` | Inline unions vs type aliases |
| `add_generation_comment` | `bool` | `true` | Comment at top of file |
| `quoted_types_for_python` | `list[str]` | `[]` | Types to quote for forward references |
| `use_future_annotations` | `bool` | `true` | Add `from __future__ import annotations` |
| `exclude_default_value_from_json` | `bool` | `false` | Exclude defaults from JSON serialization |
| `optional_field_helper_module` | `str \| None` | `null` | With excluded defaults: `null` inline lambdas, `""` inline helper, `"a.b"` import the helper |
| `add_validation` | `bool` | `false` | Add runtime validation |
| `swift_conformances` | `list[str]` | `["Codable"]` | Protocols generated Swift types conform to |
| `swift_nonisolated` | `bool` | `false` | Prefix generated Swift types with `nonisolated` |
| `cpp_namespace` | `str` | `""` | Namespace wrapping generated C++ declarations |
| `cpp_json_include` | `str` | `"<nlohmann/json.hpp>"` | nlohmann/json include directive |
| `cpp_additional_includes` | `list[str]` | `[]` | Extra C++ includes |
| `external_ref_base_module` | `str` | `""` | Base module for external `$ref` imports (Python) |
| `external_ref_schema_to_module` | `dict[str,str]` | `{}` | Schema path -> module mapping |
| `csharp_namespace` | `str` | `""` | C# namespace |
| `csharp_additional_usings` | `list[str]` | `[]` | Extra C# using statements |
| `schema_base_path` | `str` | `""` | Base path for resolving external `$ref` schemas from disk |

**Output config** (nested under `output` in JSON):

| Option | Values | Default |
|--------|--------|---------|
| `mode` | `error_if_exists`, `overwrite`, `merge` | `merge` |
| `merge_strategy` | `error`, `merge`, `delete` | `error` |

**Formatter config** (nested under `formatter`):

| Option | Default | Description |
|--------|---------|-------------|
| `enabled` | `true` | Enable ruff formatting |
| `sort_imports` | `true` | Run ruff's isort rules (grouping needs the consuming project's config) |
| `line_length` | `100` | Only when no destination path is known; otherwise the consuming project's ruff config decides |
| `target_version` | `""` | Python target (e.g. `py313`) |
| `string_normalization` | `true` | |
| `magic_trailing_comma` | `true` | |

`CodeGeneratorConfig.from_dict` / `to_dict` are driven by `dataclasses.fields()`: enum fields coerce from their value, nested blocks recurse, unknown keys warn.

## Code merging

When `output.mode = "merge"`, the generator preserves custom code from the existing file.

Python walks the existing module in order. Schema-owned and taken from the generated code where they already stand: class fields (unless marked, or carrying `field(metadata=...)`), bases, decorators (generated ones replace their namesakes, hand-added ones survive), and module-level type aliases (an alias that gained a member defined further down moves below it). Kept from the file: custom imports, constants, custom classes and methods, comments, docstrings. New classes and imports are appended.

C# / Swift extract and reinsert: custom `using` / `import` statements (placed after the last generated one), custom members and top-level declarations, member comments, and `// CUSTOM CODE START/END` sections.

**Merge strategy** controls what happens with value members in the existing file that are not in the new generated code:
- `error`: Raise error (safest)
- `merge`: Keep extra members
- `delete`: Remove extra members

**`# jstc-no-merge`**: Add this comment to a line in existing code to prevent merging on that line.

## Schema extensions

| Extension | Where | Description |
|-----------|-------|-------------|
| `x-enum-members` | Enum definition | Maps enum values to custom Python member names |
| `x-ref-class-name` | `$ref` | Override the resolved class name |
| `x-csharp-generate` | Schema root or `$defs` entry | Control whether C# code is generated (default: `true`) |
| `x-csharp-using` | Schema root | Add custom `using` statements to generated C# files |
| `x-csharp-implements` | Object | C# interface to implement |
| `x-csharp-properties` | Object | C# interface property mappings |
| `x-csharp-known-subtypes` | Object definition | Declare cross-schema polymorphic subtypes for C# `JsonSubtypes` |
| `x-swift-known-subtypes` | Object definition | Swift counterpart (falls back to the C# key) |
| `x-python-type`, `x-csharp-type`, `x-swift-type` | Any type node | Emit this type verbatim for that language (collected once into `TypeRef.type_overrides`) |
| `x-omit-when-default` | Property | Python: leave the field out of `to_dict()` while it holds its default; class-typed fields compare against a fresh default instance |

### `x-csharp-generate`

Controls C# code generation at two levels:

**Schema root** -- skip C# generation for the entire schema:
```json
{
    "x-csharp-generate": false,
    "$defs": { "GeneratorConfig": { ... } }
}
```
Python dataclasses are still generated. Use for backend-only schemas, generator configs, or objects sharing a generic C# prefab.

**Individual `$defs` entries** -- selectively exclude definitions from C#:
```json
{
    "$defs": {
        "ActivityData": { "x-csharp-generate": false, ... },
        "Problem":      { "x-csharp-generate": false, ... },
        "State":        { "x-csharp-generate": false, ... },
        "UIData":       { ... }
    }
}
```
Only `UIData` generates a C# class; Python gets all four. This is the standard pattern for activity schemas where the agent sends only UI data to Unity.

### `x-csharp-using`

Array of strings added as `using` statements in the generated C# file. Supports both namespace imports and type aliases:

```json
{
    "x-csharp-using": [
        "EduObject.Activities.Quiz",
        "QuizModel = EduObject.Exercise.Quiz.Quiz"
    ]
}
```

Generates:
```csharp
using EduObject.Activities.Quiz;
using QuizModel = EduObject.Exercise.Quiz.Quiz;
```

The generator also auto-detects common usings (`Newtonsoft.Json`, `System.Collections.Generic`, etc.).

### `x-csharp-known-subtypes`

Declares polymorphic subtypes that live in **other schema files**. The generator emits `[JsonConverter(typeof(JsonSubtypes), "...")]` and `[JsonSubtypes.KnownSubType(...)]` attributes on the base class, plus `using` directives for each subtype's namespace.

```json
{
    "WidgetData": {
        "discriminator": { "propertyName": "type" },
        "x-csharp-known-subtypes": [
            { "class": "ImageWidgetUIData", "value": "image", "using": "EduObject.Widgets.Image" },
            { "class": "TextWidgetUIData", "value": "text", "using": "EduObject.Widgets.Text" }
        ]
    }
}
```

Each entry requires `class` (C# class name) and `value` (discriminator value). `using` is optional (omit if the subtype is in the same namespace). **When adding a new widget type, you must add an entry to this list** in the base schema so the code generator emits the correct `JsonSubtypes` registration.

## Schema validation errors

The analyzer raises fatal errors for invalid schema patterns:

**`default: null` on non-nullable types:** If a property has `"default": null` but the type does not allow null, the analyzer raises `ValueError`. Fix by either making the type nullable or removing `"default": null`:

```json
// WRONG: $ref is not nullable, but default is null
{ "action": { "$ref": "#/$defs/UIAction", "default": null } }

// CORRECT: use oneOf to make it nullable
{ "action": { "oneOf": [{"$ref": "#/$defs/UIAction"}, {"type": "null"}], "default": null } }

// CORRECT: remove default if it shouldn't be nullable
{ "action": { "$ref": "#/$defs/UIAction" } }

// CORRECT: primitive nullable type
{ "count": { "type": ["integer", "null"], "default": null } }
```

**C# union alias with invalid identifier:** In C#, union types are represented as named type aliases (e.g., `IntOrString`). If the generated alias name contains characters invalid in C# identifiers (e.g., `List<T>` from an array union member), the backend raises `ValueError`. This happens with `anyOf`/`oneOf` unions containing array types.

## Inheritance

`allOf` with a `$ref` + extension object maps to class inheritance:
- The `$ref` becomes the base class
- Extension properties become subclass fields
- Discriminator is auto-detected from const fields (typically `type`)
- C#: `JsonSubTypes` attributes for polymorphic deserialization
- Python: `ABC` for abstract base classes

## Testing

**Framework**: pytest

**Test locations**:
- `json_schema_to_code/tests/` -- main test files
- `json_schema_to_code/tests/v3/` -- v3 pipeline-specific tests
- `json_schema_to_code/tests/test_data/` -- test schemas, reference outputs, merge fixtures

**Key test files**:

| File | Tests |
|------|-------|
| `v3/test_functional.py` | Data-driven generation cases from `test_data/functional/*.json` |
| `v3/test_merge.py` | Code merging: Python (aliases, decorators, markers, comments) and C# |
| `v3/test_pipeline_integration.py` | Full pipeline over `test_data/pipeline/integration` schemas |
| `test_code_merge_roundtrip.py` | Merge roundtrip over `test_data/code_merge` |
| `test_optional_field_defaults.py`, `test_omit_when_default.py`, `test_python_base_and_type_overrides.py` | Python runtime behaviour, by executing the generated module |
| `test_external_base_class_imports.py` | External `$ref` import resolution |
| `test_csharp_ast_roundtrip.py`, `test_swift_ast_roundtrip.py` | tree-sitter parsing and merging |
| `v3/test_cpp_backend.py` | C++ output shapes, plus compiling the generated header with `-Werror` and round-tripping JSON (needs a C++20 compiler and nlohmann/json: found via `NLOHMANN_JSON_INCLUDE`, the conda prefixes or the usual system paths, skipped otherwise) |
| `test_config_from_dict.py`, `test_import_sorting.py` | Config loading, ruff isort pass |
| `test_validation_rules.py` | Validation rule generation |
| `test_cli_utils.py` | CLI utility functions |

No test is left permanently skipped: a data-driven test whose data is missing fails, not skips.

**Running tests**:
```bash
python -m pytest json_schema_to_code/tests/ -v
```

**Test data patterns**:
- `test_data/test_cases/<case>/` -- schema + reference outputs (`.py`, `.cs`)
- `test_data/code_merge/<case>/` -- schema + existing dataclass files for merge testing
- `test_data/functional/*.json` -- parameterized functional test data
- `test_data/pipeline/` -- pipeline-specific test schemas

## Python API

```python
from json_schema_to_code import PipelineGenerator, CodeGeneratorConfig

config = CodeGeneratorConfig()
generator = PipelineGenerator("MySchema", schema_dict, config, "python")

# Generate code string
code = generator.generate()

# Or generate and write to file (handles merging, formatting, atomic write)
generator.generate_to_file(Path("output.py"))
```

## Name resolution

- Definition names are converted to PascalCase
- Inline objects: `{ParentClass}{FieldName}` in PascalCase
- C# keywords escaped with `@` prefix
- Collision handling via `NameResolver`

## Ruff configuration

The project's `pyproject.toml` configures ruff with `line-length = 200` for the project source itself. Generated code is formatted with the *consuming* project's ruff configuration (the destination path is passed as `--stdin-filename`); `FormatterConfig.line_length` only applies when no destination is known.
