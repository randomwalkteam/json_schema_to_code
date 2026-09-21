"""
Tests for the V3 pipeline merge functionality.

Tests the AST-level merging of generated code with existing files,
including preservation of custom imports, methods, and __post_init__.
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from json_schema_to_code.pipeline import CodeGeneratorConfig, MergeStrategy, OutputMode, PipelineGenerator
from json_schema_to_code.pipeline.merger import (
    AtomicWriter,
    CodeMergeError,
    PythonAstMerger,
)

# Test schema for generating code
SIMPLE_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "definitions": {
        "Person": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
    },
}


class TestPythonAstMerger:
    """Tests for PythonAstMerger."""

    def test_parse_valid_python(self):
        """Test parsing valid Python code."""
        merger = PythonAstMerger()
        code = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    age: int = 0
"""
        tree = merger.parse(code)
        assert tree is not None

    def test_parse_invalid_python_raises_error(self):
        """Test that invalid Python raises CodeMergeError."""
        merger = PythonAstMerger()
        code = "class Broken("  # Invalid syntax

        with pytest.raises(CodeMergeError):
            merger.parse(code)

    def test_merge_preserves_custom_imports(self):
        """Test that merge preserves custom imports."""
        merger = PythonAstMerger()

        existing = """
from __future__ import annotations
from dataclasses import dataclass
import json

@dataclass
class Person:
    name: str
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    age: int = 0
"""

        merged = merger.merge_files(generated, existing)

        # Should have the custom import
        assert "import json" in merged
        # Should have the new field
        assert "age: int" in merged

    def test_merge_consolidates_duplicate_imports_from_same_module(self):
        """Test that merge consolidates duplicate imports from the same module."""
        merger = PythonAstMerger()

        # Existing file has duplicate imports from same module (a common issue)
        existing = """
from __future__ import annotations
from dataclasses import dataclass
from mymodule import A, B, C
from other import X
from mymodule import A, B, C, D

@dataclass
class Person:
    name: str
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass
from mymodule import A, B, E

@dataclass
class Person:
    name: str
    age: int = 0
"""

        merged = merger.merge_files(generated, existing)

        # Should consolidate into single import with all names
        import_count = merged.count("from mymodule import")
        assert import_count == 1, f"Expected 1 import from mymodule, got {import_count}"

        # Should have all unique names from both imports
        assert "A" in merged
        assert "B" in merged
        assert "C" in merged
        assert "D" in merged
        assert "E" in merged

    def test_merge_preserves_custom_methods(self):
        """Test that merge preserves custom methods."""
        merger = PythonAstMerger()

        existing = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str

    def greet(self):
        return f"Hello, {self.name}!"
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    age: int = 0
"""

        merged = merger.merge_files(generated, existing)

        # Should have the custom method
        assert "def greet(self):" in merged
        # Should have the new field
        assert "age: int" in merged

    def test_merge_raises_when_existing_value_member_missing_in_generated(self):
        """Test that merge fails when existing class has a removed value member."""
        merger = PythonAstMerger()

        existing = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    legacy_value: int = 7
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
"""

        with pytest.raises(CodeMergeError, match="legacy_value"):
            merger.merge_files(generated, existing)

    def test_merge_strategy_merge_keeps_removed_value_members(self):
        merger = PythonAstMerger()
        existing = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    legacy_value: int = 7
"""
        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
"""
        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)
        assert "legacy_value" in merged

    def test_merge_strategy_delete_removes_extra_value_members(self):
        merger = PythonAstMerger()
        existing = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    legacy_value: int = 7
"""
        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
"""
        merged = merger.merge_files(generated, existing, MergeStrategy.DELETE)
        assert "legacy_value" not in merged
        assert "name: str" in merged

    def test_merge_empty_custom_code_returns_generated(self):
        """Test that merge with no custom code returns generated as-is."""
        merger = PythonAstMerger()

        existing = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    age: int = 0
"""

        merged = merger.merge_files(generated, existing)

        # Should be the same as generated (no custom code to preserve)
        assert "age: int" in merged

    def test_merge_preserves_custom_classes(self):
        """Test that merge preserves custom class definitions (e.g., Enums)."""
        merger = PythonAstMerger()

        existing = """
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class ColorFlag(str, Enum):
    NORMAL = "normal"
    ERROR = "error"

