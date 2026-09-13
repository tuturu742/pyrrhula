# Tidepool Studio — engineering conventions

## GDScript style
- snake_case for functions and variables; PascalCase for classes and scenes.
- Velocity-related variables carry the `vel_` prefix (vel_x, vel_jump).
- Every exported variable gets a one-line comment saying its unit.

## Scene and node rules
- One scene per gameplay concept; scene filename matches its root node name.
- Signals are connected in code, never in the editor, so diffs stay reviewable.

## Review checklist ("the Tidepool pass")
1. Does the game still export headless? (CI runs the web export.)
2. No magic numbers in physics code -- constants at the top of the file.
3. Player-facing strings go through the strings table, never inline.

## Commit style
`area: imperative summary` -- e.g. `player: clamp vel_x on wall contact`.
