"""
Configuration for the code generator pipeline.

Reuses the same configuration structure as the original codegen.py
for backward compatibility.
"""

from __future__ import annotations

import dataclasses
import warnings
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, get_type_hints


class OutputMode(str, Enum):
    """Output mode for code generation."""

    ERROR_IF_EXISTS = "error_if_exists"  # Error if output file exists
    OVERWRITE = "overwrite"  # Overwrite existing file
    MERGE = "merge"  # Merge with existing file


class ClassDefaultStrategy(str, Enum):
    """Default of a non-required field typed as a generated class (Python).

    SCHEMA: the annotation follows the schema. A nullable field defaults to ``None``;
    a non-nullable one gets ``field(default_factory=lambda: X())`` — if ``X`` cannot
    be built without arguments, constructing the parent without that field raises,
    which is what the schema says. CONSTRUCTIBLE: a non-nullable field whose class
    cannot be built empty is widened to ``X | None = None`` so the parent stays
    constructible. Enums always default to ``None`` (there is no empty enum value).
    """

    SCHEMA = "schema"
    CONSTRUCTIBLE = "constructible"


class MergeStrategy(str, Enum):
    """Strategy for handling existing value members not present in generated code."""

    ERROR = "error"  # Raise error (default)
    MERGE = "merge"  # Keep extra members from existing file
    DELETE = "delete"  # Remove extra members from existing file


@dataclass
class FormatterConfig:
    """Configuration for code formatters."""

    enabled: bool = True
    line_length: int = 100
    target_version: str = ""  # Python target version (e.g., "py313")
    string_normalization: bool = True  # Normalize strings to double quotes
    magic_trailing_comma: bool = True  # Add trailing comma to multi-line structures
    # Run ruff's isort rules over the output. The backend already sorts imports, but only
    # ruff knows the *consuming* project's first-party modules, so only ruff can insert the
    # group separators isort expects. Without this, a project whose own lint sorts imports
    # rewrites every generated file on the next commit.
    sort_imports: bool = True


@dataclass
class OutputConfig:
    """Configuration for output handling."""

    mode: OutputMode = OutputMode.MERGE
    merge_strategy: MergeStrategy = MergeStrategy.ERROR
    output_path: str = ""
    validate_before_write: bool = True  # Validate generated code before writing


@dataclass
class CodeGeneratorConfig:
    """Configuration options for code generation."""

    # Classes to ignore during generation
    ignore_classes: list[str] = field(default_factory=list)

    # Fields to ignore globally across all classes
    global_ignore_fields: list[str] = field(default_factory=list)

    # Order in which to generate classes (empty = definition order)
    order_classes: list[str] = field(default_factory=list)

    # Whether to ignore subclass overrides
    ignoreSubClassOverrides: bool = False

    # Whether to drop minItems/maxItems validation
    drop_min_max_items: bool = False

    # Use array of super type for variable length tuples
    use_array_of_super_type_for_variable_length_tuple: bool = True

    # Whether to use tuple types
    use_tuples: bool = True

    # Use inline union syntax instead of type aliases
    use_inline_unions: bool = False

    # Add generation comment at top of file
    add_generation_comment: bool = True

    # Protocol conformances for generated Swift types (structs and enums)
    swift_conformances: list[str] = field(default_factory=lambda: ["Codable"])

    # Prefix generated Swift types with `nonisolated` (strict-concurrency projects)
    swift_nonisolated: bool = False

    # Swift type for schema `object` / untyped values (a JSON value type the consuming
    # module provides once, e.g. "AnyCodable" or "JSONValue").
    swift_any_type: str = "AnyCodable"

    # Decode every struct through `decoder.policyContainer(keyedBy:)`, a container the
    # consuming module provides, so the module chooses at run time whether a schema
    # mismatch throws (strict) or falls back (lenient). Discriminated enums gain an
    # `unknown(type:payload:)` case. See the Swift section of CLAUDE.md for the API.
    swift_policy_decoding: bool = False

    # Types to quote for Python (forward references)
    quoted_types_for_python: list[str] = field(default_factory=list)

    # Use from __future__ import annotations
    use_future_annotations: bool = True

    # Exclude default values from JSON serialization
    exclude_default_value_from_json: bool = False

    # How a non-required class-typed field gets its default (see ClassDefaultStrategy).
    class_default_strategy: ClassDefaultStrategy = ClassDefaultStrategy.SCHEMA

    # When exclude_default_value_from_json is True, use a helper function instead of inline lambdas.
    # None  → verbose inline form (current behaviour)
    # ""    → emit the helper function definition inline in the generated file
    # "a.b" → import the helper from that module: from a.b import optional_field_in_json
    optional_field_helper_module: str | None = None

    # Add runtime validation code
    add_validation: bool = False

    # External reference import configuration for Python
    external_ref_base_module: str = ""
    external_ref_schema_to_module: dict[str, str] = field(default_factory=dict)

    # C# specific configuration
    csharp_namespace: str = ""
    csharp_additional_usings: list[str] = field(default_factory=list)

    # Base path for resolving external schema $refs
    # When set, the resolver will automatically load external schemas from disk
    # The $ref path is resolved relative to this base path
    # e.g., if base_path="/path/to/schemas" and $ref="/activities/quiz_schema#/$defs/Quiz"
    # it will load "/path/to/schemas/activities/quiz_schema.jinja.json"
    schema_base_path: str = ""

    # Output configuration
    output: OutputConfig = field(default_factory=OutputConfig)

    # Formatter configuration
    formatter: FormatterConfig = field(default_factory=FormatterConfig)

    @staticmethod
    def from_dict(d: dict) -> CodeGeneratorConfig:
        """Build a config from a JSON-loaded dict (see `_config_from_dict`)."""
        return _config_from_dict(CodeGeneratorConfig, d)

    def to_dict(self) -> dict:
        """The JSON-ready form of this config; `from_dict(to_dict(c))` is `c`."""
        return _config_to_dict(self)


def _config_from_dict(cls: type, values: dict) -> Any:
    """Populate a fresh `cls` from `values`, driven by its dataclass fields.

    Enum fields coerce (`"merge"` -> OutputMode.MERGE), dataclass fields recurse (so a
    partial `formatter` block overrides only the keys it names), and unknown keys are
    warned about and skipped -- a config file may carry keys meant for other tools.
    """
    config = cls()
    types = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    for key, value in values.items():
        if key not in known:
            warnings.warn(f"{cls.__name__}: unknown config key {key!r} ignored", stacklevel=3)
            continue
        field_type = types[key]
        if isinstance(field_type, type) and issubclass(field_type, Enum):
            value = field_type(value)
        elif dataclasses.is_dataclass(field_type) and isinstance(value, dict):
            value = _config_from_dict(field_type, value)
        setattr(config, key, value)
    return config


def _config_to_dict(config: Any) -> dict:
    """The inverse of `_config_from_dict`: enums as their values, nested configs as dicts."""
    out: dict[str, Any] = {}
    for f in fields(config):
        value = getattr(config, f.name)
        if isinstance(value, Enum):
            value = value.value
        elif dataclasses.is_dataclass(value):
            value = _config_to_dict(value)
        out[f.name] = value
    return out
