import { useMemo, useState } from "react";
import { Handle, MarkerType, Position, type Edge, type Node, type NodeChange, type NodeProps } from "@xyflow/react";
import { FlowCanvas } from "@/features/process-editor/FlowCanvas";
import type { EffectDef, SchemaValidationIssue, StateDef, StateMachineDef, TransitionDef } from "./types";
import { CelEditor } from "./CelEditor";

const NODE_COLUMN_WIDTH = 220;
const NODE_ROW_HEIGHT = 120;

interface StateNodeData extends Record<string, unknown> {
  stateKey: string;
  labelKey: string;
  isInitial: boolean;
  hasError: boolean;
  selected: boolean;
}

function StateNode({ data }: NodeProps & { data: StateNodeData }) {
  const borderColor = data.hasError ? "border-destructive" : data.selected ? "border-primary" : "border-border";
  return (
    <div className={`min-w-40 rounded-md border-2 bg-background px-3 py-2 shadow-sm ${borderColor}`}>
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center gap-1.5">
        {data.isInitial && <span title="Initial state" className="h-2 w-2 shrink-0 rounded-full bg-primary" />}
        <div className="truncate text-sm font-medium">{data.stateKey}</div>
      </div>
      <div className="truncate text-xs text-muted-foreground">{data.labelKey || "(no label_key)"}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { state: StateNode };

function autoPositions(machine: StateMachineDef): Record<string, { x: number; y: number }> {
  const positions: Record<string, { x: number; y: number }> = {};
  machine.states.forEach((s, i) => {
    positions[s.key] = { x: (i % 4) * NODE_COLUMN_WIDTH, y: Math.floor(i / 4) * NODE_ROW_HEIGHT };
  });
  return positions;
}

function issuesAt(issues: SchemaValidationIssue[], path: string): string[] {
  return issues.filter((i) => i.field_path === path || i.field_path.startsWith(`${path}.`)).map((i) => i.message);
}

function blankMachine(existing: StateMachineDef[]): StateMachineDef {
  let n = 1;
  while (existing.some((m) => m.key === `machine_${n}`)) n += 1;
  return {
    key: `machine_${n}`,
    initial: "start",
    states: [{ key: "start", label_key: "", tags: [], on_enter: [], on_exit: [] }],
    transitions: [],
  };
}

function blankEffect(): EffectDef {
  return { kind: "set_field", field: null, value: null, event: null, tool_key: null, tool_args: {}, machine: null, trigger: null };
}

/**
 * the FSM editor: states as nodes, transitions as labelled edges, on the exact same
 * React Flow shell the process editor's own phase canvas renders through
 * (`@/features/process-editor/FlowCanvas` -- one shared module, not a parallel
 * React Flow tree with its own copy-pasted chrome). Transitions/effects are edited as
 * lists below the canvas rather than via edge-click, mirroring `ProcessCanvas`/
 * `PhaseInspector`'s own split (gates are a list in the phase inspector, not
 * edge-editable on the canvas).
 */
export function FsmEditor({
  stateMachines,
  issues,
  onChange,
}: {
  stateMachines: StateMachineDef[];
  issues: SchemaValidationIssue[];
  onChange: (machines: StateMachineDef[]) => void;
}) {
  const [machineIndex, setMachineIndex] = useState(0);
  const [selectedStateKey, setSelectedStateKey] = useState<string | null>(null);
  const machine = stateMachines[machineIndex];
  const pathPrefix = `state_machines[${machineIndex}]`;

  function updateMachine(next: StateMachineDef) {
    onChange(stateMachines.map((m, i) => (i === machineIndex ? next : m)));
  }

  const errorsByState = useMemo(() => {
    const map = new Set<string>();
    if (!machine) return map;
    for (const issue of issues) {
      const match = new RegExp(`^${pathPrefix.replace(/[[\]]/g, "\\$&")}\\.states\\[(\\d+)\\]`).exec(issue.field_path);
      if (match) map.add(machine.states[Number(match[1])]?.key);
    }
    return map;
  }, [issues, machine, pathPrefix]);

  const positions = useMemo(() => (machine ? autoPositions(machine) : {}), [machine]);

  const nodes: Node[] = useMemo(() => {
    if (!machine) return [];
    return machine.states.map((s) => ({
      id: s.key,
      type: "state",
      position: positions[s.key] ?? { x: 0, y: 0 },
      data: {
        stateKey: s.key,
        labelKey: s.label_key,
        isInitial: s.key === machine.initial,
        hasError: errorsByState.has(s.key),
        selected: s.key === selectedStateKey,
      } satisfies StateNodeData,
    }));
  }, [machine, positions, errorsByState, selectedStateKey]);

  const edges: Edge[] = useMemo(() => {
    if (!machine) return [];
    return machine.transitions.map((t, i) => ({
      id: `${t.from}->${i}->${t.to}`,
      source: t.from === "*" ? machine.states[0]?.key : t.from,
      target: t.to,
      label: t.guard ? `${t.trigger} [guard]` : t.trigger,
      markerEnd: { type: MarkerType.ArrowClosed },
      style: t.guard ? { stroke: "#d97706", strokeDasharray: "4 3" } : { stroke: "#2563eb" },
    }));
  }, [machine]);

  function handleNodesChange(changes: NodeChange[]) {
    // Layout-only drag tracking (not persisted -- unlike the process editor, entity
    // schemas have no per-workspace localStorage layout key convention yet); positions
    // recompute from `autoPositions` on every render, so a drag just moves the node
    // until the next state-list edit.
    void changes;
  }

  if (!machine) {
    return (
      <button
        type="button"
        onClick={() => onChange([blankMachine(stateMachines)])}
        className="rounded-md border border-border px-3 py-2 text-sm"
      >
        + Add state machine
      </button>
    );
  }

  const selectedState = machine.states.find((s) => s.key === selectedStateKey) ?? null;

  function updateState(stateKey: string, next: Partial<StateDef>) {
    updateMachine({
      ...machine,
      states: machine.states.map((s) => (s.key === stateKey ? { ...s, ...next } : s)),
    });
  }

  function addState() {
    let n = 1;
    while (machine.states.some((s) => s.key === `state_${n}`)) n += 1;
    updateMachine({ ...machine, states: [...machine.states, { key: `state_${n}`, label_key: "", tags: [], on_enter: [], on_exit: [] }] });
  }

  function addTransition() {
    const first = machine.states[0]?.key ?? "";
    const next: TransitionDef = { from: first, to: first, trigger: "trigger", guard: null, effects: [] };
    updateMachine({ ...machine, transitions: [...machine.transitions, next] });
  }

  function updateTransition(index: number, next: Partial<TransitionDef>) {
    updateMachine({
      ...machine,
      transitions: machine.transitions.map((t, i) => (i === index ? { ...t, ...next } : t)),
    });
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <select
            className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={machineIndex}
            onChange={(e) => setMachineIndex(Number(e.target.value))}
          >
            {stateMachines.map((m, i) => (
              <option key={i} value={i}>
                {m.key}
              </option>
            ))}
          </select>
          <input
            className="rounded-md border border-input bg-transparent px-2 py-1 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={machine.key}
            onChange={(e) => updateMachine({ ...machine, key: e.target.value })}
          />
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={addState} className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50">
            + State
          </button>
          <button type="button" onClick={addTransition} className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50">
            + Transition
          </button>
          <button
            type="button"
            onClick={() => onChange([...stateMachines, blankMachine(stateMachines)])}
            className="rounded-md border border-border px-2 py-1 text-xs"
          >
            + Machine
          </button>
        </div>
      </div>

      <FlowCanvas
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={handleNodesChange}
        onNodeDragStop={() => undefined}
        onNodeClick={setSelectedStateKey}
      />

      {selectedState && (
        <div className="flex flex-col gap-2 rounded-md border border-border p-3">
          <div className="flex items-end gap-2">
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">key</span>
              <input
                className="rounded-md border border-input bg-transparent px-2 py-1 font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={selectedState.key}
                onChange={(e) => {
                  const newKey = e.target.value;
                  updateMachine({
                    ...machine,
                    initial: machine.initial === selectedState.key ? newKey : machine.initial,
                    states: machine.states.map((s) => (s.key === selectedState.key ? { ...s, key: newKey } : s)),
                    transitions: machine.transitions.map((t) => ({
                      ...t,
                      from: t.from === selectedState.key ? newKey : t.from,
                      to: t.to === selectedState.key ? newKey : t.to,
                    })),
                  });
                  setSelectedStateKey(newKey);
                }}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">label_key</span>
              <input
                className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={selectedState.label_key}
                onChange={(e) => updateState(selectedState.key, { label_key: e.target.value })}
              />
            </label>
            <button
              type="button"
              disabled={machine.initial === selectedState.key}
              onClick={() => updateMachine({ ...machine, initial: selectedState.key })}
              className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
            >
              {machine.initial === selectedState.key ? "Initial state" : "Set as initial"}
            </button>
          </div>
          {issuesAt(issues, `${pathPrefix}.states`).length > 0 && (
            <ul>
              {issuesAt(issues, `${pathPrefix}.states`).map((m, i) => (
                <li key={i} className="text-xs text-destructive">
                  {m}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-medium">Transitions</h3>
        {machine.transitions.map((t, i) => {
          const tPath = `${pathPrefix}.transitions[${i}]`;
          return (
            <div key={i} className="flex flex-col gap-2 rounded-md border border-border p-3">
              <div className="flex flex-wrap items-end gap-2">
                <label className="flex flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">from</span>
                  <select
                    className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={t.from}
                    onChange={(e) => updateTransition(i, { from: e.target.value })}
                  >
                    <option value="*">* (any state)</option>
                    {machine.states.map((s) => (
                      <option key={s.key} value={s.key}>
                        {s.key}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">to</span>
                  <select
                    className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={t.to}
                    onChange={(e) => updateTransition(i, { to: e.target.value })}
                  >
                    {machine.states.map((s) => (
                      <option key={s.key} value={s.key}>
                        {s.key}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">trigger</span>
                  <input
                    className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={t.trigger}
                    onChange={(e) => updateTransition(i, { trigger: e.target.value })}
                  />
                </label>
                <button
                  type="button"
                  onClick={() => updateMachine({ ...machine, transitions: machine.transitions.filter((_, idx) => idx !== i) })}
                  className="rounded-md border border-destructive px-2 py-1 text-xs text-destructive"
                >
                  Remove
                </button>
              </div>
              <CelEditor
                label="guard (optional)"
                value={t.guard ?? ""}
                onChange={(v) => updateTransition(i, { guard: v === "" ? null : v })}
                errors={issuesAt(issues, `${tPath}.guard`)}
              />
              {issuesAt(issues, `${tPath}.to`).concat(issuesAt(issues, `${tPath}.from`)).map((m, idx) => (
                <p key={idx} className="text-xs text-destructive">
                  {m}
                </p>
              ))}
              <EffectListEditor
                label="on-transition effects"
                effects={t.effects}
                issues={issues}
                pathPrefix={`${tPath}.effects`}
                allFieldKeys={[]}
                onChange={(effects) => updateTransition(i, { effects })}
                onAdd={() => updateTransition(i, { effects: [...t.effects, blankEffect()] })}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EffectListEditor({
  label,
  effects,
  issues,
  pathPrefix,
  onChange,
  onAdd,
}: {
  label: string;
  effects: EffectDef[];
  issues: SchemaValidationIssue[];
  pathPrefix: string;
  allFieldKeys: string[];
  onChange: (effects: EffectDef[]) => void;
  onAdd: () => void;
}) {
  function update(index: number, next: Partial<EffectDef>) {
    onChange(effects.map((e, i) => (i === index ? { ...e, ...next } : e)));
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">{label}</span>
        <button type="button" onClick={onAdd} className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-accent disabled:opacity-50">
          + Effect
        </button>
      </div>
      {effects.map((effect, i) => (
        <div key={i} className="flex flex-wrap items-end gap-2 rounded-md border border-border p-2">
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">kind</span>
            <select
              className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={effect.kind}
              onChange={(e) => update(i, { kind: e.target.value as EffectDef["kind"] })}
            >
              {(["set_field", "emit_event", "invoke_tool", "apply_modifier", "transition_other"] as const).map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </label>
          {(effect.kind === "set_field" || effect.kind === "apply_modifier") && (
            <>
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">field</span>
                <input
                  className="rounded-md border border-input bg-transparent px-2 py-1 font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={effect.field ?? ""}
                  onChange={(e) => update(i, { field: e.target.value })}
                />
              </label>
              <div className="w-48">
                <CelEditor
                  label="value (CEL)"
                  value={effect.value ?? ""}
                  onChange={(v) => update(i, { value: v })}
                  errors={issues
                    .filter((iss) => iss.field_path === `${pathPrefix}[${i}].value`)
                    .map((iss) => iss.message)}
                />
              </div>
            </>
          )}
          {effect.kind === "emit_event" && (
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">event</span>
              <input
                className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={effect.event ?? ""}
                onChange={(e) => update(i, { event: e.target.value })}
              />
            </label>
          )}
          {effect.kind === "invoke_tool" && (
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">tool_key</span>
              <input
                className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={effect.tool_key ?? ""}
                onChange={(e) => update(i, { tool_key: e.target.value })}
              />
            </label>
          )}
          {effect.kind === "transition_other" && (
            <>
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">machine</span>
                <input
                  className="rounded-md border border-input bg-transparent px-2 py-1 font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={effect.machine ?? ""}
                  onChange={(e) => update(i, { machine: e.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">trigger</span>
                <input
                  className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={effect.trigger ?? ""}
                  onChange={(e) => update(i, { trigger: e.target.value })}
                />
              </label>
            </>
          )}
          <button
            type="button"
            onClick={() => onChange(effects.filter((_, idx) => idx !== i))}
            className="rounded-md border border-destructive px-2 py-0.5 text-xs text-destructive"
          >
            Remove
          </button>
        </div>
      ))}
    </div>
  );
}