@dataclass
class Person:
    name: str
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    age: int = 0
"""

        merged = merger.merge_files(generated, existing)

        # Should preserve the custom Enum class
        assert "class ColorFlag" in merged
        assert "NORMAL" in merged
        assert "ERROR" in merged
        # Should have the generated field
        assert "age: int" in merged

    def test_no_merge_marker_preserves_field(self):
        """Field with # jstc-no-merge is kept as-is, ignoring generated version."""
        merger = PythonAstMerger()

        existing = (
            "from __future__ import annotations\n"
            "from dataclasses import dataclass, field\n"
            "from dataclasses_json import config\n"
            "\n"
            "MY_CONFIG = config(encoder=lambda v: v)\n"
            "\n"
            "@dataclass\n"
            "class Person:\n"
            "    name: str = field(metadata=MY_CONFIG)  # jstc-no-merge\n"
            "    age: int = 0\n"
        )

        generated = "from __future__ import annotations\n" "from dataclasses import dataclass\n" "\n" "@dataclass\n" "class Person:\n" "    name: str\n" "    age: int = 0\n"

        merged = merger.merge_files(generated, existing)
        assert "field(metadata=MY_CONFIG)" in merged
        assert "age: int" in merged

    def test_field_metadata_preserved_without_marker(self):
        """Field with field(metadata=...) is preserved automatically, no marker needed."""
        merger = PythonAstMerger()

        existing = (
            "from __future__ import annotations\n"
            "from dataclasses import dataclass, field\n"
            "from dataclasses_json import config\n"
            "\n"
            "MY_CONFIG = config(encoder=lambda v: v)\n"
            "\n"
            "@dataclass\n"
            "class Person:\n"
            "    name: str = field(metadata=MY_CONFIG)\n"
            "    age: int = 0\n"
        )

        generated = "from __future__ import annotations\n" "from dataclasses import dataclass\n" "\n" "@dataclass\n" "class Person:\n" "    name: str\n" "    age: int = 0\n"

        merged = merger.merge_files(generated, existing)
        assert "field(metadata=MY_CONFIG)" in merged
        assert "age: int" in merged

    def test_normal_field_still_merges(self):
        """Simple fields without metadata are merged normally from generated."""
        merger = PythonAstMerger()

        existing = "from __future__ import annotations\n" "from dataclasses import dataclass\n" "\n" "@dataclass\n" "class Person:\n" "    name: str\n" "    age: int = 0\n"

        generated = "from __future__ import annotations\n" "from dataclasses import dataclass\n" "\n" "@dataclass\n" "class Person:\n" "    name: str\n" "    age: int = 99\n"

        merged = merger.merge_files(generated, existing)
        assert "age: int = 99" in merged

    def test_validate_valid_code(self):
        """Test validation passes for valid code."""
        merger = PythonAstMerger()
        code = """
from __future__ import annotations

class Person:
    pass
"""
        # Should not raise
        merger.validate(code)

    def test_validate_invalid_code_raises(self):
        """Test validation fails for invalid code."""
        merger = PythonAstMerger()
        code = "class Broken("

        with pytest.raises(CodeMergeError):
            merger.validate(code)


class TestAtomicWriter:
    """Tests for AtomicWriter."""

    def test_write_creates_file(self):
        """Test that write creates a new file."""
        writer = AtomicWriter()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "output.py"
            code = """
from __future__ import annotations

class Person:
    pass
"""
            writer.write(path, code, "python")

            assert path.exists()
            assert path.read_text() == code

    def test_write_if_not_exists_raises_on_existing(self):
        """Test that write_if_not_exists raises if file exists."""
        writer = AtomicWriter()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "existing.py"
            path.write_text("existing content")

            with pytest.raises(FileExistsError):
                writer.write_if_not_exists(path, "new content", "python", validate=False)

    def test_write_overwrites_existing(self):
        """Test that write overwrites existing file."""
        writer = AtomicWriter()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "existing.py"
            path.write_text("old content")

            new_code = """
from __future__ import annotations

class NewClass:
    pass
"""
            writer.write(path, new_code, "python")

            assert path.read_text() == new_code

    def test_write_validates_python(self):
        """Test that write validates Python code."""
        writer = AtomicWriter()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "output.py"
            invalid_code = "class Broken("

            with pytest.raises(CodeMergeError):
                writer.write(path, invalid_code, "python", validate=True)

            # File should not exist after failed write
            assert not path.exists()

    def test_write_without_validation(self):
        """Test that write works without validation."""
        writer = AtomicWriter()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "output.py"
            # This is technically invalid Python
            code = "not really python code"

            writer.write(path, code, "python", validate=False)

            assert path.exists()
            assert path.read_text() == code


