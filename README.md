# JSON Schema to Code Generator

A Python package that generates strongly-typed classes from JSON Schema definitions. Supports code generation for **Python**, **C#**, **Swift** and **C++** with full inheritance, polymorphism, and type safety.

## Features

- **Multi-language support**: Python dataclasses, C# classes, Swift `Codable` structs, and C++20 structs with nlohmann/json serialization
- **Full JSON Schema compliance**: Supports definitions, references, inheritance (`allOf`), enums, and complex types
- **Type safety**: Generates strongly-typed code with proper nullable handling
- **Inheritance and polymorphism**: Handles base classes and subclass discrimination
- **AST-based pipeline**: Schemas are compiled to a language-native AST, not rendered from text templates
- **Merge-aware output**: Regenerating a file preserves hand-written methods, imports and constants, while fields, bases, decorators and type aliases follow the schema
- **Runtime validation**: Optionally emits constraint checks from the schema
- **Configuration support**: Flexible configuration options for customizing output
- **Command-line interface**: Easy-to-use CLI tool

## Installation

The package is not published on PyPI — install it from the repository:

```bash
pip install git+https://github.com/randomwalkteam/json_schema_to_code.git@main
```

### Dependencies

- Python 3.12+
- Click (CLI interface)

Two capabilities are optional and degrade gracefully when absent:

- **Merging C# / Swift** needs `tree-sitter`, `tree-sitter-c-sharp` and `tree-sitter-swift`. Python merging uses the standard library `ast` and always works.
- **Formatting Python output** shells out to `ruff` if it is on `PATH`; without it the generated code is emitted unformatted.

Both are included in the `dev` extra (`pip install -e ".[dev]"`).

## Quick Start

### Command Line Usage

```bash
# Basic usage - generate C# classes
json_schema_to_code schema.json output.cs

# Generate Python dataclasses
json_schema_to_code schema.json output.py --language python

# Generate Swift Codable structs
json_schema_to_code schema.json output.swift --language swift

# Generate a C++ header (C++20 structs + nlohmann/json)
json_schema_to_code schema.json output.hpp --language cpp

# Use a configuration file
json_schema_to_code schema.json output.cs --config config.json

# Specify a custom class name
json_schema_to_code schema.json output.cs --name MyRootClass
```

### Command Line Options

