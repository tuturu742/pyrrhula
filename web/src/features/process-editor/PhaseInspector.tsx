import type {
  ActorMode,
  ActorOrder,
  ActorSpec,
  AgentRole,
  AnyOfToken,
  AwaitSpec,
  BudgetSpec,
  EffectSpec,
  GateSpec,
  PhaseSpec,
  ProcessDefinitionDSL,
  SecretsVisibility,
  VisibilitySpec,
} from "./dsl";
import { KNOWN_AGENT_ROLES, KNOWN_ANY_OF_TOKENS } from "./dsl";
import type { FieldError } from "./ProcessCanvas";

interface PhaseInspectorProps {
  definition: ProcessDefinitionDSL;
  phaseKey: string;
  fieldErrors: FieldError[];
  onChangePhase: (phaseKey: string, phase: PhaseSpec) => void;
  onRenamePhase: (oldKey: string, newKey: string) => void;
  onDeletePhase: (phaseKey: string) => void;
  onSetInitial: (phaseKey: string) => void;
}

function issuesFor(fieldErrors: FieldError[], prefix: string): string[] {
  return fieldErrors
    .filter((e) => e.fieldPath === prefix || e.fieldPath.startsWith(`${prefix}.`) || e.fieldPath.startsWith(`${prefix}[`))
    .map((e) => e.message);
}

/**
 * D1.2's phase inspector: every field `schema.py`'s `PhaseSpec` accepts, addressable by
 * exactly the `field_path` strings `validator.py` emits (`phases.<key>...`), so a save-
 * time validation failure highlights the specific control that caused it rather than just
 * the phase as a whole.
 */