class TestGeneratorMerge:
    """Tests for PipelineGenerator merge functionality."""

    def test_generate_to_file_error_if_exists(self):
        """Test that default mode raises error if file exists."""
        config = CodeGeneratorConfig()
        config.output.mode = OutputMode.ERROR_IF_EXISTS
        config.add_generation_comment = False

        gen = PipelineGenerator("Test", SIMPLE_SCHEMA, config, "python")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "output.py"
            path.write_text("existing content")

            with pytest.raises(FileExistsError):
                gen.generate_to_file(path)

    def test_generate_to_file_force_overwrites(self):
        """Test that force mode overwrites existing file."""
        config = CodeGeneratorConfig()
        config.output.mode = OutputMode.OVERWRITE
        config.add_generation_comment = False

        gen = PipelineGenerator("Test", SIMPLE_SCHEMA, config, "python")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "output.py"
            path.write_text("old content")

            gen.generate_to_file(path)

            content = path.read_text()
            assert "class Person:" in content

    def test_generate_to_file_creates_new(self):
        """Test that generate_to_file creates new file when none exists."""
        config = CodeGeneratorConfig()
        config.output.mode = OutputMode.ERROR_IF_EXISTS
        config.add_generation_comment = False

        gen = PipelineGenerator("Test", SIMPLE_SCHEMA, config, "python")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "output.py"

            gen.generate_to_file(path)

            assert path.exists()
            content = path.read_text()
            assert "class Person:" in content

    def test_generate_to_file_merge_preserves_custom(self):
        """Test that merge mode preserves custom code."""
        config = CodeGeneratorConfig()
        config.output.mode = OutputMode.MERGE
        config.add_generation_comment = False

        gen = PipelineGenerator("Test", SIMPLE_SCHEMA, config, "python")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "output.py"

            # Create existing file with custom code
            existing = """
from __future__ import annotations
from dataclasses import dataclass
from dataclasses_json import dataclass_json
import json

MY_CONSTANT = 42

@dataclass_json
@dataclass(kw_only=True)
class Person:
    name: str

    def custom_method(self):
        return json.dumps({"name": self.name})
"""
            path.write_text(existing)

            gen.generate_to_file(path)

            content = path.read_text()
            # Should preserve custom import
            assert "import json" in content
            # Should preserve constant
            assert "MY_CONSTANT" in content
            # Should preserve custom method
            assert "custom_method" in content
            # Should have generated field
            assert "age" in content or "name" in content


class TestCSharpMerger:
    """Tests for CSharpAstMerger."""

    def test_csharp_merger_requires_tree_sitter(self):
        """Test that C# merger checks for tree-sitter availability."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger

            # If we get here, tree-sitter is available
            merger = CSharpAstMerger()
            assert merger is not None
        except CodeMergeError as e:
            # Expected if tree-sitter not installed
            assert "tree-sitter" in str(e)
        except ImportError:
            # Also acceptable if the import itself fails
            pytest.skip("tree-sitter-c-sharp not installed")

    def test_csharp_merge_preserves_custom_code_after_non_ascii_text(self):
        """Regression: tree-sitter offsets are UTF-8 bytes, so every custom member after
        a multi-byte character was sliced a few characters off."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Note {
        [JsonProperty("name")]
        public string Name { get; set; }
    }
}
"""
        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    /// Clé — do → ré.
    public class Note {
        [JsonProperty("name")]
        public string Name { get; set; }

        // Solfège — « do ré mi ».
        public string Describe() { return "é" + Name; }
    }
}
"""
        merged = CSharpAstMerger().merge_files(generated, existing, MergeStrategy.MERGE)

        assert "// Solfège — « do ré mi ».\n" in merged
        assert 'public string Describe() { return "é" + Name; }' in merged

    def test_csharp_merge_does_not_duplicate_properties(self):
        """Regression: merging with corrupted file (duplicate property) must not preserve duplicate."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()

        generated = """