- `path`: Input JSON Schema file (required)
- `output`: Output file path (required)
- `--language, -l`: Target language (`cs`, `python`, `swift` or `cpp`, default: `cs`)
- `--config, -c`: Configuration file path (optional)
- `--name, -n`: Root class name (optional, defaults to schema filename)
- `--add-validation`: Add runtime validation in `__post_init__` (Python) or the constructor (C#)
- `--merge-strategy`: How to treat existing members absent from the generated code — `error` (default), `merge` (keep them), `delete` (remove them)

## Configuration

Create a JSON configuration file to customize code generation:

```json
{
  "ignore_classes": ["TempClass", "DebugInfo"],
  "global_ignore_fields": ["_internal", "__debug"],
  "order_classes": ["BaseMessage", "ErrorMessage"],
  "quoted_types_for_python": ["Node", "Tree"],
  "output": {
    "mode": "merge",
    "merge_strategy": "error"
  },
  "formatter": {
    "line_length": 120
  }
}
```

### Configuration Options

Unknown keys are skipped with a warning, so a config file can carry options for several projects.

**Selecting what is generated**

- **`ignore_classes`**: List of class names to skip during generation
- **`global_ignore_fields`**: Field names to exclude from all classes
- **`order_classes`**: Order in which classes are emitted (empty = definition order)
- **`ignoreSubClassOverrides`** (default `false`): Skip property overrides in subclasses

**Type mapping**

- **`use_tuples`** (default `true`): Generate tuple types for fixed-length arrays
- **`use_array_of_super_type_for_variable_length_tuple`** (default `true`): Use arrays for variable-length tuples
- **`drop_min_max_items`** (default `false`): Ignore `minItems`/`maxItems` array length constraints
- **`use_inline_unions`** (default `false`): Emit inline union syntax instead of named type aliases
- **`add_validation`** (default `false`): Emit runtime constraint checks

**Python specifics**

- **`quoted_types_for_python`**: Type names to quote in type references (e.g. `list["MyType"]`) to handle circular definitions
- **`use_future_annotations`** (default `true`): Emit `from __future__ import annotations`
- **`exclude_default_value_from_json`** (default `false`): Omit fields still at their default from JSON output
- **`class_default_strategy`** (default `schema`): default of a non-required class-typed field. `schema` keeps the schema's type — nullable → `None`, otherwise `field(default_factory=lambda: X())` (construction raises when `X` needs arguments); `constructible` widens such a field to `X | None = None`. Enums always default to `None`; a self-referential optional reference is an error under `schema` (declare it nullable) and widened under `constructible`.
- **`optional_field_helper_module`**: With the option above — `null` for inline lambdas, `""` to inline the helper function, or a module path to import it from
- **`add_generation_comment`** (default `true`): Add the "Generated by" header

**C# specifics**

- **`csharp_namespace`**: Namespace to wrap the generated types in
- **`csharp_additional_usings`**: Extra `using` directives

**Swift specifics**

- **`swift_conformances`** (default `["Codable"]`): Protocols the generated structs and enums conform to, e.g. `["Decodable", "Hashable", "Sendable"]` for a decode-only, strict-concurrency project
- **`swift_nonisolated`** (default `false`): Prefix generated types with `nonisolated`, and restate it on the `init(from:)` extension — needed under default-MainActor isolation

**C++ specifics**

- **`cpp_namespace`** (default `""`): Namespace wrapping the generated declarations, e.g. `"montage::model"`
- **`cpp_json_include`** (default `"<nlohmann/json.hpp>"`): Include directive for nlohmann/json
- **`cpp_additional_includes`** (default `[]`): Extra includes, e.g. the header declaring an `x-cpp-type`

**External references**

- **`schema_base_path`**: Base directory for resolving external `$ref` paths from disk
- **`external_ref_base_module`**: Base module for imports generated from external `$ref`s
- **`external_ref_schema_to_module`**: Explicit schema-name → module-path overrides

**`output` block**

- **`mode`**: `merge` (default), `overwrite`, or `error_if_exists`
- **`merge_strategy`**: `error` (default), `merge`, or `delete` — what to do with existing members absent from the generated code
- **`output_path`**: Default output path
- **`validate_before_write`** (default `true`): Parse the result before replacing the file

**`formatter` block** (Python only)

- **`enabled`** (default `true`), **`target_version`**, **`string_normalization`** (default `true`), **`magic_trailing_comma`** (default `true`)
- **`sort_imports`** (default `true`): run ruff's isort rules over the output. The backend emits imports in a fixed order (`__future__`, then alphabetical); only ruff, reading the *consuming* project's configuration, knows which modules are first-party there and can group them
- **`line_length`** (default `100`): used only when the destination file is not known (e.g. `Formatter.format()` called without a path). When it is, the consuming project's own ruff configuration decides, as it does for every other formatting option

## Supported JSON Schema Features

### Basic Types
- `string`, `integer`, `number`, `boolean`, `null`
- `array` with typed items
- `object` with properties
- `enum` values

### Advanced Features
- **References**: `$ref` to definitions
- **Inheritance**: `allOf` for class hierarchies
- **Polymorphism**: Automatic subclass discrimination
- **Union types**: Multiple type options (`oneOf`)
- **Const values**: Fixed literal values
- **Optional properties**: Nullable type generation
- **Custom enum member names**: Use `x-enum-members` to specify custom enum member names
- **Verbatim types**: `x-python-type`, `x-csharp-type`, `x-swift-type` and `x-cpp-type` name the type to emit for that language instead of the inferred one (see below)
- **Omit-when-default fields**: `x-omit-when-default` leaves a Python field out of the JSON while it still holds its default (see below)
- **Constructor defaults**: `x-python-default`, `x-python-default-code` and `x-python-imports` give a field a Python constructor default while `required` keeps describing the wire (see below)

### Custom Enum Member Names

By default, when generating Python enum classes, the generator uses the enum values directly as member names (e.g., `"N"` becomes `N = "N"`). To use more descriptive member names, you can use the `x-enum-members` extension:

```json
{
  "$defs": {
    "ElementState": {
      "type": "string",
      "enum": ["N", "H", "C"],
      "x-enum-members": {
        "N": "NORMAL",
        "H": "HIDDEN",
        "C": "CORRECT_ANSWER"
      }
    }
  }
}
```

The generator picks this up directly from the schema — no preprocessing step is needed:

```python
class ElementState(str, Enum):
    NORMAL = "N"
    HIDDEN = "H"
    CORRECT_ANSWER = "C"
```

**Note**: `x-enum-members` maps enum values (the keys) to member names (the values). All three languages honour it: C# additionally emits a `JsonConverter` mapping the members back to their string values, and Swift lower-camels the member names (`CORRECT_ANSWER` becomes `case correctAnswer`).

### Verbatim Types (`x-python-type`, `x-csharp-type`, `x-swift-type`, `x-cpp-type`)

Any schema node can name the type a language should emit for it, verbatim, when the inferred one is not what the code needs: a `$ref` to a scalar `$def` (no class is generated for it), a payload the language's JSON library cannot decode as the inferred union, a hand-written type living elsewhere. Each backend reads only its own key, so the three can sit side by side; nullability still comes from the schema.

```json
{
  "when":    {"type": "string", "x-python-type": "datetime", "x-csharp-type": "DateTime", "x-swift-type": "Date"},
  "widgets": {"type": "array", "items": {"type": "object", "x-swift-type": "NamedWidget"}, "default": []},
  "digest":  {"$ref": "#/$defs/Sha256", "x-python-type": "str"}
}
```

Python: `when: datetime`, `digest: str`. C#: `public DateTime When`. Swift: `let when: Date`, `var widgets: [NamedWidget] = []`. For Swift a property-level override ending in `?` is decoded leniently (`decodeIfPresent`), and a default survives only for empty container literals. C++ follows the same rules with `std::optional<...>` overrides and `{}` defaults.

A Python override naming a type that lives elsewhere brings its import via `x-python-imports` (which stands on its own — no constructor default required):

```json
{
  "amount": {"type": "string", "x-python-type": "Decimal", "x-python-imports": ["from decimal import Decimal"]}
}
```

### Omit-when-default Fields (`x-omit-when-default`, Python)

`exclude_default_value_from_json` omits every field still at its default from `to_dict()`; `x-omit-when-default: true` does the same for one property:

```json
{
  "count": {"type": "integer", "default": 0, "x-omit-when-default": true},
  "style": {"$ref": "#/$defs/Style", "x-omit-when-default": true}
}
```

Both go through `dataclasses_json`'s `config(exclude=...)` (or the project's helper when `optional_field_helper_module` is set). A field typed as a generated class is compared against a freshly built default instance, so an all-default sub-object is omitted too. The flag on a required field with no default is a schema error.

