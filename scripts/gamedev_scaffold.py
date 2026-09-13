"""The cat-vs-mice starting point the agents are handed, and the spec they must satisfy.

This is deliberately NOT a working game. The engine plumbing is seeded (project settings,
scene, export preset) because hand-writing Godot boilerplate proves nothing about an
agent; the *rules* are stubs, and ``tests/run_tests.gd`` already encodes the full spec and
fails against them. An agent's work item is done when CI goes green -- which is a claim
the build can check, not a claim a model makes about itself.

Splitting rules out of the node is what makes that possible twice over:

* ``scripts/rules.gd`` is pure ``static func``s with no node state, so a headless
  ``SceneTree`` test can exercise real gameplay -- grid layout, collision, scoring --
  rather than merely proving the file parses.
* it also keeps each work item inside the codegen budget. ``adapters/mcp/codegen.py``
  runs one generation at ``max_tokens=4000`` and must emit *complete* files, so a
  250-line game in one shot truncates. One focused file per work item does not.

Art is optional by construction: the renderer draws from primitives and only swaps in
``assets/*.png`` if present, loaded at runtime. Scenes never reference them -- a .tscn
pointing at a not-yet-generated sprite fails to open and would break the build for
everyone.
"""

from __future__ import annotations

PROJECT_GODOT = """\
; Engine configuration. Godot 4.x project.
config_version=5

[application]
config/name="Cat vs Mice"
run/main_scene="res://main.tscn"

[display]
window/size/viewport_width=480
window/size/viewport_height=640
window/stretch/mode="canvas_items"
window/stretch/aspect="keep"

[rendering]
renderer/rendering_method="gl_compatibility"
"""

MAIN_TSCN = """\
[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/game.gd" id="1"]

[node name="Main" type="Node2D"]
script = ExtResource("1")
"""

# The stub the first work item replaces. Every function returns something type-correct
# but wrong, so the project always compiles and the failure shows up as failing tests
# rather than as a broken build.
RULES_STUB = """\
extends RefCounted
class_name Rules

# Gameplay rules for Cat vs Mice. Pure static functions, no node state, so
# tests/run_tests.gd can exercise them headlessly.
#
# STUB -- every function below returns a placeholder. tests/run_tests.gd encodes the
# real spec and currently fails. Implement these so it passes.

const PLAY_W := 480.0
const PLAY_H := 640.0

const CAT_W := 44.0
const CAT_H := 30.0
const CAT_Y := 588.0

const MOUSE_W := 34.0
const MOUSE_H := 24.0
const MOUSE_COLS := 8
const MOUSE_ROWS := 4
const MOUSE_GAP_X := 16.0
const MOUSE_GAP_Y := 20.0
const MOUSE_TOP := 90.0


static func grid_positions(cols: int, rows: int) -> Array:
    return []


static func rects_overlap(a: Rect2, b: Rect2) -> bool:
    return false


static func points_for(row: int) -> int:
    return 0


static func clamp_cat(x: float) -> float:
    return x


static func should_descend(positions: Array, direction: float) -> bool:
    return false


static func reached_pantry(positions: Array) -> bool:
    return false
"""