using System;
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Test {
    public class UIAction {
        [JsonProperty("operations")]
        public List<object> Operations { get; set; }
        [JsonProperty("metadata")]
        public object Metadata { get; set; }
    }
}
"""

        # Existing file corrupted by previous bad merge - has duplicate Metadata
        existing = """
using System;
using System.Collections.Generic;
using Newtonsoft.Json;

namespace Test {
    public class UIAction {
        [JsonProperty("operations")]
        public List<object> Operations { get; set; }
        [JsonProperty("metadata")]
        public object Metadata { get; set; }

    [JsonProperty("metadata")]
            public object Metadata { get; set; }
    }
}
"""

        merged = merger.merge_files(generated, existing)

        # Must have exactly one Metadata property, not two
        metadata_count = merged.count("public object Metadata { get; set; }")
        assert metadata_count == 1, f"Expected 1 Metadata property, got {metadata_count}"

    def test_csharp_merge_raises_when_existing_value_member_missing_in_generated(self):
        """Test that C# merge fails when existing class has removed property."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()

        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }
    }
}
"""

        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }

        [JsonProperty("legacy_value")]
        public int LegacyValue { get; set; }
    }
}
"""

        with pytest.raises(CodeMergeError, match="LegacyValue"):
            merger.merge_files(generated, existing)

    def test_csharp_merge_strategy_merge_keeps_removed_property(self):
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()
        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }
    }
}
"""
        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }

        [JsonProperty("legacy_value")]
        public int LegacyValue { get; set; }
    }
}
"""
        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)
        assert "LegacyValue" in merged

    def test_csharp_merge_strategy_delete_removes_extra_property(self):
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()
        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }
    }
}
"""
        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }

        [JsonProperty("legacy_value")]
        public int LegacyValue { get; set; }
    }
}
"""
        merged = merger.merge_files(generated, existing, MergeStrategy.DELETE)
        assert "LegacyValue" not in merged
        assert "Name" in merged

    def test_csharp_no_merge_marker_preserves_property_type(self):
        """Property with // jstc-no-merge keeps existing type, ignoring generated version."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()
        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class BinaryBlob {
        [JsonProperty("id")]
        public string Id { get; set; }
        [JsonProperty("data")]
        public string Data { get; set; }
    }
}
"""
        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class BinaryBlob {
        [JsonProperty("id")]
        public string Id { get; set; }
        [JsonProperty("data")]
        public byte[] Data { get; set; } // jstc-no-merge
    }
}
"""
        merged = merger.merge_files(generated, existing)
        assert "byte[] Data" in merged
        assert "string Data" not in merged
        assert 'JsonProperty("data")' in merged
        assert "string Id" in merged

    def test_csharp_no_merge_marker_preserves_constructor(self):
        """Constructor with // jstc-no-merge keeps existing signature."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()
        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class BinaryBlob {
        [JsonProperty("id")]
        public string Id { get; set; }
        [JsonProperty("data")]
        public string Data { get; set; }
        public BinaryBlob(string id, string data)
        {
            this.Id = id;
            this.Data = data;
        }
        public BinaryBlob() { }
    }
}
"""
        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class BinaryBlob {
        [JsonProperty("id")]
        public string Id { get; set; }
        [JsonProperty("data")]
        public byte[] Data { get; set; } // jstc-no-merge
        public BinaryBlob(string id, byte[] data) // jstc-no-merge
        {
            this.Id = id;
            this.Data = data;
        }
        public BinaryBlob() { }
    }
}
"""
        merged = merger.merge_files(generated, existing)
        assert "byte[] Data" in merged
        assert "byte[] data" in merged
        assert "string Data" not in merged
        assert "string data" not in merged
        assert "BinaryBlob() { }" in merged

    def test_csharp_custom_constructor_overload_preserved(self):
        """Constructor with different param count than generated is preserved as custom."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()
        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }
        public Person(string name)
        {
            this.Name = name;
        }
        public Person() { }
    }
}
"""
        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }
        public Person(string name)
        {
            this.Name = name;
        }
        public Person() { }
        public Person(string name, int age)
        {
            this.Name = name;
        }
    }
}
"""
        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)
        assert "Person(string name, int age)" in merged
        assert "Person(string name)" in merged
        assert "Person() { }" in merged

    def test_csharp_custom_property_preserves_preceding_attribute(self):
        """Custom property (not in generated) preserves its preceding [JsonProperty] attribute."""
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

        merger = CSharpAstMerger()
        generated = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }
    }
}
"""
        existing = """
using System;
using Newtonsoft.Json;

namespace Test {
    public class Person {
        [JsonProperty("name")]
        public string Name { get; set; }
        [JsonProperty("data")]
        public byte[] Data { get; set; }
    }
}
"""
        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)
        assert "byte[] Data" in merged
        assert 'JsonProperty("data")' in merged