### Constructor Defaults (`x-python-default`, `x-python-default-code`, `x-python-imports`)

`required` describes the wire: validation demands the property. Whether a *constructor* needs it is a different question — a server-built placeholder, a generated id — and these keys answer it without loosening the schema:

```json
{
  "id":    {"type": "string", "x-python-default-code": "field(default_factory=unique_id)", "x-python-imports": ["from mylib.ids import unique_id"]},
  "month": {"type": "integer", "x-python-default": 0},
  "map":   {"$ref": "#/$defs/Map", "x-python-default": {"zoom": 2, "center": {"lat": 0, "lng": 0}}},
  "scene": {"$ref": "#/$defs/Scene", "x-python-default": null}
}
```

`x-python-default` takes a JSON value and renders it exactly like `default` would: a dict on a class-typed field becomes a `from_dict` factory, `null` widens the annotation to `X | None`. `x-python-default-code` is emitted verbatim; `field(` / `config(` in it pull in their own imports, anything else is listed in `x-python-imports` as plain `from m import n` / `import m` statements. The field stays `required` for validation and for the other backends; `default` remains the place for a value that holds on the wire too.

## Output Examples

### Input Schema
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "definitions": {
    "Shape": {
      "type": "object",
      "properties": {
        "type": {"type": "string"}
      },
      "required": ["type"]
    },
    "Circle": {
      "allOf": [
        {"$ref": "#/definitions/Shape"},
        {
          "properties": {
            "type": {"const": "circle"},
            "radius": {"type": "number"}
          },
          "required": ["radius"]
        }
      ]
    }
  }
}
```

### Generated C# Code
```csharp
#nullable enable