# The spec, as executable tests. Seeded complete and never written by an agent -- if the
# agent could edit the tests, "CI passed" would stop meaning anything.
RUN_TESTS_GD = """\
extends SceneTree

# The specification for scripts/rules.gd, executable. These run headless in CI; an
# agent's work item is done when they pass.

const Rules = preload("res://scripts/rules.gd")


func _initialize() -> void:
    var failures := 0

    # -- grid layout -------------------------------------------------------------
    var grid: Array = Rules.grid_positions(8, 4)
    failures += _check("grid has one position per mouse (8x4 = 32)", grid.size() == 32)
    if grid.size() == 32:
        failures += _check("grid is centred horizontally",
            absf((grid[0].x + grid[7].x + Rules.MOUSE_W) - Rules.PLAY_W) < 1.0)
        failures += _check("row 2 sits below row 1", grid[8].y > grid[0].y)
        failures += _check("columns step rightward", grid[1].x > grid[0].x)
        failures += _check("first row starts at MOUSE_TOP",
            absf(grid[0].y - Rules.MOUSE_TOP) < 0.01)
        failures += _check("column spacing is width + gap",
            absf((grid[1].x - grid[0].x) - (Rules.MOUSE_W + Rules.MOUSE_GAP_X)) < 0.01)
        failures += _check("row spacing is height + gap",
            absf((grid[8].y - grid[0].y) - (Rules.MOUSE_H + Rules.MOUSE_GAP_Y)) < 0.01)
        failures += _check("the whole block fits on screen",
            grid[0].x >= 0.0 and grid[7].x + Rules.MOUSE_W <= Rules.PLAY_W)

    # -- collision ---------------------------------------------------------------
    failures += _check("overlapping rects collide",
        Rules.rects_overlap(Rect2(0, 0, 10, 10), Rect2(5, 5, 10, 10)))
    failures += _check("disjoint rects do not collide",
        not Rules.rects_overlap(Rect2(0, 0, 10, 10), Rect2(50, 50, 10, 10)))
    failures += _check("touching-but-separate rects do not collide",
        not Rules.rects_overlap(Rect2(0, 0, 10, 10), Rect2(20, 0, 10, 10)))

    # -- scoring -----------------------------------------------------------------
    failures += _check("the back row scores more than the front row",
        Rules.points_for(0) > Rules.points_for(3))
    failures += _check("every row is worth something",
        Rules.points_for(3) > 0)

    # -- cat movement ------------------------------------------------------------
    failures += _check("cat cannot leave the left edge",
        Rules.clamp_cat(-100.0) >= 0.0)
    failures += _check("cat cannot leave the right edge",
        Rules.clamp_cat(9999.0) <= Rules.PLAY_W)
    failures += _check("cat is unchanged mid-screen",
        absf(Rules.clamp_cat(240.0) - 240.0) < 0.01)

    # -- descent ------------------------------------------------------------------
    failures += _check("descends when moving right into the wall",
        Rules.should_descend([Vector2(Rules.PLAY_W - Rules.MOUSE_W, 100)], 1.0))
    failures += _check("descends when moving left into the wall",
        Rules.should_descend([Vector2(0.0, 100)], -1.0))
    failures += _check("does not descend mid-screen",
        not Rules.should_descend([Vector2(200, 100)], 1.0))

    # -- losing -------------------------------------------------------------------
    failures += _check("a mouse at the pantry ends the game",
        Rules.reached_pantry([Vector2(100, Rules.CAT_Y)]))
    failures += _check("mice up high do not end the game",
        not Rules.reached_pantry([Vector2(100, 100)]))

    if failures == 0:
        print("ALL TESTS PASSED")
    else:
        print("FAILED: %d" % failures)
    quit(1 if failures > 0 else 0)


func _check(label: String, ok: bool) -> int:
    if not ok:
        print("  FAIL: %s" % label)
        return 1
    return 0
"""

# The node stub the second work item replaces: it compiles and renders a placeholder, so
# the project is always buildable and always deployable, even before any agent runs.
GAME_STUB = """\
extends Node2D

# The Cat vs Mice scene: input, simulation and rendering. Rules live in
# scripts/rules.gd and are covered by tests/run_tests.gd.
#
# STUB -- draws a placeholder and does nothing else. Implement the game loop.

const Rules = preload("res://scripts/rules.gd")


func _ready() -> void:
    print("Cat vs Mice (stub) booted")


func _draw() -> void:
    draw_rect(Rect2(0, 0, Rules.PLAY_W, Rules.PLAY_H), Color(0.06, 0.06, 0.10), true)
    var font: Font = ThemeDB.fallback_font
    draw_string(font, Vector2(60, Rules.PLAY_H * 0.5), "Cat vs Mice -- not implemented yet",
        HORIZONTAL_ALIGNMENT_LEFT, -1, 18, Color(0.92, 0.92, 0.96))
"""

GITIGNORE = ".godot/\nbuild/\ngame-web.tar.gz\n"

EXPORT_PRESETS = """\
[preset.0]

name="Web"
platform="Web"
runnable=true
advanced_options=false
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="build/web/index.html"
encryption_include_filters=""
encryption_exclude_filters=""
encrypt_pck=false
encrypt_directory=false

[preset.0.options]

custom_template/debug=""
custom_template/release=""
variant/extensions_support=false
vram_texture_compression/for_desktop=true
vram_texture_compression/for_mobile=false
html/export_icon=true
html/custom_html_shell=""
html/head_include=""
html/canvas_resize_policy=2
html/focus_canvas_on_start=true
html/experimental_virtual_keyboard=false
progressive_web_app/enabled=false
"""

