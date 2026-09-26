import { useLabel } from "@/lib/vocabulary/useLabel";
import type { EntityView } from "./types";
import { FieldWidget } from "./FieldWidget";

const DEFAULT_GROUP_KEY = "__default__";
const DEFAULT_GROUP_LABEL = "sheet.default_group";

export interface SheetViewProps {
  entity: EntityView;
  /** `label_key` prefix for this entity's own fields, e.g. `"schema.character"` --
   * resolved through the vocabulary overlay like every other user-facing string
   * (agent-guide ); a pack decides the actual prefix, this component never
   * hardcodes one. */
  labelKeyPrefix: string;
  showHistory?: boolean;
}

/**
 * Auto-generated graphical entity representation: a pure function of
 * schema + data. The `ViewDef` (if the schema declares one) decides grouping/ordering/
 * tabs; a field the `ViewDef` doesn't mention still renders, in a synthesized default
 * group, never silently dropped. Zero domain knowledge anywhere in this component --
 * the RPG character, the enterprise project, and the swdev work item all render
 * through this exact same function.
 */
export function SheetView({ entity, labelKeyPrefix, showHistory }: SheetViewProps) {
  const t = useLabel();
  const fieldsByKey = Object.fromEntries(entity.fields.map((f) => [f.key, f]));
  const view = entity.views[0];

  const groups = view?.groups ?? [];
  const assignedKeys = new Set(groups.flatMap((g) => g.field_keys));
  const unassigned = entity.fields.map((f) => f.key).filter((k) => !assignedKeys.has(k));

  const renderedGroups = [
    ...groups.map((group) => ({ key: group.key, labelKey: group.label_key, fieldKeys: group.field_keys })),
    ...(unassigned.length > 0
      ? [{ key: DEFAULT_GROUP_KEY, labelKey: DEFAULT_GROUP_LABEL, fieldKeys: unassigned }]
      : []),
  ];

  return (
    <div className="flex flex-col gap-4" data-testid="sheet-view">
      {Object.keys(entity.fsm_states).length > 0 && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(entity.fsm_states).map(([machine, state]) => (
            <span
              key={machine}
              className="rounded-full border border-border bg-muted px-2 py-0.5 text-xs"
            >
              {machine}: {state}
            </span>
          ))}
        </div>
      )}

      {renderedGroups.map((group) => (
        <div key={group.key} className="flex flex-col gap-3 rounded-md border border-border p-3">
          <div className="text-sm font-semibold">{t(group.labelKey)}</div>
          <div className="grid grid-cols-2 gap-3">
            {group.fieldKeys
              .map((key) => fieldsByKey[key])
              .filter((field): field is NonNullable<typeof field> => field !== undefined)
              .map((field) => (
                <FieldWidget
                  key={field.key}
                  field={field}
                  fieldsByKey={fieldsByKey}
                  entityId={entity.id}
                  labelKeyPrefix={labelKeyPrefix}
                  showHistory={showHistory}
                />
              ))}
          </div>
        </div>
      ))}
    </div>
  );
}