using JsonSubTypes;
using Newtonsoft.Json;
using System;

[Serializable]
[JsonConverter(typeof(JsonSubtypes), "type")]
[JsonSubtypes.KnownSubType(typeof(Circle), "circle")]
public class Shape
{
    [JsonProperty("type")]
    public virtual string Type { get; set; }
    public Shape(string type)
    {
        this.Type = type;
    }
    // Parameterless constructor for Unity editor and serialization
    public Shape() { }

}

[Serializable]
public class Circle : Shape
{
    [JsonProperty("type")]
    public override string Type => "circle";
    [JsonProperty("radius")]
    public float Radius { get; set; }
    public Circle(float radius): base("circle")
    {
        this.Radius = radius;
    }
    // Parameterless constructor for Unity editor and serialization
    public Circle() { }

}
```

### Generated Python Code
```python
from __future__ import annotations
from abc import ABC
from dataclasses import dataclass
from typing import Literal
from dataclasses_json import dataclass_json

@dataclass_json
@dataclass(kw_only=True)
class Shape(ABC):
    type: str

@dataclass_json
@dataclass(kw_only=True)
class Circle(Shape):
    radius: float
    type: Literal['circle'] = 'circle'
```

### Generated Swift Code
```swift
import Foundation

struct Shape: Codable {
    let type: String

    enum CodingKeys: String, CodingKey {
        case type = "type"
    }
}

struct Circle: Codable {
    var type: String = "circle"
    let radius: Double

    enum CodingKeys: String, CodingKey {
        case type = "type"
        case radius = "radius"
    }
}

extension Circle {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.type = try container.decodeIfPresent(String.self, forKey: .type) ?? "circle"
        self.radius = try container.decode(Double.self, forKey: .radius)
    }
}
```

### Generated C++ Code
```cpp
struct Circle {
    std::string type = "circle";
    double radius{};

    bool operator==(const Circle&) const = default;
};

using Shape = std::variant<Circle, Square>;

inline void from_json(const nlohmann::json& j, Circle& v) {
    if (const auto it = j.find("type"); it != j.end()) it->get_to(v.type);
    j.at("radius").get_to(v.radius);
}

inline void from_json(const nlohmann::json& j, Shape& v) {
    const auto& discriminator = j.at("type");
    if (discriminator == "circle") {
        v = j.get<Circle>();
        return;
    }
    // ...
    throw std::invalid_argument("unknown discriminator " + discriminator.dump() + " for Shape");
}
```

## Python API Usage

```python
from json_schema_to_code import PipelineGenerator, CodeGeneratorConfig
import json

# Load schema
with open('schema.json') as f:
    schema = json.load(f)

# Create configuration
config = CodeGeneratorConfig()
config.ignore_classes = ['TempClass']

# Generate code
generator = PipelineGenerator('MySchema', schema, config, 'python')
code = generator.generate()

# Save to file
with open('output.py', 'w') as f:
    f.write(code)
