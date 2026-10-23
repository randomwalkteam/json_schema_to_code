"""
Schema analyzer that transforms AST to IR.

Phase 2 of the pipeline: Resolve references, handle inheritance,
and build the IR ready for code generation.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import CodeGeneratorConfig
from ..schema_ast.nodes import (
    AllOfNode,
    ArrayNode,
    ConstNode,
    DefinitionNode,
    EnumNode,
    ObjectNode,
    PrimitiveNode,
    PropertyDef,
    RefNode,
    SchemaAST,
    SchemaNode,
    UnionNode,
)
from .ir_nodes import (
    IR,
    ClassDef,
    EnumDef,
    FieldDef,
    ImportDef,
    TypeAlias,
    TypeKind,
    TypeRef,
)
from .name_resolver import NameMapping, NameResolver
from .reference_resolver import ReferenceResolver, ResolvedRef

# Import validation generator from existing code
try:
    from ...validator import ValidationGenerator
except ImportError:
    ValidationGenerator = None


# Languages without class inheritance in their generated model: allOf bases are flattened
# into each struct and discriminated bases become sum types (Swift enum, C++ std::variant).
FLATTENING_LANGUAGES = ("swift", "cpp")


class SchemaAnalyzer:
    """Analyzes schema AST and builds IR."""

    def __init__(self, language: str, config: CodeGeneratorConfig):
        """
        Initialize the analyzer.

        Args:
            language: Target language ("python" or "cs")
            config: Code generation configuration
        """
        self.language = language
        self.config = config
        self.name_resolver = NameResolver(language)

        # Initialize validation generator if enabled
        self.validator = None
        if config.add_validation and ValidationGenerator:
            self.validator = ValidationGenerator(language)
        self.needs_re_import = False

        # Will be set during analysis
        self.ast: SchemaAST | None = None
        self.name_mapping: NameMapping | None = None
        self.ref_resolver: ReferenceResolver | None = None

        # Bases that are genuine discriminated unions / known-subtypes bases (not plain allOf
        # inheritance). Used by the Swift backend to choose a discriminated enum over a struct.
        self.polymorphic_bases: set[str] = set()

        # Track subclass relationships
        # base -> [(name, discriminator), ...]
        self.subclasses: dict[str, list[tuple[str, str]]] = {}
        self.base_class: dict[str, str] = {}  # class -> base
        # base -> discriminator JSON property name (e.g. "type", "action_type")
        self.discriminator_property_by_base: dict[str, str] = {}

        # Track type aliases needed
        self.type_aliases: dict[str, TypeAlias] = {}

        # Track required imports
        self.required_imports: set[str] = set()
        self.python_imports: set[tuple[str, str]] = set()

    def analyze(self, ast: SchemaAST) -> IR:
        """
        Analyze the AST and build IR.

        Args:
            ast: The parsed schema AST

        Returns:
            IR ready for code generation
        """
        self.ast = ast
        self.name_mapping = self.name_resolver.resolve_names(ast)
        self.ref_resolver = ReferenceResolver(ast, self.name_mapping.definition_names, self.config.schema_base_path)

        ir = IR(root_name=ast.root_name)
        ir.name_mapping = self.name_mapping.definition_names.copy()

        # First pass: identify inheritance relationships
        self._build_inheritance_graph()

        # Second pass: build class definitions
        classes_to_generate = self._collect_classes_to_generate()
        for def_node in classes_to_generate:
            class_def = self._analyze_definition(def_node)
            if class_def:
                self._mark_polymorphic_base(class_def)
                ir.classes.append(class_def)

        # Add inline classes
        inline_classes = self._collect_inline_classes()
        for class_def in inline_classes:
            self._mark_polymorphic_base(class_def)
            ir.classes.append(class_def)

        # Add root class if needed
        if ast.root_node:
            root_class = self._analyze_root_node()
            if root_class:
                self._mark_polymorphic_base(root_class)
                ir.classes.insert(0, root_class)

        # Add type aliases
        ir.type_aliases = list(self.type_aliases.values())

        # Sort type aliases by forward reference
        simple_aliases = [a for a in ir.type_aliases if not a.has_forward_refs]
        forward_aliases = [a for a in ir.type_aliases if a.has_forward_refs]
        ir.type_aliases = simple_aliases  # Simple ones go in prefix

        # Forward reference aliases get added after classes
        for alias in forward_aliases:
            ir.type_aliases.append(alias)

        # Build imports
        ir.imports = self._build_imports()

        return ir

    def _mark_polymorphic_base(self, class_def: ClassDef) -> None:
        """Flag genuine discriminated-union / known-subtypes bases (Swift emits an enum for these)."""
        if class_def.name in self.polymorphic_bases:
            class_def.is_polymorphic_base = True

    def _build_inheritance_graph(self) -> None:
        """Build the inheritance graph from allOf and anyOf/oneOf relationships."""
        for def_node in self.ast.definitions:
            # Handle allOf (explicit inheritance)
            if isinstance(def_node.body, AllOfNode):
                self._process_allof_inheritance(def_node)

            # Handle anyOf/oneOf discriminated unions (C# and Swift)
            # For anyOf/oneOf with all $refs, the union type becomes a polymorphic base
            # (a base class in C#, a discriminated enum in Swift).
            if self.language in ("cs", *FLATTENING_LANGUAGES) and isinstance(def_node.body, UnionNode):
                self._process_union_discriminated_type(def_node)

    def _process_allof_inheritance(self, def_node: DefinitionNode) -> None:
        """Process allOf inheritance relationship."""
        allof = def_node.body
        if not isinstance(allof, AllOfNode) or not allof.base_ref:
            return

        # Resolve the base class
        resolved = self.ref_resolver.resolve(allof.base_ref)
        base_class_name = resolved.target_name

        # Get this class's name
        class_name = self.name_mapping.definition_names.get(def_node.original_name, def_node.original_name)

        # Store discriminator property name for the base (from base definition's raw schema)
        defs = self.ast.raw_schema.get("$defs") or self.ast.raw_schema.get("definitions") or {}
        base_original_name = allof.base_ref.ref_path.split("/")[-1]
        base_raw_def = defs.get(base_original_name, {})
        disc_prop = base_raw_def.get("discriminator", {}).get("propertyName", "type")
        self.discriminator_property_by_base[base_class_name] = disc_prop
        # Only an explicit "discriminator" on the base makes this a polymorphic union;
        # plain allOf inheritance (no discriminator) keeps the base as a normal struct in Swift.
        if isinstance(base_raw_def, dict) and "discriminator" in base_raw_def:
            self.polymorphic_bases.add(base_class_name)

        # Find discriminator value from const in extension (property name may be "type" or e.g. "action_type")
        discriminator = class_name
        if allof.extension:
            for prop in allof.extension.properties:
                if prop.name == disc_prop and isinstance(prop.type_node, ConstNode):
                    discriminator = prop.type_node.value
                    break

        # Register the relationship
        if base_class_name not in self.subclasses:
            self.subclasses[base_class_name] = []

        if class_name not in self.config.ignore_classes:
            self.subclasses[base_class_name].append((class_name, discriminator))

        self.base_class[class_name] = base_class_name

    def _process_union_discriminated_type(self, def_node: DefinitionNode) -> None:
        """Process anyOf/oneOf as discriminated union for C#."""
        union = def_node.body
        if not isinstance(union, UnionNode):
            return

        # Check if all variants are $refs
        all_refs = all(isinstance(v, RefNode) for v in union.variants)
        if not all_refs or len(union.variants) < 2:
            return

        # Check if all referenced definitions actually exist in this schema
        # If they don't, this isn't a discriminated union we can generate
        all_exist = True
        for variant in union.variants:
            if isinstance(variant, RefNode):
                ref_name = variant.ref_path.split("/")[-1]
                subtype_def = self.ref_resolver.get_definition(ref_name)
                if not subtype_def:
                    all_exist = False
                    break

        if not all_exist:
            return

        # This is a discriminated union - the base type name
        base_class_name = self.name_mapping.definition_names.get(def_node.original_name, def_node.original_name)

        # Store discriminator property name from this definition's raw schema
        defs = self.ast.raw_schema.get("$defs") or self.ast.raw_schema.get("definitions") or {}
        disc_prop = defs.get(def_node.original_name, {}).get("discriminator", {}).get("propertyName", "type")
        self.discriminator_property_by_base[base_class_name] = disc_prop
        # A oneOf/anyOf of $refs is always a genuine discriminated union.
        self.polymorphic_bases.add(base_class_name)

        if base_class_name not in self.subclasses:
            self.subclasses[base_class_name] = []

        # Each $ref is a subtype
        for variant in union.variants:
            if not isinstance(variant, RefNode):
                continue

            resolved = self.ref_resolver.resolve(variant)
            subtype_name = resolved.target_name

            # Find discriminator value from subtype's const property (disc_prop, e.g. "type" or "action_type")
            discriminator = subtype_name
            subtype_def = self.ref_resolver.get_definition(variant.ref_path.split("/")[-1])
            if subtype_def and isinstance(subtype_def.body, ObjectNode):
                for prop in subtype_def.body.properties:
                    if prop.name == disc_prop and isinstance(prop.type_node, ConstNode):
                        discriminator = prop.type_node.value
                        break

            if subtype_name not in self.config.ignore_classes:
                self.subclasses[base_class_name].append((subtype_name, discriminator))
                self.base_class[subtype_name] = base_class_name

    def _collect_classes_to_generate(self) -> list[DefinitionNode]:
        """Collect definitions in the order they should be generated."""
        # Use config order if specified
        ordered = []
        remaining = {d.original_name: d for d in self.ast.definitions}

        # First, add in specified order
        for name in self.config.order_classes:
            if name in remaining:
                ordered.append(remaining.pop(name))

        # Then add remaining in definition order
        for def_node in self.ast.definitions:
            if def_node.original_name in remaining:
                ordered.append(remaining.pop(def_node.original_name))

        return ordered

    def _analyze_definition(self, def_node: DefinitionNode) -> ClassDef | None:
        """Analyze a single definition and create ClassDef."""
        class_name = self.name_mapping.definition_names.get(def_node.original_name, def_node.original_name)

        if class_name in self.config.ignore_classes:
            return None

        body = def_node.body

        # Handle enum types
        if isinstance(body, EnumNode):
            return self._analyze_enum_definition(def_node, body, class_name)

        # Handle union types (oneOf/anyOf with all $refs)
        if isinstance(body, UnionNode):
            return self._analyze_union_definition(def_node, body, class_name)

        # Handle allOf (inheritance)
        if isinstance(body, AllOfNode):
            return self._analyze_allof_definition(def_node, body, class_name)

        # Handle object types
        if isinstance(body, ObjectNode):
            return self._analyze_object_definition(def_node, body, class_name)

        # Handle primitive base types
        if isinstance(body, PrimitiveNode):
            return self._analyze_primitive_definition(def_node, body, class_name)

        return None

    def _analyze_enum_definition(self, def_node: DefinitionNode, enum_node: EnumNode, class_name: str) -> ClassDef | None:
        """Analyze an enum definition."""
        # Build enum member names
        members = {}
        for value in enum_node.values:
            if value in enum_node.member_names:
                member_name = enum_node.member_names[value]
            else:
                # Auto-generate member name
                member_name = self._to_enum_member_name(str(value))
            members[member_name] = value

        enum_def = EnumDef(
            name=class_name,
            original_name=def_node.original_name,
            value_type=enum_node.inferred_type,
            members=members,
        )

        # For C#, string enums become enum classes
        # For Python, they become Enum subclasses
        class_def = ClassDef(
            name=class_name,
            original_name=def_node.original_name,
            is_enum=True,
            enum_def=enum_def,
        )

        return class_def

    def _analyze_union_definition(self, def_node: DefinitionNode, union_node: UnionNode, class_name: str) -> ClassDef | None:
        """Analyze a union type definition (oneOf/anyOf)."""
        # Check if all variants are $refs (discriminated union pattern)
        all_refs = all(isinstance(v, RefNode) for v in union_node.variants)

        if all_refs and len(union_node.variants) > 1:
            # This is a type alias (union of classes)
            union_types = []
            for variant in union_node.variants:
                if isinstance(variant, RefNode):
                    resolved = self.ref_resolver.resolve(variant)
                    union_types.append(resolved.target_name)

            # Create type alias
            alias = TypeAlias(
                name=class_name,
                union_components=sorted(union_types),
            )
            self.type_aliases[class_name] = alias

            # For C#/Swift, we still generate a polymorphic base if discriminated
            if self.language in ("cs", *FLATTENING_LANGUAGES) and class_name in self.subclasses:
                disc_prop = self.discriminator_property_by_base.get(class_name, "type")
                return ClassDef(
                    name=class_name,
                    original_name=def_node.original_name,
                    subclasses=self.subclasses.get(class_name, []),
                    discriminator_property=disc_prop,
                    is_polymorphic_base=True,
                )

            return None

        # Not all refs - treat as union type field
        return None

    def _analyze_allof_definition(self, def_node: DefinitionNode, allof: AllOfNode, class_name: str) -> ClassDef:
        """Analyze an allOf (inheritance) definition."""
        class_def = ClassDef(
            name=class_name,
            original_name=def_node.original_name,
        )

        # Set base class
        if allof.base_ref:
            resolved = self.ref_resolver.resolve(allof.base_ref)
            class_def.base_class = resolved.target_name

            # Register external import for base class (Python only)
            if resolved.is_external and self.language == "python":
                self._register_external_import(resolved)

            # Get base class properties to pass to constructor
            base_def = self.ref_resolver.get_definition(allof.base_ref.ref_path.split("/")[-1])
            if base_def and isinstance(base_def.body, ObjectNode):
                class_def.base_fields = self._analyze_base_properties(base_def.body, allof.extension, class_name)
            elif base_def and isinstance(base_def.body, AllOfNode):
                class_def.base_fields = self._analyze_allof_base_properties(base_def.body, allof.extension, class_name)
            elif resolved.is_external:
                ext_def = self.ref_resolver.load_external_definition(resolved.external_path, resolved.class_name_in_external)
                if ext_def:
                    class_def.base_fields = self._analyze_external_base_properties(ext_def, allof.extension, class_name, resolved.external_path)

        # Resolve extra base classes (for multiple inheritance in Python)
        for extra_ref in allof.extra_refs:
            extra_resolved = self.ref_resolver.resolve(extra_ref)
            class_def.extra_base_classes.append(extra_resolved.target_name)
            if extra_resolved.is_external and self.language == "python":
                self._register_external_import(extra_resolved)
            # Swift has no multiple inheritance: mixin ($ref beyond the first) properties
            # are flattened into the struct. _analyze_properties (not the base-properties
            # path) so schema defaults survive into the generated decode.
            if self.language in FLATTENING_LANGUAGES:
                extra_def = self.ref_resolver.get_definition(extra_ref.ref_path.split("/")[-1])
                if extra_def and isinstance(extra_def.body, ObjectNode):
                    class_def.base_fields += self._analyze_properties(extra_def.body, class_name)
                elif extra_resolved.is_external:
                    ext_def = self.ref_resolver.load_external_definition(extra_resolved.external_path, extra_resolved.class_name_in_external)
                    if ext_def:
                        class_def.base_fields += self._analyze_external_base_properties(ext_def, allof.extension, class_name, extra_resolved.external_path)

        # Add subclasses if this is a base class
        class_def.subclasses = self.subclasses.get(class_name, [])

        # If this class extends a discriminated base, inherit discriminator property name
        # so the C# backend can emit a get-only property instead of const for serialization
        if class_def.base_class:
            class_def.discriminator_property = self.discriminator_property_by_base.get(class_def.base_class)

        # Analyze extension properties. A property the base requires stays required
        # when the subclass redeclares it to narrow its type.
        if allof.extension:
            inherited_required = frozenset(f.name for f in class_def.base_fields if f.is_required)
            base_fields_by_name = {f.name: f for f in class_def.base_fields}
            class_def.fields = self._analyze_properties(allof.extension, class_name, inherited_required, base_fields_by_name)

        # When ignoreSubClassOverrides is set, remove from fields any field that
        # is already covered by a base_field, UNLESS it is a const override of a
        # non-const base field (e.g. the discriminator "type" const).
        if self.config.ignoreSubClassOverrides and class_def.base_fields:
            base_field_names = {f.name for f in class_def.base_fields}
            filtered = []
            for f in class_def.fields:
                if f.name in base_field_names:
                    # Keep const overrides (e.g. type = "DHMFoo") so the subclass
                    # still emits a get-only discriminator property.
                    # Drop non-const overrides (e.g. id with tighter constraints)
                    # because the base class already declares that property.
                    if f.is_const:
                        filtered.append(f)
                    # else: skip – base class handles it
                else:
                    filtered.append(f)
            class_def.fields = filtered

        # Build constructor fields (non-const fields)
        class_def.constructor_fields = [f for f in class_def.fields if not f.is_const]

        return class_def

    def _analyze_base_properties(
        self,
        base_obj: ObjectNode,
        extension: ObjectNode | None,
        class_name: str,
    ) -> list[FieldDef]:
        """Analyze base class properties for constructor passing."""
        base_fields = []

        for base_prop in base_obj.properties:
            # Check if base property is already a const
            is_base_const = isinstance(base_prop.type_node, ConstNode)

            # Check if this property is overridden with a const in extension
            is_overridden_const = False
            override_value = None
            if extension:
                for ext_prop in extension.properties:
                    if ext_prop.name == base_prop.name:
                        if isinstance(ext_prop.type_node, ConstNode):
                            is_overridden_const = True
                            override_value = ext_prop.type_node.value
                        break

            field_def = FieldDef(
                name=base_prop.name,
                original_name=base_prop.name,
                is_required=base_prop.is_required,
            )
            # Swift flattens base fields into the struct, so schema defaults must
            # survive for the decode to stay lenient (decodeIfPresent ?? default).
            # C#/Python keep base fields as constructor params, where a default
            # would change signatures — leave them untouched.
            if self.language in FLATTENING_LANGUAGES and base_prop.has_default and base_prop.default_value is not None:
                field_def.has_default = True
                field_def.default_value = base_prop.default_value

            # Set type_ref for C# constructor parameter types
            if base_prop.type_node:
                field_def.type_ref = self._analyze_type(base_prop.type_node, base_prop.name, class_name, base_prop.is_required)
                self._collect_language_defaults(field_def, base_prop.type_node.metadata, class_name)

            # Mark as const based on override or base status
            # is_const = True means exclude from constructor params
            # For base const: pass variable name in base call
            # For overridden const: pass literal value in base call
            if is_overridden_const:
                field_def.default_value = override_value
                field_def.has_default = True
                field_def.is_const = True
                field_def.is_overridden_const = True  # Flag for using literal
            elif is_base_const:
                # Base class const - exclude from constructor params
                # but pass variable name in base call
                field_def.is_const = True
                field_def.is_overridden_const = False  # Flag for using variable name

            base_fields.append(field_def)

        return base_fields

    def _analyze_allof_base_properties(
        self,
        allof_node: AllOfNode,
        extension: ObjectNode | None,
        class_name: str,
    ) -> list[FieldDef]:
        """Analyze base class properties when the base itself uses allOf (inheritance).

        Recursively collects properties from the allOf chain:
        the extension ObjectNode's properties plus any ancestor properties
        reached via the allOf's own base_ref.

        When the immediate parent constifies a field that already exists in the
        ancestor chain (typical discriminator pattern: ``WidgetImage.type`` →
        ``ImageByURL.type = const "ImageByURL"`` → ``ImageBySearch.type = const
        "ImageBySearch"``), the parent's own constructor handles the const
        internally and does not expose it as a parameter. From a grandchild's
        perspective the field must therefore be skipped in its ``base(...)``
        call. We model this by replacing the ancestor's view of the field with
        the parent's view and flagging ``is_skipped_in_base_call=True`` so the
        constructor generator skips it; ``is_overridden_const`` is preserved
        so the override-getter detection and emission paths still see that
        the grandchild is overriding a const discriminator.
        """
        base_fields: list[FieldDef] = []

        if allof_node.base_ref:
            ancestor_def = self.ref_resolver.get_definition(allof_node.base_ref.ref_path.split("/")[-1])
            if ancestor_def and isinstance(ancestor_def.body, ObjectNode):
                base_fields.extend(self._analyze_base_properties(ancestor_def.body, extension, class_name))
            elif ancestor_def and isinstance(ancestor_def.body, AllOfNode):
                base_fields.extend(self._analyze_allof_base_properties(ancestor_def.body, extension, class_name))

        if allof_node.extension:
            ancestors_by_name = {f.name: f for f in base_fields}
            parent_fields = self._analyze_base_properties(allof_node.extension, extension, class_name)
            for pf in parent_fields:
                ancestor = ancestors_by_name.get(pf.name)
                if ancestor is not None:
                    # Drop the ancestor's view; the parent's view supersedes it —
                    # except for a constructor default the parent did not restate,
                    # which keeps travelling down the chain.
                    self._inherit_language_defaults(pf, ancestor)
                    base_fields = [f for f in base_fields if f.name != pf.name]
                    if pf.is_const:
                        # Parent constified at its level → grandchild's base()
                        # call must skip it (the parent's constructor doesn't
                        # take it as a parameter). Keep is_overridden_const so
                        # the override-getter emission still detects the
                        # virtual discriminator on the base.
                        pf.is_skipped_in_base_call = True
                base_fields.append(pf)

        return base_fields

    def _collect_external_properties(
        self,
        external_def: dict,
        schema_defs: dict,
    ) -> tuple[dict[str, dict], set[str]]:
        """Recursively collect all properties from an external schema definition.

        Handles allOf chains by resolving local $ref within the same external
        schema and merging properties from the entire inheritance hierarchy.

        Returns (properties, required) where properties maps name -> schema dict.
        """
        properties: dict[str, dict] = {}
        required: set[str] = set()

        if "allOf" in external_def:
            for item in external_def["allOf"]:
                if "$ref" in item:
                    ref_path = item["$ref"]
                    if ref_path.startswith("#/$defs/") or ref_path.startswith("#/definitions/"):
                        ref_name = ref_path.split("/")[-1]
                        ref_def = schema_defs.get(ref_name)
                        ref_defs = schema_defs
                    else:
                        # The base lives in yet another schema file: follow it so
                        # `required` (and properties) travel the whole chain.
                        ext_path, ext_name = self.ref_resolver.split_external_ref(ref_path)
                        ref_def = self.ref_resolver.load_external_definition(ext_path, ext_name)
                        ref_defs = self.ref_resolver.load_external_schema_defs(ext_path)
                    if ref_def:
                        parent_props, parent_req = self._collect_external_properties(ref_def, ref_defs)
                        self._merge_redeclared_properties(properties, parent_props)
                        required.update(parent_req)
                else:
                    sub_props, sub_req = self._collect_external_properties(item, schema_defs)
                    self._merge_redeclared_properties(properties, sub_props)
                    required.update(sub_req)

        self._merge_redeclared_properties(properties, external_def.get("properties", {}))
        required.update(external_def.get("required", []))

        return properties, required

    @classmethod
    def _merge_redeclared_properties(cls, properties: dict[str, dict], incoming: dict[str, dict]) -> None:
        """`properties.update(incoming)`, except that a redeclaration keeps the constructor
        defaults it does not restate.

        A later allOf member narrowing a property's type supersedes the earlier
        declaration, but an `x-<lang>-default` / `-default-code` / `-imports` the
        redeclaration says nothing about still describes how to fill the field (see
        `_inherit_language_defaults` for the in-file counterpart).
        """
        for prop_name, prop_schema in incoming.items():
            inherited = properties.get(prop_name)
            if inherited is not None and isinstance(prop_schema, dict):
                prop_schema = cls._with_inherited_language_default_keys(prop_schema, inherited)
            properties[prop_name] = prop_schema

    @classmethod
    def _with_inherited_language_default_keys(cls, prop_schema: dict, inherited: dict) -> dict:
        """`prop_schema` plus the `x-<lang>-default` / `-default-code` / `-imports` keys of
        `inherited` for every language `prop_schema` declares no default of its own for."""
        if not isinstance(inherited, dict):
            return prop_schema

        def declared_languages(schema: dict) -> set[str]:
            languages = set()
            for key in schema:
                match = cls._LANGUAGE_DEFAULT_KEY.match(key)
                if match and match.group(2) in ("default", "default-code"):
                    languages.add(match.group(1))
            return languages

        own = declared_languages(prop_schema)
        carried = {key: value for key, value in inherited.items() if (match := cls._LANGUAGE_DEFAULT_KEY.match(key)) and match.group(1) not in own}
        if not carried:
            return prop_schema
        return {**carried, **prop_schema}

    def _analyze_external_base_properties(
        self,
        external_def: dict,
        extension: ObjectNode | None,
        class_name: str,
        schema_path: str,
    ) -> list[FieldDef]:
        """Analyze base class properties from external schema definition (raw dict)."""
        base_fields = []

        schema_defs = self.ref_resolver.load_external_schema_defs(schema_path)
        properties, required = self._collect_external_properties(external_def, schema_defs)

        for prop_name, prop_schema in properties.items():
            # Check if this property is overridden with a const in extension
            is_overridden_const = False
            override_value = None
            if extension:
                for ext_prop in extension.properties:
                    if ext_prop.name == prop_name:
                        if isinstance(ext_prop.type_node, ConstNode):
                            is_overridden_const = True
                            override_value = ext_prop.type_node.value
                        break

            # Check if base property is a const
            is_base_const = "const" in prop_schema

            field_def = FieldDef(
                name=prop_name,
                original_name=prop_name,
                is_required=prop_name in required,
            )
            # Same as _analyze_base_properties: Swift needs the schema default so
            # the flattened field decodes leniently. `default: null` is treated as
            # no-default — the field then falls back to optional decoding.
            if self.language in FLATTENING_LANGUAGES and prop_schema.get("default") is not None:
                field_def.has_default = True
                field_def.default_value = prop_schema["default"]

            # Set type_ref based on JSON schema type
            field_def.type_ref = self._type_ref_from_json_schema(prop_schema, prop_name in required)
            self._collect_language_defaults(field_def, prop_schema, class_name)

            # Mark as const based on override or base status
            if is_overridden_const:
                field_def.default_value = override_value
                field_def.has_default = True
                field_def.is_const = True
                field_def.is_overridden_const = True
            elif is_base_const:
                field_def.is_const = True
                field_def.is_overridden_const = False

            base_fields.append(field_def)

        return base_fields

    def _type_ref_from_json_schema(self, prop_schema: dict, is_required: bool) -> TypeRef:
        """Create a TypeRef from a raw JSON schema property definition."""
        type_ref = self._type_ref_from_json_schema_inner(prop_schema, is_required)
        type_ref.type_overrides = self._type_overrides(prop_schema)
        return type_ref

    def _type_ref_from_json_schema_inner(self, prop_schema: dict, is_required: bool) -> TypeRef:
        if "$ref" in prop_schema and "type" not in prop_schema:
            return TypeRef(kind=TypeKind.ANY)

        if ("anyOf" in prop_schema or "oneOf" in prop_schema) and "type" not in prop_schema:
            return TypeRef(kind=TypeKind.ANY)

        schema_type = prop_schema.get("type")
        if schema_type is None:
            return TypeRef(kind=TypeKind.ANY)

        if isinstance(schema_type, list):
            non_null = [t for t in schema_type if t != "null"]
            if not non_null:
                return TypeRef(kind=TypeKind.ANY)
            result = self._type_ref_from_json_schema({**prop_schema, "type": non_null[0]}, is_required)
            if "null" in schema_type:
                result.is_nullable = True
            return result

        primitive_mapping = {
            "string": "string",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
        }
        if schema_type in primitive_mapping:
            return TypeRef(kind=TypeKind.PRIMITIVE, name=primitive_mapping[schema_type])

        if schema_type == "array":
            items_schema = prop_schema.get("items")
            if items_schema:
                item_type = self._type_ref_from_json_schema(items_schema, True)
            else:
                item_type = TypeRef(kind=TypeKind.ANY)
            return TypeRef(kind=TypeKind.ARRAY, type_args=[item_type])

        if schema_type == "object":
            additional = prop_schema.get("additionalProperties")
            key_type = TypeRef(kind=TypeKind.PRIMITIVE, name="string")
            if additional and isinstance(additional, dict):
                value_type = self._type_ref_from_json_schema(additional, True)
            else:
                value_type = TypeRef(kind=TypeKind.ANY)
            return TypeRef(kind=TypeKind.DICT, type_args=[key_type, value_type])

        return TypeRef(kind=TypeKind.ANY)

    def _analyze_object_definition(self, def_node: DefinitionNode, obj: ObjectNode, class_name: str) -> ClassDef:
        """Analyze an object type definition."""
        subclasses = self.subclasses.get(class_name, [])
        disc_prop = self.discriminator_property_by_base.get(class_name, "type") if subclasses else None
        # If no subclasses in same schema, still respect discriminator on this definition (polymorphic base in another file)
        if disc_prop is None and self.language in ("cs", *FLATTENING_LANGUAGES):
            defs = self.ast.raw_schema.get("$defs") or self.ast.raw_schema.get("definitions") or {}
            raw_def = defs.get(def_node.original_name, {})
            if isinstance(raw_def, dict) and "discriminator" in raw_def:
                disc_prop = raw_def.get("discriminator", {}).get("propertyName", "type")
        # Cross-schema known subtypes declared via x-csharp-known-subtypes
        # (Swift / C++ read x-swift- / x-cpp-known-subtypes first, falling back to the C# key).
        external_subtypes = []
        subtype_usings = []
        if self.language in ("cs", *FLATTENING_LANGUAGES):
            defs = defs if "defs" in dir() else (self.ast.raw_schema.get("$defs") or self.ast.raw_schema.get("definitions") or {})
            raw_def = defs.get(def_node.original_name, {}) if isinstance(defs, dict) else {}
            known = None
            if isinstance(raw_def, dict):
                if self.language in FLATTENING_LANGUAGES:
                    known = raw_def.get(f"x-{self.language}-known-subtypes")
                if not known:
                    known = raw_def.get("x-csharp-known-subtypes", [])
            for entry in known or []:
                external_subtypes.append((entry["class"], entry["value"]))
                # C# needs per-type 'using' namespaces; Swift has a single module, so ignore.
                if self.language == "cs" and "using" in entry:
                    subtype_usings.append(entry["using"])
            # Known-subtypes or an explicit discriminator make this object a polymorphic base.
            raw_def_for_poly = defs.get(def_node.original_name, {}) if isinstance(defs, dict) else {}
            if external_subtypes or (isinstance(raw_def_for_poly, dict) and "discriminator" in raw_def_for_poly):
                self.polymorphic_bases.add(class_name)

        class_def = ClassDef(
            name=class_name,
            original_name=def_node.original_name,
            subclasses=subclasses + external_subtypes,
            discriminator_property=disc_prop,
            subtype_usings=subtype_usings,
        )

        # Check if this class is a subtype (from discriminated union or allOf)
        if class_name in self.base_class:
            class_def.base_class = self.base_class[class_name]
            # Inherit discriminator property name so C# emits get-only property for serialization
            base_name = class_def.base_class
            if base_name in self.discriminator_property_by_base:
                class_def.discriminator_property = self.discriminator_property_by_base[base_name]

        # C# interface implementation
        if obj.implements:
            class_def.implements = obj.implements
            class_def.interface_properties = obj.interface_properties

        # Analyze properties
        class_def.fields = self._analyze_properties(obj, class_name)

        # Build constructor fields
        class_def.constructor_fields = [f for f in class_def.fields if not f.is_const and f.name not in self.config.global_ignore_fields]

        # Generate validation code if enabled
        if self.validator:
            class_def.validation_code = self._generate_validation_code(obj, class_def)

        return class_def

    def _analyze_primitive_definition(self, def_node: DefinitionNode, prim: PrimitiveNode, class_name: str) -> ClassDef | None:
        """Analyze a primitive type definition (rare case)."""
        # Check if this is a string enum (type: "string" with enum in metadata)
        # For both Python and C#: generate enum class
        if prim.type_name == "string" and "enum" in prim.metadata:
            enum_values = prim.metadata["enum"]
            members = {self._to_enum_member_name(str(v)): v for v in enum_values}

            enum_def = EnumDef(
                name=class_name,
                original_name=def_node.original_name,
                value_type="string",
                members=members,
            )

            return ClassDef(
                name=class_name,
                original_name=def_node.original_name,
                is_enum=True,
                enum_def=enum_def,
            )

        # This is typically used for type aliases to primitives
        return None

    def _analyze_properties(
        self,
        obj: ObjectNode,
        parent_class: str,
        inherited_required: frozenset[str] = frozenset(),
        base_fields_by_name: dict[str, FieldDef] | None = None,
    ) -> list[FieldDef]:
        """Analyze properties and create FieldDefs.

        ``inherited_required`` names the properties a base class already requires:
        a subclass re-declaring one of them (to narrow its type) keeps it required —
        ``required`` is a constraint on the composed object, not on the member that
        happens to redeclare the property.
        """
        fields = []

        for prop in obj.properties:
            if prop.name in self.config.global_ignore_fields:
                continue

            base_field = base_fields_by_name.get(prop.name) if base_fields_by_name else None
            field_def = self._analyze_property(prop, parent_class, prop.name in inherited_required, base_field)
            fields.append(field_def)

        return fields

    def _analyze_property(self, prop: PropertyDef, parent_class: str, required_by_base: bool = False, base_field: FieldDef | None = None) -> FieldDef:
        """Analyze a single property.

        ``base_field`` is the base class's declaration when this property redeclares
        one (an allOf subclass narrowing a type): the base's constructor default
        still describes how to fill the field, so it carries over unless the
        redeclaration sets its own for that language.
        """
        is_required = prop.is_required or required_by_base
        field_def = FieldDef(
            name=prop.name,
            original_name=prop.name,
            is_required=is_required,
            has_default=prop.has_default,
            default_value=prop.default_value,
        )
        if prop.type_node:
            field_def.omit_when_default = bool(prop.type_node.metadata.get("x-omit-when-default", False))
            self._collect_language_defaults(field_def, prop.type_node.metadata, parent_class)
        if base_field is not None:
            self._inherit_language_defaults(field_def, base_field)

        # Escape C# keywords
        if self.language == "cs":
            escaped = self.name_resolver.escape_keyword(prop.name)
            if escaped != prop.name:
                field_def.escaped_name = escaped

        # Analyze type
        if prop.type_node:
            field_def.type_ref = self._analyze_type(
                prop.type_node,
                prop.name,
                parent_class,
                is_required,
            )

            # If field has a non-null default value, it shouldn't be nullable
            # The default provides the value, so no need for null
            if prop.has_default and field_def.type_ref and prop.default_value is not None:
                field_def.type_ref.is_nullable = False
            # Same for a non-null constructor default of this language: the
            # optional-scalar widening (`str | None`) exists only to express
            # absence, and the default already does that.
            lang_key = {"cs": "csharp"}.get(self.language, self.language)
            if field_def.type_ref and (field_def.language_defaults.get(lang_key) is not None or lang_key in field_def.language_default_code):
                field_def.type_ref.is_nullable = False
            elif prop.has_default and field_def.type_ref and prop.default_value is None:
                if not field_def.type_ref.is_nullable:
                    raise ValueError(
                        f"Schema error in {parent_class}.{prop.name}: "
                        f"'default: null' on a non-nullable type ({field_def.type_ref.name}). "
                        f"Either make the type nullable (e.g. oneOf: [$ref, null]) "
                        f"or remove 'default: null' from the schema."
                    )

            # Check for const type
            if isinstance(prop.type_node, ConstNode):
                field_def.is_const = True
                field_def.default_value = prop.type_node.value
                field_def.has_default = True

            # Add comment for standalone enum nodes (enum without type)
            # The original codegen.py only adds comments for pure enum fields,
            # not for fields with "type" + "enum" (those go through the type path)
            if isinstance(prop.type_node, EnumNode):
                values_str = ", ".join(f'"{v}"' for v in prop.type_node.values)
                comment_prefix = "#" if self.language == "python" else "//"
                field_def.comment = f"  {comment_prefix} Allowed values: {values_str}"

        return field_def

    def _analyze_type(
        self,
        node: SchemaNode,
        field_name: str,
        parent_class: str,
        is_required: bool,
    ) -> TypeRef:
        """Analyze a type node and create TypeRef."""
        type_ref = self._analyze_type_node(node, field_name, parent_class, is_required)
        # Every node kind takes its overrides here, once: the schema author's
        # `x-<language>-type` wins over whatever the dispatch inferred. A wrapper that
        # collapsed to its inner node's TypeRef (`oneOf: [T, null]`) keeps what that
        # node declared, the wrapper's own keys taking precedence.
        type_ref.type_overrides = {**type_ref.type_overrides, **self._type_overrides(node.metadata)}
        return type_ref

    _TYPE_OVERRIDE_KEY = re.compile(r"^x-([a-z]+)-type$")

    def _type_overrides(self, metadata: dict) -> dict[str, str]:
        """`x-python-type` / `x-csharp-type` / `x-swift-type` -> {language: type}."""
        overrides: dict[str, str] = {}
        for key, value in metadata.items():
            match = self._TYPE_OVERRIDE_KEY.match(key)
            if match and isinstance(value, str) and value:
                overrides[match.group(1)] = value
        return overrides

    _LANGUAGE_DEFAULT_KEY = re.compile(r"^x-([a-z]+)-(default|default-code|imports)$")

    @staticmethod
    def _inherit_language_defaults(field_def: FieldDef, base_field: FieldDef) -> None:
        """Carry the base declaration's per-language constructor defaults onto a
        redeclaration that sets none of its own for that language."""

        def declared(lang: str) -> bool:
            return lang in field_def.language_defaults or lang in field_def.language_default_code

        for lang, value in base_field.language_defaults.items():
            if not declared(lang):
                field_def.language_defaults[lang] = value
        for lang, code in base_field.language_default_code.items():
            if not declared(lang):
                field_def.language_default_code[lang] = code
        for lang, imports in base_field.language_imports.items():
            field_def.language_imports.setdefault(lang, list(imports))

    def _collect_language_defaults(self, field_def: FieldDef, metadata: dict, parent_class: str) -> None:
        """`x-<language>-default` / `-default-code` / `-imports` -> per-language constructor defaults.

        `required` describes the wire: validation demands the property. Whether a
        constructor needs it is a different question (a server-built placeholder, a
        generated id), and these keys answer it without loosening the schema.
        """
        where = f"{parent_class}.{field_def.name}"
        for key, value in metadata.items():
            match = self._LANGUAGE_DEFAULT_KEY.match(key)
            if not match:
                continue
            language, kind = match.groups()
            if kind == "default":
                field_def.language_defaults[language] = value
            elif kind == "default-code":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Schema error in {where}: {key} must be a non-empty expression string")
                field_def.language_default_code[language] = value
            else:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError(f"Schema error in {where}: {key} must be a list of import statements")
                field_def.language_imports[language] = list(value)
        for language in sorted(field_def.language_defaults.keys() & field_def.language_default_code.keys()):
            raise ValueError(f"Schema error in {where}: x-{language}-default and x-{language}-default-code both given; keep one")

    def _analyze_type_node(
        self,
        node: SchemaNode,
        field_name: str,
        parent_class: str,
        is_required: bool,
    ) -> TypeRef:
        if isinstance(node, RefNode):
            return self._analyze_ref_type(node, is_required)

        if isinstance(node, PrimitiveNode):
            return self._analyze_primitive_type(node, is_required)

        if isinstance(node, ConstNode):
            return self._analyze_const_type(node)

        if isinstance(node, EnumNode):
            return self._analyze_enum_type(node)

        if isinstance(node, ArrayNode):
            return self._analyze_array_type(node, field_name, parent_class, is_required)

        if isinstance(node, ObjectNode):
            # Objects with additionalProperties schema become dict[str, T]
            if node.additional_properties and not node.properties:
                value_type = self._analyze_type(node.additional_properties, field_name, parent_class, True)
                type_ref = TypeRef(
                    kind=TypeKind.DICT,
                    name="dict",
                    type_args=[TypeRef(kind=TypeKind.PRIMITIVE, name="string"), value_type],
                )
                has_default = "default" in node.metadata
                if not is_required and not has_default:
                    type_ref.is_nullable = True
                return type_ref
            # Objects without properties become Any (matching original codegen.py)
            if not node.properties:
                self.required_imports.add("Any")
                type_ref = TypeRef(kind=TypeKind.ANY, name="Any")
                has_default = "default" in node.metadata
                if not is_required and not has_default:
                    type_ref.is_nullable = True
                return type_ref
            return self._analyze_inline_object_type(node, field_name, parent_class, is_required)

        if isinstance(node, UnionNode):
            return self._analyze_union_type(node, field_name, is_required)

        # Fallback
        return TypeRef(kind=TypeKind.ANY, name="Any")

    def _analyze_ref_type(self, node: RefNode, is_required: bool) -> TypeRef:
        """Analyze a $ref type."""
        resolved = self.ref_resolver.resolve(node)

        type_ref = TypeRef(
            kind=TypeKind.CLASS,
            name=resolved.target_name,
        )

        # Check for quoted types (forward references)
        if resolved.target_name in self.config.quoted_types_for_python:
            type_ref.is_quoted = True

        # Handle default value on $ref
        if "default" in node.metadata:
            type_ref.has_default = True
            type_ref.default_value = node.metadata["default"]
        elif not is_required:
            # For Python: don't make nullable, will use default_factory instead
            # For C#: make nullable
            if self.language == "cs":
                type_ref.is_nullable = True
            # For Python, leave is_nullable=False but mark as needing default_factory
            # The backend will detect this from is_required=False and generate default_factory

        # Register external import for Python
        if resolved.is_external and self.language == "python":
            self._register_external_import(resolved)

        return type_ref

    def _analyze_primitive_type(self, node: PrimitiveNode, is_required: bool) -> TypeRef:
        """Analyze a primitive type."""
        type_name = node.type_name

        # Check for string enum - should become Literal type
        if type_name == "string" and "enum" in node.metadata:
            self.required_imports.add("Literal")
            type_ref = TypeRef(
                kind=TypeKind.ENUM,
                name="string",
                enum_values=node.metadata["enum"],
            )
            # Handle default value
            if "default" in node.metadata:
                type_ref.has_default = True
                type_ref.default_value = node.metadata["default"]
            elif not is_required:
                type_ref.is_nullable = True
            return type_ref

        # Map to language type
        kind = TypeKind.PRIMITIVE
        if type_name == "object":
            kind = TypeKind.ANY
            type_name = "Any"
            self.required_imports.add("any")

        type_ref = TypeRef(kind=kind, name=type_name)

        # Handle default value
        if "default" in node.metadata:
            type_ref.has_default = True
            type_ref.default_value = node.metadata["default"]
        elif not is_required:
            type_ref.is_nullable = True

        return type_ref

    def _analyze_const_type(self, node: ConstNode) -> TypeRef:
        """Analyze a const type."""
        return TypeRef(
            kind=TypeKind.CONST,
            name=node.inferred_type,
            const_value=node.value,
            has_default=True,
            default_value=node.value,
        )

    def _analyze_enum_type(self, node: EnumNode) -> TypeRef:
        """Analyze an inline enum type."""
        type_ref = TypeRef(
            kind=TypeKind.ENUM,
            name=node.inferred_type,
            enum_values=node.values,
            enum_member_names=node.member_names,
        )
        return type_ref

    def _analyze_array_type(
        self,
        node: ArrayNode,
        field_name: str,
        parent_class: str,
        is_required: bool,
    ) -> TypeRef:
        """Analyze an array type."""
        # Determine if this is a tuple or list
        is_tuple = isinstance(node.items, list) and node.min_items == node.max_items and self.config.use_tuples

        if is_tuple and isinstance(node.items, list):
            # Tuple type
            self.required_imports.add("tuple")
            item_types = []
            for item in node.items:
                item_type = self._analyze_type_for_array_item(item, field_name, parent_class)
                item_types.append(item_type)

            type_ref = TypeRef(
                kind=TypeKind.TUPLE,
                name="tuple",
                type_args=item_types,
            )
        else:
            # List type
            self.required_imports.add("list")
            if node.items:
                if isinstance(node.items, list):
                    # Variable length tuple -> compute super type across all items
                    item_type = self._compute_super_type(node.items, field_name, parent_class)
                else:
                    item_type = self._analyze_type_for_array_item(node.items, field_name, parent_class)
            else:
                item_type = TypeRef(kind=TypeKind.ANY, name="Any")

            type_ref = TypeRef(
                kind=TypeKind.ARRAY,
                name="list",
                type_args=[item_type],
            )

        # Handle default value
        if "default" in node.metadata:
            type_ref.has_default = True
            type_ref.default_value = node.metadata["default"]
        elif not is_required:
            type_ref.is_nullable = True

        return type_ref

    def _compute_super_type(
        self,
        items: list[SchemaNode],
        field_name: str,
        parent_class: str,
    ) -> TypeRef:
        """Compute the super type of a list of item schemas (for variable-length tuples).

        Collects all distinct primitive type names across all items and returns a
        union TypeRef when there are multiple distinct types, or a single TypeRef
        when all items share the same type.
        """
        type_names: set[str] = set()
        for item in items:
            if isinstance(item, PrimitiveNode):
                type_names.add(item.type_name)
            elif isinstance(item, UnionNode):
                for variant in item.variants:
                    if isinstance(variant, PrimitiveNode):
                        type_names.add(variant.type_name)

        # Remove null from the set - nullability is handled separately
        type_names.discard("null")

        if len(type_names) <= 1:
            # Single type (or empty) - fall back to first item
            return self._analyze_type_for_array_item(items[0], field_name, parent_class)

        # Multiple types - build a union TypeRef
        sorted_names = sorted(type_names)
        type_args = [TypeRef(kind=TypeKind.PRIMITIVE, name=n) for n in sorted_names]
        return TypeRef(kind=TypeKind.UNION, name="union", type_args=type_args)

    def _analyze_type_for_array_item(
        self,
        node: SchemaNode,
        field_name: str,
        parent_class: str,
    ) -> TypeRef:
        """Analyze a type node for array items.

        For Python: Keep nullable types in unions, will be converted to type alias like NoneOrInt.
        For C#: Keep nullable types in unions, will be converted to T?.
        Also uses simple PascalCase names for inline objects (not parent-prefixed).
        """
        # Handle union types specially
        if isinstance(node, UnionNode):
            # Check if this is a nullable union (T | null)
            null_variants = [v for v in node.variants if isinstance(v, PrimitiveNode) and v.type_name == "null"]
            non_null_variants = [v for v in node.variants if not (isinstance(v, PrimitiveNode) and v.type_name == "null")]

            has_null = len(null_variants) > 0

            if len(non_null_variants) == 1 and has_null:
                # Single non-null type + null
                variant = non_null_variants[0]
                if isinstance(variant, ObjectNode):
                    non_null_type = self._analyze_inline_object_type(variant, field_name, parent_class, True, is_array_item=True)
                else:
                    non_null_type = self._analyze_type(variant, field_name, parent_class, True)

                # Check if this is from type: ["T", "null"] (typeArray) vs oneOf: [...] (explicit)
                # For typeArray: strip null for both Python and C# (matches original codegen.py)
                # For explicit oneOf: Python creates type alias (NoneOrInt), C# creates T?
                is_type_array = node.union_type == "typeArray"

                if is_type_array:
                    # Strip null for type arrays in both Python and C#
                    non_null_type.is_nullable = False
                    return non_null_type
                else:
                    # Create a union type with None/null
                    null_type = TypeRef(kind=TypeKind.PRIMITIVE, name="null")
                    type_ref = TypeRef(
                        kind=TypeKind.UNION,
                        name="union",
                        type_args=[non_null_type, null_type],
                        is_nullable=False,  # The union itself handles nullability
                    )
                    return type_ref
            elif len(non_null_variants) > 1:
                # Multiple non-null types - create union
                types = [self._analyze_type(v, field_name, parent_class, True) for v in non_null_variants]
                # Add null if present
                if has_null:
                    types.append(TypeRef(kind=TypeKind.PRIMITIVE, name="null"))
                type_ref = TypeRef(
                    kind=TypeKind.UNION,
                    name="union",
                    type_args=types,
                    is_nullable=False,
                )
                return type_ref

        # Handle inline objects with is_array_item=True
        if isinstance(node, ObjectNode):
            result = self._analyze_inline_object_type(node, field_name, parent_class, True, is_array_item=True)
            result.is_nullable = False
            return result

        # For non-union, non-object types, analyze normally but don't make nullable
        result = self._analyze_type(node, field_name, parent_class, True)
        result.is_nullable = False
        return result

    def _analyze_inline_object_type(
        self,
        node: ObjectNode,
        field_name: str,
        parent_class: str,
        is_required: bool,
        is_array_item: bool = False,
    ) -> TypeRef:
        """Analyze an inline object type."""
        # Get the inline class name
        if is_array_item:
            # Match original codegen.py bug: array items use simple PascalCase
            inline_name = self._to_pascal_case(field_name)
        else:
            inline_name = self.name_mapping.inline_class_names.get(
                (parent_class, field_name),
                f"{parent_class}{self._to_pascal_case(field_name)}",
            )

        type_ref = TypeRef(
            kind=TypeKind.CLASS,
            name=inline_name,
        )

        if not is_required:
            type_ref.is_nullable = True

        return type_ref

    def _analyze_union_type(
        self,
        node: UnionNode,
        field_name: str,
        is_required: bool,
    ) -> TypeRef:
        """Analyze a union type."""
        # Check for nullable union (T | null)
        types = []
        is_nullable = False

        for variant in node.variants:
            if isinstance(variant, PrimitiveNode) and variant.type_name == "null":
                is_nullable = True
            else:
                variant_type = self._analyze_type(variant, field_name, "", True)
                types.append(variant_type)

        if len(types) == 1 and is_nullable:
            # Simple nullable type
            result = types[0]
            result.is_nullable = True
            return result

        # Multiple types - create union
        type_ref = TypeRef(
            kind=TypeKind.UNION,
            name="union",
            type_args=types,
            is_nullable=is_nullable,
        )

        # Handle default value
        if "default" in node.metadata:
            type_ref.has_default = True
            type_ref.default_value = node.metadata["default"]
        elif not is_required and not is_nullable:
            type_ref.is_nullable = True

        return type_ref

    def _analyze_root_node(self) -> ClassDef | None:
        """Analyze the root node (if it has properties)."""
        if not isinstance(self.ast.root_node, ObjectNode):
            return None

        class_def = ClassDef(
            name=self.ast.root_name,
            original_name=self.ast.root_name,
        )

        class_def.fields = self._analyze_properties(self.ast.root_node, self.ast.root_name)
        class_def.constructor_fields = [f for f in class_def.fields if not f.is_const]

        # Generate validation code if enabled
        if self.validator:
            class_def.validation_code = self._generate_validation_code(self.ast.root_node, class_def)

        return class_def

    def _collect_inline_classes(self) -> list[ClassDef]:
        """Collect inline classes that need to be generated."""
        inline_classes = []
        processed = set()

        # Process inline objects from root node
        if self.ast.root_node and isinstance(self.ast.root_node, ObjectNode):
            self._collect_inline_from_object(
                self.ast.root_node,
                self.ast.root_name,
                inline_classes,
                processed,
            )

        # Process inline objects from definitions
        for def_node in self.ast.definitions:
            class_name = self.name_mapping.definition_names.get(def_node.original_name, def_node.original_name)
            if isinstance(def_node.body, ObjectNode):
                self._collect_inline_from_object(
                    def_node.body,
                    class_name,
                    inline_classes,
                    processed,
                )
            elif isinstance(def_node.body, AllOfNode) and def_node.body.extension:
                self._collect_inline_from_object(
                    def_node.body.extension,
                    class_name,
                    inline_classes,
                    processed,
                )

        # Sort inline classes by name to match original codegen.py behavior
        inline_classes.sort(key=lambda c: c.name)
        return inline_classes

    def _collect_inline_from_object(
        self,
        obj: ObjectNode,
        parent_name: str,
        inline_classes: list[ClassDef],
        processed: set[str],
    ) -> None:
        """Recursively collect inline classes from an object node."""
        for prop in obj.properties:
            if prop.name in self.config.global_ignore_fields:
                continue

            type_node = prop.type_node
            if type_node is None:
                continue

            # Check for inline object
            if isinstance(type_node, ObjectNode) and type_node.properties:
                inline_name = self._get_inline_class_name(parent_name, prop.name)
                if inline_name not in processed:
                    processed.add(inline_name)
                    class_def = ClassDef(
                        name=inline_name,
                        original_name=f"{parent_name}.{prop.name}",
                    )
                    class_def.fields = self._analyze_properties(type_node, inline_name)
                    class_def.constructor_fields = [f for f in class_def.fields if not f.is_const]
                    inline_classes.append(class_def)

                    # Recursively process nested inline objects
                    self._collect_inline_from_object(type_node, inline_name, inline_classes, processed)

            # Check for array of inline objects
            elif isinstance(type_node, ArrayNode):
                items = type_node.items
                if isinstance(items, ObjectNode) and items.properties:
                    # Use is_array_item=True to match original codegen.py bug
                    inline_name = self._get_inline_class_name(parent_name, prop.name, is_array_item=True)

                    # Match original codegen.py behavior: last occurrence wins (overwrites)
                    # Remove existing class with same name if present
                    inline_classes[:] = [c for c in inline_classes if c.name != inline_name]
                    processed.discard(inline_name)

                    processed.add(inline_name)
                    class_def = ClassDef(
                        name=inline_name,
                        original_name=f"{parent_name}.{prop.name}",
                    )
                    class_def.fields = self._analyze_properties(items, inline_name)
                    class_def.constructor_fields = [f for f in class_def.fields if not f.is_const]
                    inline_classes.append(class_def)

                    # Recursively process
                    self._collect_inline_from_object(items, inline_name, inline_classes, processed)

    def _get_inline_class_name(self, parent_name: str, field_name: str, is_array_item: bool = False) -> str:
        """Get the inline class name for a field.

        Args:
            parent_name: The parent class name
            field_name: The field name
            is_array_item: If True, use simple PascalCase name to match original codegen.py bug
                          where array item inline classes aren't tracked in the mapping

        Note: The original codegen.py has a bug where the elif for arrays is at the wrong
        indentation level, so array item inline classes never get added to the mapping.
        They fall back to simple PascalCase names, causing collisions.
        """
        if is_array_item:
            # Match original codegen.py bug: array items use simple PascalCase, not parent-prefixed
            return self._to_pascal_case(field_name)

        key = (parent_name, field_name)
        if key in self.name_mapping.inline_class_names:
            return self.name_mapping.inline_class_names[key]
        return f"{parent_name}{self._to_pascal_case(field_name)}"

    def _register_external_import(self, resolved: ResolvedRef) -> None:
        """Register an import for an external $ref."""
        if not self.config.external_ref_base_module:
            return

        # Convert schema path to module path
        path = resolved.external_path.lstrip("/")
        module_path = path.replace("/", ".").replace("_schema", "_dataclass")
        full_module = f"{self.config.external_ref_base_module}.{module_path}"

        self.python_imports.add((full_module, resolved.target_name))

    def _build_imports(self) -> list[ImportDef]:
        """Build the list of required imports."""
        imports = []

        # Group python_imports by module
        module_to_names: dict[str, list[str]] = {}
        for module, name in self.python_imports:
            if module not in module_to_names:
                module_to_names[module] = []
            module_to_names[module].append(name)

        # Convert to ImportDef objects
        for module, names in module_to_names.items():
            imports.append(ImportDef(module=module, names=sorted(names)))

        return imports

    def _generate_validation_code(self, obj: ObjectNode, class_def: ClassDef) -> list[str]:
        """Generate validation code for a class."""
        if not self.validator:
            return []

        validation_lines = []
        required_fields = obj.required

        for prop in obj.properties:
            if prop.name in self.config.global_ignore_fields:
                continue

            is_required = prop.name in required_fields

            # Get field info in format validator expects
            field_info = self._property_to_validator_info(prop)

            # Get type string
            field_type = ""
            if prop.type_node:
                type_ref = self._analyze_type(prop.type_node, prop.name, class_def.name, is_required)
                # Simple type string for validation
                if type_ref.kind == TypeKind.PRIMITIVE:
                    field_type = type_ref.name
                elif type_ref.kind == TypeKind.ARRAY:
                    field_type = "list"
                elif type_ref.kind == TypeKind.CLASS:
                    field_type = type_ref.name

            # Generate validation
            field_validations = self.validator.generate_field_validation(prop.name, field_info, field_type, is_required)

            # Track if we need 're' import
            if self.validator.needs_re_import(field_info):
                self.needs_re_import = True

            validation_lines.extend(field_validations)

        return validation_lines

    def _property_to_validator_info(self, prop: PropertyDef) -> dict[str, Any]:
        """Convert a PropertyDef to the format expected by ValidationGenerator."""
        info: dict[str, Any] = {}

        if not prop.type_node:
            return info

        node = prop.type_node

        if isinstance(node, PrimitiveNode):
            info["type"] = node.type_name
            if node.min_length is not None:
                info["minLength"] = node.min_length
            if node.max_length is not None:
                info["maxLength"] = node.max_length
            if node.pattern is not None:
                info["pattern"] = node.pattern
            if node.minimum is not None:
                info["minimum"] = node.minimum
            if node.maximum is not None:
                info["maximum"] = node.maximum
            if node.exclusive_minimum is not None:
                info["exclusiveMinimum"] = node.exclusive_minimum
            if node.exclusive_maximum is not None:
                info["exclusiveMaximum"] = node.exclusive_maximum
            if node.multiple_of is not None:
                info["multipleOf"] = node.multiple_of

        elif isinstance(node, ArrayNode):
            info["type"] = "array"
            if node.min_items is not None:
                info["minItems"] = node.min_items
            if node.max_items is not None:
                info["maxItems"] = node.max_items
            if node.items:
                if isinstance(node.items, RefNode):
                    info["items"] = {"$ref": node.items.ref_path}

        elif isinstance(node, RefNode):
            info["$ref"] = node.ref_path

        elif isinstance(node, EnumNode):
            info["enum"] = node.values

        elif isinstance(node, ConstNode):
            info["const"] = node.value

        return info

    def _to_pascal_case(self, text: str) -> str:
        """Convert text to PascalCase."""
        return self.name_resolver._to_pascal_case(text)

    def _to_enum_member_name(self, value: str) -> str:
        """Convert a value to an enum member name."""
        if self.language == "python":
            return value.upper()
        else:
            return self._to_pascal_case(value)
