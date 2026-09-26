import type {
  ConstraintDef,
  DerivedDef,
  FieldDef,
  FieldType,
  SchemaValidationIssue,
} from "./types";
import { TagPicker } from "./TagPicker";
import { CelEditor } from "./CelEditor";

const FIELD_TYPES: FieldType[] = ["string", "integer", "number", "boolean", "array"];
const ITEM_TYPES = ["string", "integer", "number"] as const;

interface FieldListEditorProps {
  fields: FieldDef[];
  derived: DerivedDef[];
  constraints: ConstraintDef[];
  issues: SchemaValidationIssue[];
  onChangeFields: (fields: FieldDef[]) => void;
  onChangeDerived: (derived: DerivedDef[]) => void;
  onChangeConstraints: (constraints: ConstraintDef[]) => void;
}

function issuesAt(issues: SchemaValidationIssue[], path: string): string[] {
  return issues.filter((i) => i.field_path === path || i.field_path.startsWith(`${path}.`)).map((i) => i.message);
}

function blankField(existing: FieldDef[]): FieldDef {
  let n = 1;
  while (existing.some((f) => f.key === `field_${n}`)) n += 1;
  return {
    key: `field_${n}`,
    type: "string",
    indexed: false,
    tags: [],
    tag_metadata: {},
    enum: null,
    minimum: null,
    maximum: null,
    scope_key: null,
  };
}

/**
 * the field list editor: one row per `FieldDef` (type, enum, range, `indexed`,
 * `scope_key`, plus a `TagPicker`), then derived fields and constraints as CEL
 * expressions -- the three sections `EntitySchemaDefinition` composes (`fields`/
 * `derived`/`constraints`). Every control is addressable by the exact `field_path`
 * `validate_schema_definition` emits, so a save-time validation failure highlights the
 * row that caused it, same discipline as the process editor's `PhaseInspector`.
 */