```

## Language-Specific Features

### Python Output
- Uses `@dataclass(kw_only=True)` with `dataclasses_json` for JSON serialization
- Supports union types with `|` syntax (Python 3.10+)
- Generates `Literal` types for const values
- Uses `ABC` for abstract base classes
- A non-required field typed as a generated class defaults to an empty instance when the class can be built with no arguments, and to `None` (widening the annotation) when it cannot — an enum, or a class with a required field, inherited ones included. A dict `default` on a class-typed field builds the instance through `from_dict`
- Imports are emitted `__future__` first, then alphabetically; grouping is left to ruff (`formatter.sort_imports`)

### C# Output
- Includes `[Serializable]` attributes
- Uses `JsonSubTypes` for polymorphic serialization
- Generates proper constructors with base class calls, plus a parameterless one for Unity
- Supports nullable reference types

### Swift Output
- Generates `struct`s with an explicit `CodingKeys` enum, conforming to whatever `swift_conformances` lists (`Codable` by default)
- String enums become raw-value `Codable` enums
- Schema `object` / untyped values map to an `AnyCodable` helper the project must supply
- **Decoding is deliberately lenient**: a field with a schema default emits as `var x = default` plus `decodeIfPresent ?? default`, and a non-required field with neither default nor nullability becomes optional. One absent or unexpected field cannot throw away an entire payload — which matters when the server is free to add fields ahead of the client.
- Defaults declared on a base class survive `allOf` flattening, including across external `$ref`s (Swift only; C#/Python constructor signatures are untouched)

### C++ Output
- A single header-only file of C++20 aggregate structs with default member initializers and a defaulted `operator==`; needs nlohmann/json 3.11+
- Same model as Swift: `allOf` bases are flattened, discriminated bases become `using Base = std::variant<...>` dispatched on the discriminator key
- Members are named after the JSON keys (sanitized: `content-type` becomes `content_type`, keywords get a trailing `_`), so the struct reads like the payload
- Enums become `enum class` with an explicit mapping; an unknown value or discriminator throws `std::invalid_argument` instead of decoding silently to a default
- Decoding is lenient like Swift: absent keys keep the member's default, non-required fields without a default are `std::optional`, nullable fields are `std::optional` written as `null`
- Untyped values (`object` without properties, heterogeneous unions) are `nlohmann::json`; dict defaults on a struct field use designated initializers
- Types are emitted in dependency order. A cycle of by-value references (e.g. a node holding an optional node) is an error: make the recursive reference an array (`std::vector` accepts incomplete types) or give it an `x-cpp-type`
- The header is fully generated, so `output.mode = "merge"` simply overwrites it: extend the types in a separate header

## Architecture

The generator uses an AST-based pipeline for code generation:

1. **Schema Parser**: Parses JSON Schema into an AST
2. **Analyzer**: Resolves references and builds an intermediate representation (IR)
3. **AST Backend**: Generates language-native AST from IR
4. **Serializer**: Converts AST to source code
5. **Formatter** (optional): Post-processing with ruff for Python
6. **Merger** (optional): Merges with existing files to preserve custom code

## Development

### Running Tests

```bash
# Install with development dependencies (pytest, ruff, tree-sitter grammars)
pip install -e ".[dev]"

# Run tests
python -m pytest json_schema_to_code/tests/
```

### Project Structure

```
json_schema_to_code/
├── json_schema_to_code/
│   ├── json_schema_to_code.py      # CLI entry point
│   ├── validation_rules.py         # Schema constraint -> validation code
│   ├── validator.py
│   ├── pipeline/
│   │   ├── config.py               # CodeGeneratorConfig / Output / Formatter
│   │   ├── generator.py            # PipelineGenerator - drives the phases
│   │   ├── schema_ast/             # Phase 1: JSON Schema -> Schema AST
│   │   ├── analyzer/               # Phase 2: $ref resolution, naming, IR
│   │   ├── ast_backends/           # Phases 3-4: language AST + serializers
│   │   │   ├── python_ast_backend.py
│   │   │   ├── csharp_ast_backend.py / csharp_serializer.py
│   │   │   ├── swift_ast_backend.py / swift_serializer.py
│   │   │   └── cpp_ast_backend.py / cpp_serializer.py
│   │   ├── formatters/             # Phase 5: ruff
│   │   └── merger/                 # Phase 6: per-language merge + atomic write
│   │       ├── python_merger.py    #   stdlib ast, order-preserving
│   │       ├── tree_sitter_merger.py  # shared base of the two below
│   │       └── csharp_merger.py / swift_merger.py
│   └── tests/
│       ├── test_data/              # Reference cases and functional test JSON
│       └── v3/                     # Pipeline test suites
├── CHANGELOG.md
└── pyproject.toml
```

Packaging metadata lives entirely in `pyproject.toml`; there is no `setup.py`.

## License

MIT License

## Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues for bugs and feature requests.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