README = """\
# Cat vs Mice

A Space Invaders variant: the cat defends the pantry, the mice descend in a block.
Arrows move, Space pounces.

## Layout

- `scripts/rules.gd` -- pure gameplay rules, `static func` only, no node state.
  Everything here is covered by `tests/run_tests.gd`.
- `scripts/game.gd` -- the scene: input, simulation, rendering. Calls into `Rules`.
- `tests/run_tests.gd` -- the executable specification. Run headless in CI.

## Conventions

- Godot 4.3, GDScript, 4-space indent.
- Type inference (`:=`) cannot infer from `max()`/`abs()` (they return Variant) or from
  elements of an untyped `Array`. Use `maxi`/`maxf`/`absf`, or annotate explicitly
  (`var x: float = ...`). This is the single most common build failure here.
- Art is optional. Draw from primitives; load `res://assets/*.png` at runtime with
  `ResourceLoader.exists()` and fall back to shapes. Never reference `assets/` from a
  `.tscn` -- a missing resource makes the scene fail to open.
- Do not edit `tests/run_tests.gd`. It is the spec.
"""

SCAFFOLD_FILES: dict[str, str] = {
    "project.godot": PROJECT_GODOT,
    "main.tscn": MAIN_TSCN,
    "scripts/rules.gd": RULES_STUB,
    "scripts/game.gd": GAME_STUB,
    "tests/run_tests.gd": RUN_TESTS_GD,
    ".gitignore": GITIGNORE,
    "export_presets.cfg": EXPORT_PRESETS,
    "README.md": README,
    "assets/.gitkeep": "",
}


WORK_ITEMS = [
    {
        "key": "cm-rules",
        "name": "Implement the gameplay rules",
        "description": (
            "Rewrite scripts/rules.gd so every test in tests/run_tests.gd passes. "
            "Keep the existing constants and the exact function signatures; replace only "
            "the placeholder bodies.\n"
            "\n"
            "grid_positions(cols, rows): return an Array of Vector2 top-left corners, one "
            "per mouse, in row-major order (row 0 first). Columns are MOUSE_W + "
            "MOUSE_GAP_X apart, rows are MOUSE_H + MOUSE_GAP_Y apart, the first row sits "
            "at MOUSE_TOP, and the block is centred horizontally in PLAY_W.\n"
            "rects_overlap(a, b): true when the two Rect2 overlap.\n"
            "points_for(row): points for killing a mouse in that row, 0-based from the "
            "back; the back row must be worth strictly more than the front, and every row "
            "worth more than zero.\n"
            "clamp_cat(x): keep the cat's centre on screen given CAT_W.\n"
            "should_descend(positions, direction): true when any mouse has reached the "
            "wall it is travelling toward (direction > 0 is rightward).\n"
            "reached_pantry(positions): true when any mouse has descended to CAT_Y.\n"
            "\n"
            "Do not edit tests/run_tests.gd. Godot 4.3: use maxi/maxf/absf and annotate "
            "types explicitly -- ':=' cannot infer from max()/abs() or from untyped Array "
            "elements."
        ),
    },
    {
        "key": "cm-loop",
        "name": "Implement the game loop and rendering",
        "description": (
            "Rewrite scripts/game.gd into the playable game, calling into Rules for all "
            "gameplay decisions. Keep 'const Rules = preload(\"res://scripts/rules.gd\")'.\n"
            "\n"
            "State: cat x position, an Array of mice (each a Dictionary with pos/alive/row), "
            "an Array of laser positions, movement direction, score, and a state string of "
            "'playing' | 'won' | 'lost'.\n"
            "_ready(): build the mouse grid from Rules.grid_positions(Rules.MOUSE_COLS, "
            'Rules.MOUSE_ROWS) and print("Cat vs Mice booted").\n'
            "_process(delta): move the cat with the built-in ui_left/ui_right actions; fire "
            "on ui_accept with a short cooldown; move lasers upward; move the mouse block "
            "sideways, and when Rules.should_descend says so reverse direction and step the "
            "whole block down; remove mice hit by a laser via Rules.rects_overlap and add "
            "Rules.points_for(row) to the score; win when no mice remain, lose when "
            "Rules.reached_pantry is true. Call queue_redraw().\n"
            "_draw(): dark background, the mice, the lasers, the cat, and the score. Draw "
            "everything from primitives (draw_rect / draw_circle / draw_colored_polygon / "
            "draw_string with ThemeDB.fallback_font). If res://assets/cat.png or "
            "res://assets/mouse.png exist -- check with ResourceLoader.exists() and load() "
            "at runtime -- draw those instead with draw_texture_rect.\n"
            "\n"
            "Do not edit tests/run_tests.gd or scripts/rules.gd. Godot 4.3: annotate types "
            "explicitly; ':=' cannot infer from max()/abs() or untyped Array elements."
        ),
    },
]
