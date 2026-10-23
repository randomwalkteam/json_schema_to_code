"""C++ backend: the generated header compiles and round-trips real JSON.

The compile tests build a small program against nlohmann/json with the system C++
compiler. They are skipped when no compiler or nlohmann/json header is available
(set NLOHMANN_JSON_INCLUDE to the directory containing ``nlohmann/json.hpp``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from json_schema_to_code.pipeline.config import CodeGeneratorConfig
from json_schema_to_code.pipeline.generator import PipelineGenerator

SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "definitions": {
        "Color": {"type": "string", "enum": ["red", "green", "blue_ish"]},
        "Point": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"],
        },
        "Circle": {
            "type": "object",
            "properties": {"type": {"const": "circle"}, "radius": {"type": "number"}},
            "required": ["type", "radius"],
        },
        "Square": {
            "type": "object",
            "properties": {"type": {"const": "square"}, "side": {"type": "number"}},
            "required": ["type", "side"],
        },
        "Shape": {
            "oneOf": [{"$ref": "#/definitions/Circle"}, {"$ref": "#/definitions/Square"}],
            "discriminator": {"propertyName": "type"},
        },
        "Node": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "children": {"type": "array", "items": {"$ref": "#/definitions/Node"}, "default": []},
            },
            "required": ["name"],
        },
        "Base": {
            "type": "object",
            "properties": {"kind": {"type": "string", "default": "base"}},
        },
        "Scene": {
            "allOf": [
                {"$ref": "#/definitions/Base"},
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "ratio": {"type": "number", "default": 1.5},
                        "is_active": {"type": "boolean", "default": True},
                        "nickname": {"type": ["string", "null"]},
                        "label": {"type": ["string", "null"]},
                        "tags": {"type": "array", "items": {"type": "string"}, "default": ["a", "b"]},
                        "links": {"type": "object", "additionalProperties": {"type": "integer"}},
                        "color": {"$ref": "#/definitions/Color", "default": "blue_ish"},
                        "class": {"type": "string", "default": "x"},
                        "content-type": {"type": "string", "default": "text/plain"},
                        "origin": {"$ref": "#/definitions/Point", "default": {"y": 2, "x": 1}},
                        "shape": {"$ref": "#/definitions/Shape"},
                        "shapes": {"type": "array", "items": {"$ref": "#/definitions/Shape"}, "default": []},
                        "tree": {"$ref": "#/definitions/Node"},
                        "extra": {"type": "object", "default": {"a": [1, 2]}},
                        "note": {"type": "string"},
                    },
                    "required": ["id", "label", "links", "shape"],
                },
            ]
        },
    },
}

MAIN = r"""
#include "scene.hpp"
#include <cassert>
#include <iostream>

using nlohmann::json;
using namespace demo;