class TestPythonFutureImportOrdering:
    """Tests that from __future__ import annotations is always placed first."""

    def test_future_import_added_at_top_when_missing_from_existing(self):
        """When existing file lacks __future__ import, merger should add it at position 0."""
        merger = PythonAstMerger()

        existing = """
import random
from dataclasses import dataclass, field
from dataclasses_json import dataclass_json

@dataclass_json
@dataclass(kw_only=True)
class Person:
    name: str
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass, field
from dataclasses_json import dataclass_json

@dataclass_json
@dataclass(kw_only=True)
class Person:
    name: str
    age: int = 0
"""

        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)

        lines = [line for line in merged.splitlines() if line.strip()]
        future_idx = next(i for i, line in enumerate(lines) if "__future__" in line)
        first_other_import = next(i for i, line in enumerate(lines) if ("import " in line or "from " in line) and "__future__" not in line)
        assert future_idx < first_other_import, f"__future__ import at line {future_idx} should be before " f"first other import at line {first_other_import}"

    def test_future_import_stays_first_when_already_present(self):
        """When existing file already has __future__ at top, order is preserved."""
        merger = PythonAstMerger()

        existing = """
from __future__ import annotations
import random
from dataclasses import dataclass

@dataclass
class Person:
    name: str
"""

        generated = """
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Person:
    name: str
    age: int = 0
"""

        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)

        lines = [line for line in merged.splitlines() if line.strip()]
        future_idx = next(i for i, line in enumerate(lines) if "__future__" in line)
        assert future_idx == 0, f"__future__ should be the first line, but was at index {future_idx}"


class TestPythonCommentPreservation:
    """Tests that Python comments survive merge round-trips."""

    @staticmethod
    def _load_comment_preservation_tests():
        import json

        test_file = Path(__file__).parent.parent / "test_data" / "functional" / "comment_preservation_merge_tests.json"
        with open(test_file) as f:
            return json.load(f)

    @pytest.fixture(params=_load_comment_preservation_tests.__func__(), ids=lambda t: t["name"])
    def test_case(self, request):
        return request.param

    def test_comment_preservation(self, test_case):
        merger = PythonAstMerger()
        existing = "\n".join(test_case["existing_lines"])
        generated = "\n".join(test_case["generated_lines"])
        round_trips = test_case.get("round_trips", 1)

        merged = existing
        for _ in range(round_trips):
            merged = merger.merge_files(generated, merged, MergeStrategy.MERGE)

        for expected in test_case["expected_contains"]:
            assert expected in merged, f"Expected '{expected}' in merged output:\n{merged}"
        for not_expected in test_case["expected_not_contains"]:
            assert not_expected not in merged, f"Did not expect '{not_expected}' in merged output:\n{merged}"


