import type { StateVarSpec, StateVarType } from "./dsl";

interface StateVarsEditorProps {
  state: Record<string, StateVarSpec>;
  onChange: (state: Record<string, StateVarSpec>) => void;
}

function defaultForType(type: StateVarType): StateVarSpec["default"] {
  switch (type) {
    case "integer":
    case "number":
      return 0;
    case "boolean":
      return false;
    case "string":
      return "";
  }
}

/** D1.2 subtask: the `state:` block editor -- session-scoped variables with a
 * type-checked default (schema.py's `StateVarSpec` rejects a bool default for an int var
 * and vice versa; this editor can't produce that mismatch since the default input's shape
 * follows the selected type directly). */
export function StateVarsEditor({ state, onChange }: StateVarsEditorProps) {
  const entries = Object.entries(state);

  function updateVar(name: string, next: StateVarSpec) {
    onChange({ ...state, [name]: next });
  }

  function renameVar(oldName: string, newName: string) {
    if (!newName || newName === oldName || newName in state) return;
    const { [oldName]: spec, ...rest } = state;
    onChange({ ...rest, [newName]: spec });
  }

  function removeVar(name: string) {
    const next = { ...state };
    delete next[name];
    onChange(next);
  }

  function addVar() {
    let n = 1;
    while (`var_${n}` in state) n += 1;
    onChange({ ...state, [`var_${n}`]: { type: "integer", default: 0 } });
  }

  return (
    <div className="flex flex-col gap-2">
      {entries.length === 0 && (
        <p className="text-sm text-muted-foreground">No session-state variables declared.</p>
      )}
      {entries.map(([name, spec]) => (
        <div key={name} className="grid grid-cols-[1fr_120px_1fr_auto] items-center gap-2">
          <input
            className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={name}
            onChange={(e) => renameVar(name, e.target.value)}
          />
          <select
            className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={spec.type}
            onChange={(e) => {
              const type = e.target.value as StateVarType;
              updateVar(name, { type, default: defaultForType(type) });
            }}
          >
            <option value="integer">integer</option>
            <option value="number">number</option>
            <option value="string">string</option>
            <option value="boolean">boolean</option>
          </select>
          {spec.type === "boolean" ? (
            <select
              className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={String(spec.default)}
              onChange={(e) => updateVar(name, { ...spec, default: e.target.value === "true" })}
            >
              <option value="false">false</option>
              <option value="true">true</option>
            </select>
          ) : (
            <input
              className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              type={spec.type === "string" ? "text" : "number"}
              value={String(spec.default)}
              onChange={(e) =>
                updateVar(name, {
                  ...spec,
                  default:
                    spec.type === "string" ? e.target.value : Number(e.target.value) || 0,
                })
              }
            />
          )}
          <button
            type="button"
            onClick={() => removeVar(name)}
            className="rounded-md border border-border px-2 py-1 text-xs"
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={addVar}
        className="self-start rounded-md border border-border px-3 py-1 text-sm hover:bg-accent disabled:opacity-50"
      >
        + Add state variable
      </button>
    </div>
  );
}