int main() {
    // Minimal payload: every default applies.
    auto minimal = json::parse(R"({"id": 7, "label": null, "links": {}, "shape": {"type": "circle", "radius": 2}})");
    Scene scene = minimal.get<Scene>();
    assert(scene.kind == "base");
    assert(scene.ratio == 1.5 && scene.is_active);
    assert((scene.tags == std::vector<std::string>{"a", "b"}));
    assert(scene.color == Color::BlueIsh);
    assert(scene.class_ == "x" && scene.content_type == "text/plain");
    assert(scene.origin.x == 1 && scene.origin.y == 2);
    assert(std::get<Circle>(scene.shape).radius == 2);
    assert(!scene.nickname && !scene.label && !scene.note && !scene.tree);
    assert(scene.extra == json::parse(R"({"a": [1, 2]})"));

    // Full payload round-trips exactly.
    auto full = json::parse(R"({
        "kind": "k", "id": 1, "ratio": 0.25, "is_active": false, "nickname": "n", "label": "l",
        "tags": [], "links": {"a": 1}, "color": "green", "class": "c",
        "content-type": "x", "origin": {"x": 3, "y": 4},
        "shape": {"type": "square", "side": 5},
        "shapes": [{"type": "circle", "radius": 1}, {"type": "square", "side": 2}],
        "tree": {"name": "root", "children": [{"name": "leaf", "children": []}]},
        "extra": {"anything": true}, "note": "hello"
    })");
    Scene parsed = full.get<Scene>();
    assert(json(parsed) == full);
    assert(parsed == json(parsed).get<Scene>());
    assert(std::get<Square>(parsed.shapes[1]).side == 2);
    assert(parsed.tree->children[0].name == "leaf");

    // Required nullable is written as null, optional absent is omitted.
    json written = scene;
    assert(written.contains("label") && written["label"].is_null());
    assert(!written.contains("nickname") && !written.contains("note"));

    // Unknown enum value and unknown discriminator are errors, not silent defaults.
    bool threw = false;
    try { json::parse(R"("purple")").get<Color>(); } catch (const std::invalid_argument&) { threw = true; }
    assert(threw);
    threw = false;
    try { json::parse(R"({"type": "hexagon"})").get<Shape>(); } catch (const std::invalid_argument&) { threw = true; }
    assert(threw);

    // A missing required key is an error.
    threw = false;
    try { json::parse(R"({"label": null, "links": {}, "shape": {"type": "circle", "radius": 1}})").get<Scene>(); }
    catch (const json::exception&) { threw = true; }
    assert(threw);

    std::cout << "ok" << std::endl;
    return 0;
}
"""


def _generate(schema: dict, **config_values) -> str:
    config = CodeGeneratorConfig()
    config.add_generation_comment = False
    for key, value in config_values.items():
        setattr(config, key, value)
    return PipelineGenerator("Scene", schema, config, "cpp").generate()


def _nlohmann_include() -> Path | None:
    candidates = [os.environ.get("NLOHMANN_JSON_INCLUDE", "")]
    candidates += [f"{sys.prefix}/include", f"{sys.base_prefix}/include", "/opt/homebrew/include", "/usr/local/include", "/usr/include"]
    conda = os.environ.get("CONDA_PREFIX")
    if conda:
        candidates.append(f"{conda}/include")
    conda_exe = os.environ.get("CONDA_EXE")  # base install, when running from a conda env
    if conda_exe:
        candidates.append(str(Path(conda_exe).parents[1] / "include"))
    for candidate in candidates:
        if candidate and (Path(candidate) / "nlohmann" / "json.hpp").exists():
            return Path(candidate)
    return None


def _compiler() -> str | None:
    return next((c for c in (os.environ.get("CXX"), "clang++", "g++", "c++") if c and shutil.which(c)), None)


requires_toolchain = pytest.mark.skipif(
    _compiler() is None or _nlohmann_include() is None,
    reason="needs a C++20 compiler and nlohmann/json (set NLOHMANN_JSON_INCLUDE)",
)


@requires_toolchain
def test_generated_header_compiles_and_round_trips(tmp_path):
    (tmp_path / "scene.hpp").write_text(_generate(SCHEMA, cpp_namespace="demo"))
    (tmp_path / "main.cpp").write_text(MAIN)
    binary = tmp_path / "main"
    build = subprocess.run(
        [_compiler(), "-std=c++20", "-Wall", "-Wextra", "-Werror", "-isystem", str(_nlohmann_include()), f"-I{tmp_path}", str(tmp_path / "main.cpp"), "-o", str(binary)],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(binary)], capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.strip() == "ok", run.stdout + run.stderr


def test_types_are_ordered_by_value_dependencies():
    code = _generate(SCHEMA)
    # Point and the variant's alternatives come before the struct holding them by value.
    assert code.index("struct Point {") < code.index("struct Scene {")
    assert code.index("struct Circle {") < code.index("using Shape = std::variant<Circle, Square>;") < code.index("struct Scene {")


def test_field_shapes():
    code = _generate(SCHEMA, cpp_namespace="demo")
    for expected in [
        "namespace demo {",
        "} // namespace demo",
        'std::string kind = "base";',
        "std::optional<std::string> nickname;",
        "std::optional<std::string> label;",
        "std::optional<Node> tree;",
        'std::vector<std::string> tags = {"a", "b"};',
        "Point origin = Point{.x = 1, .y = 2};",
        'nlohmann::json extra = nlohmann::json::parse(R"json({"a": [1, 2]})json");',
        'std::string content_type = "text/plain";',
        'j["content-type"] = v.content_type;',
        "std::vector<Node> children{};",
        'j["label"] = v.label ? nlohmann::json(*v.label) : nlohmann::json(nullptr);',
    ]:
        assert expected in code, f"missing: {expected}"


def test_by_value_cycle_is_a_clear_error():
    schema = {
        "definitions": {
            "A": {"type": "object", "properties": {"b": {"$ref": "#/definitions/B"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/definitions/A"}}},
        }
    }
    with pytest.raises(ValueError, match="cycle: A -> B -> A"):
        _generate(schema)


def test_type_override_and_extra_includes():
    schema = {
        "definitions": {
            "Clip": {
                "type": "object",
                "properties": {
                    "time": {"type": "number", "x-cpp-type": "montage::MediaTime"},
                    "maybe": {"type": "number", "x-cpp-type": "std::optional<montage::MediaTime>"},
                },
                "required": ["time"],
            }
        }
    }
    code = _generate(schema, cpp_additional_includes=['"MediaTime.hpp"'], cpp_json_include='"json.hpp"')
    assert "montage::MediaTime time{};" in code
    assert "std::optional<montage::MediaTime> maybe;" in code
    assert "v.maybe = it->get<montage::MediaTime>();" in code
    assert '#include "json.hpp"\n#include "MediaTime.hpp"' in code