class TestPythonNoMergeMarkerPersistence:
    """Tests that # jstc-no-merge markers survive multiple merge round-trips."""

    def test_no_merge_marker_preserved_after_unparse(self):
        """Field type and marker survive ast.unparse() round-trip."""
        merger = PythonAstMerger()
        generated = textwrap.dedent("""\
            from __future__ import annotations
            from dataclasses import dataclass
            @dataclass(kw_only=True)
            class BinaryBlob:
                blobId: str
                data: str
                final: bool
        """)
        existing = textwrap.dedent("""\
            from __future__ import annotations
            from dataclasses import dataclass
            @dataclass(kw_only=True)
            class BinaryBlob:
                blobId: str
                data: bytes  # jstc-no-merge
                final: bool
        """)
        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)
        assert "data: bytes" in merged
        assert "# jstc-no-merge" in merged
        assert "data: str" not in merged

    def test_no_merge_marker_survives_two_consecutive_merges(self):
        """Marker and type override survive two consecutive merge cycles."""
        merger = PythonAstMerger()
        generated = textwrap.dedent("""\
            from __future__ import annotations
            from dataclasses import dataclass
            @dataclass(kw_only=True)
            class BinaryBlob:
                blobId: str
                data: str
                final: bool
        """)
        existing = textwrap.dedent("""\
            from __future__ import annotations
            from dataclasses import dataclass
            @dataclass(kw_only=True)
            class BinaryBlob:
                blobId: str
                data: bytes  # jstc-no-merge
                final: bool
        """)
        merged_once = merger.merge_files(generated, existing, MergeStrategy.MERGE)
        assert "data: bytes" in merged_once
        assert "# jstc-no-merge" in merged_once

        merged_twice = merger.merge_files(generated, merged_once, MergeStrategy.MERGE)
        assert "data: bytes" in merged_twice
        assert "# jstc-no-merge" in merged_twice
        assert "data: str" not in merged_twice

    def test_no_merge_marker_only_on_marked_fields(self):
        """Marker is only re-added to fields that originally had it."""
        merger = PythonAstMerger()
        generated = textwrap.dedent("""\
            from __future__ import annotations
            from dataclasses import dataclass
            @dataclass(kw_only=True)
            class BinaryBlob:
                blobId: str
                data: str
                final: bool
        """)
        existing = textwrap.dedent("""\
            from __future__ import annotations
            from dataclasses import dataclass
            @dataclass(kw_only=True)
            class BinaryBlob:
                blobId: str
                data: bytes  # jstc-no-merge
                final: bool
        """)
        merged = merger.merge_files(generated, existing, MergeStrategy.MERGE)
        for line in merged.splitlines():
            stripped = line.strip()
            if stripped.startswith("blobId:") or stripped.startswith("final:"):
                assert "jstc-no-merge" not in line


