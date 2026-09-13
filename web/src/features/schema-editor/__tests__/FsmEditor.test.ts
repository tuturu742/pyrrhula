import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

const FSM_EDITOR_SOURCE = fs.readFileSync(
  path.resolve(__dirname, "../FsmEditor.tsx"),
  "utf-8",
);

describe("FSM editor reuses the process editor's React Flow canvas", () => {
  it("test_fsm_editor_reuses_process_editor_canvas_module", () => {
    // The shared module import must be present -- proves this is the same
    // `<ReactFlow>` shell the process editor's `ProcessCanvas` renders through, not a
    // parallel copy.
    expect(FSM_EDITOR_SOURCE).toMatch(
      /from ["']@\/features\/process-editor\/FlowCanvas["']/,
    );
    // And the FSM editor must not stand up its own `<ReactFlow>` tree/chrome -- that
    // would defeat the point of a shared module even if the import above were present.
    expect(FSM_EDITOR_SOURCE).not.toMatch(/<ReactFlow[\s>]/);
    expect(FSM_EDITOR_SOURCE).not.toMatch(/\bimport\s*\{[^}]*\bReactFlow\b/);
  });
});