export function PhaseInspector({
  definition,
  phaseKey,
  fieldErrors,
  onChangePhase,
  onRenamePhase,
  onDeletePhase,
  onSetInitial,
}: PhaseInspectorProps) {
  const phase = definition.phases[phaseKey];
  if (!phase) return null;
  const pathPrefix = `phases.${phaseKey}`;
  const phaseErrors = issuesFor(fieldErrors, pathPrefix);
  const otherPhaseKeys = Object.keys(definition.phases).filter((k) => k !== phaseKey);

  function set(next: Partial<PhaseSpec>) {
    onChangePhase(phaseKey, { ...phase, ...next });
  }

  return (
    <div className="flex flex-col gap-4 rounded-md border border-border p-4">
      <div className="flex items-center justify-between gap-2">
        <input
          className="rounded-md border border-input bg-transparent px-2 py-1 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={phaseKey}
          onChange={(e) => onRenamePhase(phaseKey, e.target.value)}
        />
        <div className="flex gap-2">
          <button
            type="button"
            disabled={definition.initial_phase === phaseKey}
            onClick={() => onSetInitial(phaseKey)}
            className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
          >
            {definition.initial_phase === phaseKey ? "Initial phase" : "Set as initial"}
          </button>
          <button
            type="button"
            onClick={() => onDeletePhase(phaseKey)}
            className="rounded-md border border-destructive px-2 py-1 text-xs text-destructive"
          >
            Delete phase
          </button>
        </div>
      </div>

      {phaseErrors.length > 0 && (
        <ul className="rounded-md border border-destructive bg-destructive/10 p-2 text-xs text-destructive">
          {phaseErrors.map((m, i) => (
            <li key={i}>{m}</li>
          ))}
        </ul>
      )}

      <Field label="label_key" error={issuesFor(fieldErrors, `${pathPrefix}.label_key`)[0]}>
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={phase.label_key}
          onChange={(e) => set({ label_key: e.target.value })}
        />
      </Field>

      <Field label="Phase instructions (injected into every acting persona's turn)">
        <textarea
          className="min-h-16 w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={phase.prompt ?? ""}
          onChange={(e) => set({ prompt: e.target.value })}
          placeholder="What is this phase for, and what shape should the output take? e.g. Output markdown with '## Assessment' and '## Proposal'; no filler."
        />
      </Field>

      <ActorsEditor
        actors={phase.actors}
        fieldErrors={fieldErrors}
        pathPrefix={pathPrefix}
        onChange={(actors) => set({ actors })}
      />

      <VisibilityEditor
        visibility={phase.visibility}
        fieldErrors={fieldErrors}
        pathPrefix={`${pathPrefix}.visibility`}
        onChange={(visibility) => set({ visibility })}
      />

      <BudgetEditor
        budget={phase.budget}
        fieldErrors={fieldErrors}
        pathPrefix={`${pathPrefix}.budget`}
        onChange={(budget) => set({ budget })}
      />

      <TransitionEditor
        phase={phase}
        otherPhaseKeys={otherPhaseKeys}
        fieldErrors={fieldErrors}
        pathPrefix={pathPrefix}
        onChange={set}
      />

      <EffectsEditor
        effects={phase.effects ?? []}
        fieldErrors={fieldErrors}
        pathPrefix={pathPrefix}
        onChange={(effects) => set({ effects })}
      />

      <AwaitEditor
        awaitSpec={phase.await}
        otherPhaseKeys={otherPhaseKeys}
        fieldErrors={fieldErrors}
        pathPrefix={`${pathPrefix}.await`}
        onChange={(awaitSpec) => set({ await: awaitSpec })}
      />

      <Field label="Flags (comma-separated)" hint='e.g. "mechanical", "requires_citation"'>
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={(phase.flags ?? []).join(", ")}
          onChange={(e) =>
            set({ flags: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
          }
        />
      </Field>

      <Field label="Tools (comma-separated)" hint='e.g. "randomizer", "stat_calculator"'>
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={(phase.tools ?? []).join(", ")}
          onChange={(e) =>
            set({ tools: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
          }
        />
      </Field>
    </div>
  );
}

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      {error && <span className="text-xs text-destructive">{error}</span>}
    </label>
  );
}

function ActorsEditor({
  actors,
  fieldErrors,
  pathPrefix,
  onChange,
}: {
  actors: ActorSpec[];
  fieldErrors: FieldError[];
  pathPrefix: string;
  onChange: (actors: ActorSpec[]) => void;
}) {
  function updateActor(i: number, next: ActorSpec) {
    onChange(actors.map((a, idx) => (idx === i ? next : a)));
  }
  function removeActor(i: number) {
    onChange(actors.filter((_, idx) => idx !== i));
  }
  function addActor() {
    onChange([...actors, { mode: "generate", order: "declared", persona_type: "facilitator" }]);
  }

  return (
    <fieldset className="flex flex-col gap-3 rounded-md border border-dashed border-border p-3">
      <legend className="px-1 text-sm font-medium text-muted-foreground">Actors</legend>
      {actors.length === 0 && (
        <p className="text-xs text-muted-foreground">No actors -- gates/on_complete fire immediately.</p>
      )}
      {actors.map((actor, i) => {
        const selectorKind: "persona_type" | "any_of" | "human_participant" | "none" =
          actor.persona_type !== undefined
            ? "persona_type"
            : actor.any_of !== undefined
              ? "any_of"
              : actor.human_participant !== undefined
                ? "human_participant"
                : "none";
        const actorErrors = issuesFor(fieldErrors, `${pathPrefix}.actors[${i}]`);
        return (
          <div key={i} className="flex flex-col gap-2 rounded-md border border-border p-2">
            <div className="grid grid-cols-2 gap-2">
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">Selector</span>
                <select
                  className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={selectorKind}
                  onChange={(e) => {
                    const kind = e.target.value;
                    const base: ActorSpec = { mode: actor.mode, order: actor.order, max_turns: actor.max_turns };
                    if (kind === "persona_type") updateActor(i, { ...base, persona_type: "facilitator" });
                    else if (kind === "any_of") updateActor(i, { ...base, any_of: ["human_participant"] });
                    else if (kind === "human_participant") updateActor(i, { ...base, human_participant: "all" });
                    else updateActor(i, { ...base, order: "initiative" });
                  }}
                >
                  <option value="persona_type">persona_type</option>
                  <option value="any_of">any_of</option>
                  <option value="human_participant">human_participant</option>
                  <option value="none">(implicit -- requires order: initiative)</option>
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">Mode</span>
                <select
                  className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={actor.mode}
                  onChange={(e) => updateActor(i, { ...actor, mode: e.target.value as ActorMode })}
                >
                  <option value="free">free</option>
                  <option value="generate">generate</option>
                  <option value="generate_as">generate_as</option>
                </select>
              </label>
            </div>

            {selectorKind === "persona_type" && (
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">persona_type</span>
                <select
                  className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={actor.persona_type}
                  onChange={(e) => updateActor(i, { ...actor, persona_type: e.target.value as AgentRole })}
                >
                  {KNOWN_AGENT_ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {selectorKind === "any_of" && (
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">any_of (comma-separated)</span>
                <input
                  className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={(actor.any_of ?? []).join(", ")}
                  onChange={(e) =>
                    updateActor(i, {
                      ...actor,
                      any_of: e.target.value
                        .split(",")
                        .map((s) => s.trim())
                        .filter(Boolean) as AnyOfToken[],
                    })
                  }
                />
                <span className="text-[10px] text-muted-foreground">
                  known: {KNOWN_ANY_OF_TOKENS.join(", ")}
                </span>
              </label>
            )}

            <div className="grid grid-cols-2 gap-2">
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">Order</span>
                <select
                  className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={actor.order ?? "declared"}
                  onChange={(e) => updateActor(i, { ...actor, order: e.target.value as ActorOrder })}
                >
                  <option value="declared">declared</option>
                  <option value="initiative">initiative</option>
                  <option value="free">free</option>
                </select>
              </label>
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">Max turns</span>
                <input
                  type="number"
                  className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={actor.max_turns ?? ""}
                  onChange={(e) =>
                    updateActor(i, {
                      ...actor,
                      max_turns: e.target.value === "" ? undefined : Number(e.target.value),
                    })
                  }
                />
              </label>
            </div>

            {actor.order === "initiative" && (
              <Field label="from" hint='e.g. entity_field("initiative")'>
                <input
                  className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={actor.from ?? ""}
                  onChange={(e) => updateActor(i, { ...actor, from: e.target.value })}
                />
              </Field>
            )}

            {actorErrors.length > 0 && (
              <ul className="text-xs text-destructive">
                {actorErrors.map((m, idx) => (
                  <li key={idx}>{m}</li>
                ))}
              </ul>
            )}

            <button
              type="button"
              onClick={() => removeActor(i)}
              className="self-start rounded-md border border-border px-2 py-1 text-xs"
            >
              Remove actor
            </button>
          </div>
        );
      })}
      <button
        type="button"
        onClick={addActor}
        className="self-start rounded-md border border-border px-3 py-1 text-sm hover:bg-accent disabled:opacity-50"
      >
        + Add actor
      </button>
    </fieldset>
  );
}

function VisibilityEditor({
  visibility,
  fieldErrors,
  pathPrefix,
  onChange,
}: {
  visibility: VisibilitySpec;
  fieldErrors: FieldError[];
  pathPrefix: string;
  onChange: (v: VisibilitySpec) => void;
}) {
  return (
    <fieldset className="flex flex-col gap-3 rounded-md border border-dashed border-border p-3">
      <legend className="px-1 text-sm font-medium text-muted-foreground">
        Visibility (mandatory -- who sees what)
      </legend>
      <Field label="Knowledge classes (comma-separated)" error={issuesFor(fieldErrors, `${pathPrefix}.knowledge_classes`)[0]}>
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={visibility.knowledge_classes.join(", ")}
          onChange={(e) =>
            onChange({
              ...visibility,
              knowledge_classes: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
            })
          }
        />
      </Field>
      <Field label="Scopes (comma-separated)" error={issuesFor(fieldErrors, `${pathPrefix}.scopes`)[0]}>
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={visibility.scopes.join(", ")}
          onChange={(e) =>
            onChange({ ...visibility, scopes: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })
          }
        />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Entity fields</span>
          <select
            className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={visibility.entity_fields === "all" ? "all" : "list"}
            onChange={(e) => onChange({ ...visibility, entity_fields: e.target.value === "all" ? "all" : [] })}
          >
            <option value="all">all</option>
            <option value="list">specific fields</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Secrets</span>
          <select
            className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={visibility.secrets}
            onChange={(e) => onChange({ ...visibility, secrets: e.target.value as SecretsVisibility })}
          >
            <option value="held_by_actor">held_by_actor</option>
            <option value="none">none</option>
          </select>
        </label>
      </div>
      {visibility.entity_fields !== "all" && (
        <Field label="Entity fields (comma-separated)">
          <input
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={(visibility.entity_fields as string[]).join(", ")}
            onChange={(e) =>
              onChange({
                ...visibility,
                entity_fields: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
              })
            }
          />
        </Field>
      )}
    </fieldset>
  );
}

function BudgetEditor({
  budget,
  fieldErrors,
  pathPrefix,
  onChange,
}: {
  budget: BudgetSpec | undefined;
  fieldErrors: FieldError[];
  pathPrefix: string;
  onChange: (b: BudgetSpec | undefined) => void;
}) {
  const ratioError = issuesFor(fieldErrors, `${pathPrefix}.ratio`)[0];
  const sum = budget ? Object.values(budget.ratio).reduce((a, b) => a + b, 0) : 0;
  const sumOk = Math.abs(sum - 1.0) <= 0.02;

  if (!budget) {
    return (
      <fieldset className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
        <legend className="px-1 text-sm font-medium text-muted-foreground">Budget</legend>
        <p className="text-xs text-muted-foreground">
          No budget -- valid only for a pure-await/no-generation phase.
        </p>
        <button
          type="button"
          onClick={() => onChange({ ratio: { rules: 0.5, lore: 0.5 }, max_tokens: 4000, spill: "proportional" })}
          className="self-start rounded-md border border-border px-3 py-1 text-sm"
        >
          + Add budget
        </button>
      </fieldset>
    );
  }

  function setRatio(className: string, value: number) {
    onChange({ ...budget!, ratio: { ...budget!.ratio, [className]: value } });
  }
  function renameClass(oldName: string, newName: string) {
    if (!newName || newName === oldName || newName in budget!.ratio) return;
    const { [oldName]: v, ...rest } = budget!.ratio;
    onChange({ ...budget!, ratio: { ...rest, [newName]: v } });
  }
  function removeClass(name: string) {
    const nextRatio = { ...budget!.ratio };
    delete nextRatio[name];
    onChange({ ...budget!, ratio: nextRatio });
  }
  function addClass() {
    let n = 1;
    while (`class_${n}` in budget!.ratio) n += 1;
    onChange({ ...budget!, ratio: { ...budget!.ratio, [`class_${n}`]: 0 } });
  }

  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
      <div className="flex items-center justify-between">
        <legend className="px-1 text-sm font-medium text-muted-foreground">Budget</legend>
        <button
          type="button"
          onClick={() => onChange(undefined)}
          className="rounded-md border border-border px-2 py-1 text-xs"
        >
          Remove budget
        </button>
      </div>
      {Object.entries(budget.ratio).map(([className, value]) => (
        <div key={className} className="grid grid-cols-[1fr_2fr_60px_auto] items-center gap-2">
          <input
            className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={className}
            onChange={(e) => renameClass(className, e.target.value)}
          />
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={value}
            onChange={(e) => setRatio(className, Number(e.target.value))}
          />
          <span className="text-xs text-muted-foreground">{value.toFixed(2)}</span>
          <button
            type="button"
            onClick={() => removeClass(className)}
            className="rounded-md border border-border px-2 py-1 text-xs"
          >
            Remove
          </button>
        </div>
      ))}
      <button type="button" onClick={addClass} className="self-start rounded-md border border-border px-3 py-1 text-sm hover:bg-accent disabled:opacity-50">
        + Add class
      </button>
      <p className={`text-xs ${sumOk ? "text-muted-foreground" : "text-destructive"}`}>
        Sum: {sum.toFixed(3)} {sumOk ? "" : "-- ratios must sum to ~1.0"}
      </p>
      {ratioError && <p className="text-xs text-destructive">{ratioError}</p>}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Max tokens">
          <input
            type="number"
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={budget.max_tokens}
            onChange={(e) => onChange({ ...budget, max_tokens: Number(e.target.value) })}
          />
        </Field>
        <Field label="Spill">
          <select
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={budget.spill ?? "proportional"}
            onChange={(e) => onChange({ ...budget, spill: e.target.value as BudgetSpec["spill"] })}
          >
            <option value="proportional">proportional</option>
            <option value="none">none</option>
          </select>
        </Field>
      </div>
    </fieldset>
  );
}

function TransitionEditor({
  phase,
  otherPhaseKeys,
  fieldErrors,
  pathPrefix,
  onChange,
}: {
  phase: PhaseSpec;
  otherPhaseKeys: string[];
  fieldErrors: FieldError[];
  pathPrefix: string;
  onChange: (next: Partial<PhaseSpec>) => void;
}) {
  const gates = phase.gates ?? [];
  const mechanism: "on_complete" | "gates" = gates.length > 0 ? "gates" : "on_complete";

  function updateGate(i: number, next: GateSpec) {
    onChange({ gates: gates.map((g, idx) => (idx === i ? next : g)) });
  }
  function removeGate(i: number) {
    onChange({ gates: gates.filter((_, idx) => idx !== i) });
  }
  function addGate() {
    onChange({ gates: [...gates, { on: "actor_declares_action", to: otherPhaseKeys[0] ?? "" }] });
  }

  return (
    <fieldset className="flex flex-col gap-3 rounded-md border border-dashed border-border p-3">
      <legend className="px-1 text-sm font-medium text-muted-foreground">
        Transition (one mechanism per phase)
      </legend>
      <div className="flex gap-3 text-sm">
        <label className="flex items-center gap-1">
          <input
            type="radio"
            checked={mechanism === "on_complete"}
            onChange={() => onChange({ gates: [], on_complete: phase.on_complete ?? otherPhaseKeys[0] })}
          />
          on_complete
        </label>
        <label className="flex items-center gap-1">
          <input
            type="radio"
            checked={mechanism === "gates"}
            onChange={() =>
              onChange({
                on_complete: undefined,
                gates: gates.length > 0 ? gates : [{ on: "actor_declares_action", to: otherPhaseKeys[0] ?? "" }],
              })
            }
          />
          gates
        </label>
      </div>

      {mechanism === "on_complete" && (
        <Field label="on_complete -> phase" error={issuesFor(fieldErrors, `${pathPrefix}.on_complete`)[0]}>
          <select
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={phase.on_complete ?? ""}
            onChange={(e) => onChange({ on_complete: e.target.value })}
          >
            <option value="">(select target phase)</option>
            {otherPhaseKeys.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </Field>
      )}

      {mechanism === "gates" && (
        <div className="flex flex-col gap-2">
          {gates.map((gate, i) => {
            const kind = gate.on ? "on" : gate.when ? "when" : "else";
            const gateErrors = issuesFor(fieldErrors, `${pathPrefix}.gates[${i}]`);
            return (
              <div key={i} className="flex flex-col gap-2 rounded-md border border-border p-2">
                <div className="grid grid-cols-3 gap-2">
                  <select
                    className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={kind}
                    onChange={(e) => {
                      const k = e.target.value;
                      if (k === "on") updateGate(i, { on: "actor_declares_action", to: gate.to });
                      else if (k === "when") updateGate(i, { when: "true", to: gate.to });
                      else updateGate(i, { else: true, to: gate.to });
                    }}
                  >
                    <option value="on">on</option>
                    <option value="when">when</option>
                    <option value="else">else</option>
                  </select>
                  {kind === "on" && (
                    <input
                      className="col-span-1 rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                      value={gate.on}
                      placeholder="actor_declares_action or timeout(24h)"
                      onChange={(e) => updateGate(i, { on: e.target.value, to: gate.to })}
                    />
                  )}
                  {kind === "when" && (
                    <input
                      className="col-span-1 rounded-md border border-input bg-transparent px-2 py-1 text-sm font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                      value={gate.when}
                      placeholder="CEL expression, e.g. state.round > 3"
                      onChange={(e) => updateGate(i, { when: e.target.value, to: gate.to })}
                    />
                  )}
                  <select
                    className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={gate.to}
                    onChange={(e) => updateGate(i, { ...gate, to: e.target.value })}
                  >
                    <option value="">(select target phase)</option>
                    {otherPhaseKeys.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                </div>
                {gateErrors.length > 0 && (
                  <ul className="text-xs text-destructive">
                    {gateErrors.map((m, idx) => (
                      <li key={idx}>{m}</li>
                    ))}
                  </ul>
                )}
                <button
                  type="button"
                  onClick={() => removeGate(i)}
                  className="self-start rounded-md border border-border px-2 py-1 text-xs"
                >
                  Remove gate
                </button>
              </div>
            );
          })}
          <button type="button" onClick={addGate} className="self-start rounded-md border border-border px-3 py-1 text-sm hover:bg-accent disabled:opacity-50">
            + Add gate
          </button>
          <p className="text-xs text-muted-foreground">
            Evaluated in order, first match wins -- an "else" gate must be last.
          </p>
        </div>
      )}
    </fieldset>
  );
}

function EffectsEditor({
  effects,
  fieldErrors,
  pathPrefix,
  onChange,
}: {
  effects: EffectSpec[];
  fieldErrors: FieldError[];
  pathPrefix: string;
  onChange: (effects: EffectSpec[]) => void;
}) {
  function updateEffect(i: number, next: EffectSpec) {
    onChange(effects.map((e, idx) => (idx === i ? next : e)));
  }
  function removeEffect(i: number) {
    onChange(effects.filter((_, idx) => idx !== i));
  }
  function addEffect() {
    onChange([...effects, { set: "", to: "" }]);
  }

  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
      <legend className="px-1 text-sm font-medium text-muted-foreground">
        Effects (applied atomically with the transition)
      </legend>
      {effects.map((effect, i) => {
        const err = issuesFor(fieldErrors, `${pathPrefix}.effects[${i}]`)[0];
        return (
          <div key={i} className="flex flex-col gap-1">
            <div className="grid grid-cols-[1fr_auto_2fr_auto] items-center gap-2">
              <input
                className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={effect.set}
                placeholder="state.round"
                onChange={(e) => updateEffect(i, { ...effect, set: e.target.value })}
              />
              <span className="text-xs text-muted-foreground">=</span>
              <input
                className="rounded-md border border-input bg-transparent px-2 py-1 text-sm font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={effect.to}
                placeholder="state.round + 1"
                onChange={(e) => updateEffect(i, { ...effect, to: e.target.value })}
              />
              <button
                type="button"
                onClick={() => removeEffect(i)}
                className="rounded-md border border-border px-2 py-1 text-xs"
              >
                Remove
              </button>
            </div>
            {err && <p className="text-xs text-destructive">{err}</p>}
          </div>
        );
      })}
      <button type="button" onClick={addEffect} className="self-start rounded-md border border-border px-3 py-1 text-sm hover:bg-accent disabled:opacity-50">
        + Add effect
      </button>
    </fieldset>
  );
}

function AwaitEditor({
  awaitSpec,
  otherPhaseKeys,
  fieldErrors,
  pathPrefix,
  onChange,
}: {
  awaitSpec: AwaitSpec | undefined;
  otherPhaseKeys: string[];
  fieldErrors: FieldError[];
  pathPrefix: string;
  onChange: (a: AwaitSpec | undefined) => void;
}) {
  if (!awaitSpec) {
    return (
      <fieldset className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
        <legend className="px-1 text-sm font-medium text-muted-foreground">Await</legend>
        <button
          type="button"
          onClick={() => onChange({ type: "human_input", timeout: "24h", on_timeout: otherPhaseKeys[0] ?? "" })}
          className="self-start rounded-md border border-border px-3 py-1 text-sm"
        >
          + Add await (suspend for human input)
        </button>
      </fieldset>
    );
  }
  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
      <div className="flex items-center justify-between">
        <legend className="px-1 text-sm font-medium text-muted-foreground">Await</legend>
        <button
          type="button"
          onClick={() => onChange(undefined)}
          className="rounded-md border border-border px-2 py-1 text-xs"
        >
          Remove await
        </button>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Timeout" hint='e.g. "72h"' error={issuesFor(fieldErrors, `${pathPrefix}.timeout`)[0]}>
          <input
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={awaitSpec.timeout}
            onChange={(e) => onChange({ ...awaitSpec, timeout: e.target.value })}
          />
        </Field>
        <Field label="on_timeout -> phase" error={issuesFor(fieldErrors, `${pathPrefix}.on_timeout`)[0]}>
          <select
            className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={awaitSpec.on_timeout}
            onChange={(e) => onChange({ ...awaitSpec, on_timeout: e.target.value })}
          >
            <option value="">(select target phase)</option>
            {otherPhaseKeys.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </Field>
      </div>
    </fieldset>
  );
}