class TestCSharpMemberComments:
    """Comments above C# members survive a merge, whether the member is generated or custom."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        try:
            from json_schema_to_code.pipeline.merger import CSharpAstMerger

            self.merger = CSharpAstMerger()
        except (CodeMergeError, ImportError):
            pytest.skip("tree-sitter-c-sharp not installed")

    def test_merge_preserves_comment_before_generated_constructor(self):
        existing = (
            "using System;\n"
            "namespace Foo\n"
            "{\n"
            "    [Serializable]\n"
            "    public class Bar\n"
            "    {\n"
            "        public Bar(string name)\n"
            "        {\n"
            "        }\n"
            "        // Parameterless constructor for Unity\n"
            "        public Bar() { }\n"
            "    }\n"
            "}\n"
        )
        generated = (
            "using System;\n"
            "namespace Foo\n"
            "{\n"
            "    [Serializable]\n"
            "    public class Bar\n"
            "    {\n"
            "        public Bar(string name)\n"
            "        {\n"
            "        }\n"
            "        public Bar() { }\n"
            "    }\n"
            "}\n"
        )
        merged = self.merger.merge_files(generated, existing)
        assert "// Parameterless constructor for Unity" in merged
        assert "public Bar() { }" in merged

    def test_merge_preserves_comment_before_generated_property(self):
        existing = (
            "using System;\n"
            "namespace Foo\n"
            "{\n"
            "    public class Person\n"
            "    {\n"
            "        // Full legal name\n"
            "        public string Name { get; set; }\n"
            "        public int Age { get; set; }\n"
            "    }\n"
            "}\n"
        )
        generated = (
            "using System;\n" "namespace Foo\n" "{\n" "    public class Person\n" "    {\n" "        public string Name { get; set; }\n" "        public int Age { get; set; }\n" "    }\n" "}\n"
        )
        merged = self.merger.merge_files(generated, existing)
        assert "// Full legal name" in merged
        assert "public string Name" in merged

    def test_merge_preserves_multiple_comment_lines(self):
        existing = (
            "using System;\n"
            "namespace Foo\n"
            "{\n"
            "    public class Bar\n"
            "    {\n"
            "        // First line of comment\n"
            "        // Second line of comment\n"
            "        public string Name { get; set; }\n"
            "    }\n"
            "}\n"
        )
        generated = "using System;\n" "namespace Foo\n" "{\n" "    public class Bar\n" "    {\n" "        public string Name { get; set; }\n" "    }\n" "}\n"
        merged = self.merger.merge_files(generated, existing)
        assert "// First line of comment" in merged
        assert "// Second line of comment" in merged

    def test_merge_preserves_comment_before_custom_method(self):
        existing = (
            "using System;\n"
            "namespace Foo\n"
            "{\n"
            "    public class Bar\n"
            "    {\n"
            "        public string Name { get; set; }\n"
            "        // Custom helper for serialization\n"
            "        public string ToJson()\n"
            "        {\n"
            "            return Name;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        generated = "using System;\n" "namespace Foo\n" "{\n" "    public class Bar\n" "    {\n" "        public string Name { get; set; }\n" "    }\n" "}\n"
        merged = self.merger.merge_files(generated, existing)
        assert "// Custom helper for serialization" in merged
        assert "public string ToJson()" in merged

    def test_no_comment_no_change(self):
        existing = "using System;\n" "namespace Foo\n" "{\n" "    public class Bar\n" "    {\n" "        public string Name { get; set; }\n" "    }\n" "}\n"
        generated = "using System;\n" "namespace Foo\n" "{\n" "    public class Bar\n" "    {\n" "        public string Name { get; set; }\n" "        public int Age { get; set; }\n" "    }\n" "}\n"
        merged = self.merger.merge_files(generated, existing)
        assert merged == generated


def test_merge_adds_new_union_alias_after_its_dependencies():
    """A new oneOf gains a type alias, placed where it actually resolves.

    The generator emits every alias in one trailing block. Appending that block
    verbatim would put a new alias below the classes it unions -- a NameError,
    since ``A | B`` evaluates eagerly -- and below the class annotating with it.
    """
    generated = textwrap.dedent(
        """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

        @dataclass
        class Dog:
            name: str

        @dataclass
        class Owner:
            pet: Pet

        Pet = Cat | Dog
        """
    ).strip()

    # The existing file predates Dog/Owner/Pet: only Cat had been generated.
    existing = textwrap.dedent(
        """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

            def speak(self) -> str:
                return "meow"
        """
    ).strip()

    merged = PythonAstMerger().merge_files(generated, existing, MergeStrategy.MERGE)

    # Hand-written behaviour survives.
    assert "def speak" in merged

    lines = merged.splitlines()

    def index_of(fragment: str) -> int:
        return next(i for i, line in enumerate(lines) if fragment in line)

    alias = index_of("Pet = ")
    assert alias > index_of("class Cat")
    assert alias > index_of("class Dog")
    assert alias < index_of("class Owner")

    # And the merged module is importable: eager `A | B` would raise otherwise.
    namespace: dict = {}
    exec(compile(merged, "<merged>", "exec"), namespace)
    assert namespace["Pet"] is not None


def _merge(generated: str, existing: str) -> str:
    return PythonAstMerger().merge_files(textwrap.dedent(generated).strip(), textwrap.dedent(existing).strip(), MergeStrategy.MERGE)


def _exec(code: str) -> dict:
    namespace: dict = {}
    exec(compile(code, "<merged>", "exec"), namespace)
    return namespace


def test_merge_updates_an_alias_that_gained_a_member():
    """A module-level alias is schema-owned: when the union gains a variant, the
    file's alias follows -- and moves below the new class, which the merge appends."""
    generated = """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

        @dataclass
        class Dog:
            name: str

        @dataclass
        class Bird:
            name: str

        @dataclass
        class Owner:
            pet: Pet

        Pet = Bird | Cat | Dog
        """
    existing = """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

        @dataclass
        class Dog:
            name: str

        Pet = Cat | Dog

        @dataclass
        class Owner:
            pet: Pet

            def feed(self) -> None:
                pass
        """
    merged = _merge(generated, existing)

    assert "def feed" in merged
    assert merged.count("Pet = ") == 1
    assert "Pet = Bird | Cat | Dog" in merged
    lines = merged.splitlines()
    alias = next(i for i, line in enumerate(lines) if line.startswith("Pet = "))
    assert alias > next(i for i, line in enumerate(lines) if "class Bird" in line)
    namespace = _exec(merged)
    assert namespace["Pet"] == namespace["Bird"] | namespace["Cat"] | namespace["Dog"]