export function FieldListEditor({
  fields,
  derived,
  constraints,
  issues,
  onChangeFields,
  onChangeDerived,
  onChangeConstraints,
}: FieldListEditorProps) {
  const allFieldKeys = fields.map((f) => f.key);

  function updateField(index: number, next: Partial<FieldDef>) {
    onChangeFields(fields.map((f, i) => (i === index ? { ...f, ...next } : f)));
  }

  function removeField(index: number) {
    onChangeFields(fields.filter((_, i) => i !== index));
  }

  function updateDerived(index: number, next: Partial<DerivedDef>) {
    onChangeDerived(derived.map((d, i) => (i === index ? { ...d, ...next } : d)));
  }

  function updateConstraint(index: number, next: Partial<ConstraintDef>) {
    onChangeConstraints(constraints.map((c, i) => (i === index ? { ...c, ...next } : c)));
  }

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">Fields</h3>
          <button
            type="button"
            onClick={() => onChangeFields([...fields, blankField(fields)])}
            className="rounded-md border border-border px-2 py-1 text-xs"
          >
            + Add field
          </button>
        </div>
        {fields.map((field, i) => {
          const path = `fields[${i}]`;
          return (
            <div key={i} className="flex flex-col gap-2 rounded-md border border-border p-3">
              <div className="flex flex-wrap items-end gap-2">
                <label className="flex flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">key</span>
                  <input
                    className="rounded-md border border-input bg-transparent px-2 py-1 font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={field.key}
                    onChange={(e) => updateField(i, { key: e.target.value })}
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">type</span>
                  <select
                    className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={field.type}
                    onChange={(e) =>
                      updateField(i, {
                        type: e.target.value as FieldType,
                        items: e.target.value === "array" ? (field.items ?? "string") : null,
                      })
                    }
                  >
                    {FIELD_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </label>
                {field.type === "array" && (
                  <label className="flex flex-col gap-1 text-xs">
                    <span className="text-muted-foreground">items</span>
                    <select
                      className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                      value={field.items ?? "string"}
                      onChange={(e) => updateField(i, { items: e.target.value as (typeof ITEM_TYPES)[number] })}
                    >
                      {ITEM_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {t}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {(field.type === "integer" || field.type === "number") && (
                  <>
                    <label className="flex flex-col gap-1 text-xs">
                      <span className="text-muted-foreground">minimum</span>
                      <input
                        type="number"
                        className="w-20 rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                        value={field.minimum ?? ""}
                        onChange={(e) =>
                          updateField(i, { minimum: e.target.value === "" ? null : Number(e.target.value) })
                        }
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-xs">
                      <span className="text-muted-foreground">maximum</span>
                      <input
                        type="number"
                        className="w-20 rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                        value={field.maximum ?? ""}
                        onChange={(e) =>
                          updateField(i, { maximum: e.target.value === "" ? null : Number(e.target.value) })
                        }
                      />
                    </label>
                  </>
                )}
                <label className="flex flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">enum (comma-separated)</span>
                  <input
                    className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={(field.enum ?? []).join(",")}
                    onChange={(e) => {
                      const raw = e.target.value;
                      if (raw.trim() === "") {
                        updateField(i, { enum: null });
                        return;
                      }
                      const parts = raw.split(",").map((s) => s.trim());
                      updateField(i, {
                        enum: field.type === "integer" ? (parts.map(Number) as number[]) : parts,
                      });
                    }}
                  />
                </label>
                <label className="flex items-center gap-1 text-xs">
                  <input
                    type="checkbox"
                    checked={field.indexed}
                    onChange={(e) => updateField(i, { indexed: e.target.checked })}
                  />
                  indexed
                </label>
                <label className="flex flex-col gap-1 text-xs">
                  <span className="text-muted-foreground">scope_key (private fields)</span>
                  <input
                    className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    value={field.scope_key ?? ""}
                    onChange={(e) => updateField(i, { scope_key: e.target.value === "" ? null : e.target.value })}
                  />
                </label>
                <button
                  type="button"
                  onClick={() => removeField(i)}
                  className="rounded-md border border-destructive px-2 py-1 text-xs text-destructive"
                >
                  Remove
                </button>
              </div>
              <TagPicker
                fieldPath={path}
                tags={field.tags}
                tagMetadata={field.tag_metadata}
                fieldType={field.type}
                allFieldKeys={allFieldKeys.filter((k) => k !== field.key)}
                issues={issues}
                onChange={(tags, tagMetadata) => updateField(i, { tags, tag_metadata: tagMetadata })}
              />
            </div>
          );
        })}
      </section>

      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">Derived fields</h3>
          <button
            type="button"
            onClick={() =>
              onChangeDerived([...derived, { key: `derived_${derived.length + 1}`, type: "number", expression: "" }])
            }
            className="rounded-md border border-border px-2 py-1 text-xs"
          >
            + Add derived field
          </button>
        </div>
        {derived.map((d, i) => (
          <div key={i} className="flex flex-col gap-2 rounded-md border border-border p-3">
            <div className="flex items-end gap-2">
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">key</span>
                <input
                  className="rounded-md border border-input bg-transparent px-2 py-1 font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={d.key}
                  onChange={(e) => updateDerived(i, { key: e.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1 text-xs">
                <span className="text-muted-foreground">type</span>
                <select
                  className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={d.type}
                  onChange={(e) => updateDerived(i, { type: e.target.value as DerivedDef["type"] })}
                >
                  {(["string", "integer", "number", "boolean"] as const).map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={() => onChangeDerived(derived.filter((_, idx) => idx !== i))}
                className="rounded-md border border-destructive px-2 py-1 text-xs text-destructive"
              >
                Remove
              </button>
            </div>
            <CelEditor
              label="expression"
              value={d.expression}
              onChange={(v) => updateDerived(i, { expression: v })}
              errors={issuesAt(issues, `derived[${i}].expression`)}
            />
          </div>
        ))}
      </section>

      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">Constraints</h3>
          <button
            type="button"
            onClick={() => onChangeConstraints([...constraints, { expression: "", message: null }])}
            className="rounded-md border border-border px-2 py-1 text-xs"
          >
            + Add constraint
          </button>
        </div>
        {constraints.map((c, i) => (
          <div key={i} className="flex flex-col gap-2 rounded-md border border-border p-3">
            <CelEditor
              label="expression"
              value={c.expression}
              onChange={(v) => updateConstraint(i, { expression: v })}
              errors={issuesAt(issues, `constraints[${i}].expression`)}
            />
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">message (optional)</span>
              <input
                className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={c.message ?? ""}
                onChange={(e) => updateConstraint(i, { message: e.target.value === "" ? null : e.target.value })}
              />
            </label>
            <button
              type="button"
              onClick={() => onChangeConstraints(constraints.filter((_, idx) => idx !== i))}
              className="self-start rounded-md border border-destructive px-2 py-1 text-xs text-destructive"
            >
              Remove
            </button>
          </div>
        ))}
      </section>
    </div>
  );
}