def test_merge_keeps_an_unchanged_alias_where_it_stands():
    """No churn: an alias the schema did not change stays exactly where the file had it."""
    generated = """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

        @dataclass
        class Dog:
            name: str

        Pet = Cat | Dog
        """
    existing = """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

        @dataclass
        class Dog:
            name: str

        HELPER = 1

        Pet = Cat | Dog
        """
    merged = _merge(generated, existing)

    body = [line for line in merged.splitlines() if line and not line.startswith(" ")]
    assert body.index("HELPER = 1") < body.index("Pet = Cat | Dog")
    assert merged.count("Pet = ") == 1


def test_merge_keeps_a_marked_alias_verbatim_across_regenerations():
    """`# jstc-no-merge` on a module-level alias pins it: dataclasses_json decodes a
    union's members in order, so a hand-ordered alias must not take the generator's
    alphabetical one -- and the marker itself must survive the round trip."""
    generated = """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

        @dataclass
        class Dog:
            name: str

        Pet = Cat | Dog
        """
    existing = """
        from __future__ import annotations
        from dataclasses import dataclass

        @dataclass
        class Cat:
            name: str

        @dataclass
        class Dog:
            name: str

        Pet = (
            Dog | Cat
        )  # jstc-no-merge
        """
    once = _merge(generated, existing)
    assert "Pet = Dog | Cat  # jstc-no-merge" in once
    assert "Cat | Dog" not in once

    twice = _merge(generated, once)
    assert "Pet = Dog | Cat  # jstc-no-merge" in twice


def _decorators_above(merged: str, class_name: str) -> list[str]:
    lines = merged.splitlines()
    end = next(i for i, line in enumerate(lines) if line.startswith(f"class {class_name}"))
    start = end
    while start > 0 and lines[start - 1].startswith("@"):
        start -= 1
    return lines[start:end]


def test_merge_restores_generated_decorators_on_an_undecorated_base():
    """A file generated while polymorphic bases were emitted bare heals on the next merge."""
    generated = """
        from dataclasses import dataclass
        from dataclasses_json import dataclass_json

        @dataclass_json
        @dataclass(kw_only=True)
        class Base:
            kind: str
        """
    existing = """
        from dataclasses import dataclass
        from dataclasses_json import dataclass_json

        class Base:
            kind: str
        """
    merged = _merge(generated, existing)

    assert _decorators_above(merged, "Base") == ["@dataclass_json", "@dataclass(kw_only=True)"]


def test_merge_keeps_a_hand_added_decorator():
    generated = """
        from dataclasses import dataclass

        @dataclass(kw_only=True)
        class Point:
            x: int
        """
    existing = """
        import functools
        from dataclasses import dataclass

        @functools.total_ordering
        @dataclass(kw_only=True)
        class Point:
            x: int

            def __lt__(self, other):
                return self.x < other.x
        """
    merged = _merge(generated, existing)

    assert _decorators_above(merged, "Point") == ["@dataclass(kw_only=True)", "@functools.total_ordering"]
    assert "def __lt__" in merged


def test_merge_takes_a_generated_decorator_whose_arguments_changed():
    """`@dataclass` -> `@dataclass(kw_only=True)` is one decorator changing, not two."""
    generated = """
        from dataclasses import dataclass

        @dataclass(kw_only=True)
        class Point:
            x: int
        """
    existing = """
        from dataclasses import dataclass

        @dataclass
        class Point:
            x: int
        """
    merged = _merge(generated, existing)

    assert _decorators_above(merged, "Point") == ["@dataclass(kw_only=True)"]


if __name__ == "__main__":
    pytest.main([__file__])
