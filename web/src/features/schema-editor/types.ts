/** Wire shapes for F3.11's schema authoring endpoints -- a hand-written mirror of
 * `packages/core/entities/schema.py` / `fsm.py` / `views.py`'s Pydantic models, same
 * rationale as `entity-sheets/types.ts`: simple, direct types for the editor components
 * without every one of them reaching into the generated `schema.ts`. */

export type FieldType = "string" | "integer" | "number" | "boolean" | "array";

export interface FieldDef {
  key: string;
  type: FieldType;
  items?: "string" | "integer" | "number" | null;
  enum?: string[] | number[] | null;
  minimum?: number | null;
  maximum?: number | null;
  indexed: boolean;
  tags: string[];
  tag_metadata: Record<string, unknown>;
  scope_key?: string | null;
}

export interface DerivedDef {
  key: string;
  type: "string" | "integer" | "number" | "boolean";
  expression: string;
}

export interface ConstraintDef {
  expression: string;
  message?: string | null;
}

export type EffectKind = "set_field" | "emit_event" | "invoke_tool" | "apply_modifier" | "transition_other";

export interface EffectDef {
  kind: EffectKind;
  field?: string | null;
  value?: string | null;
  event?: string | null;
  tool_key?: string | null;
  tool_args: Record<string, unknown>;
  machine?: string | null;
  trigger?: string | null;
}

export interface StateDef {
  key: string;
  label_key: string;
  tags: string[];
  on_enter: EffectDef[];
  on_exit: EffectDef[];
}

/** Wire key is `from` (Python's `TransitionDef.from_state` alias) -- kept as `from`
 * here too so a definition round-trips through the API with no field renaming. */
export interface TransitionDef {
  from: string;
  to: string;
  trigger: string;
  guard?: string | null;
  effects: EffectDef[];
}

export interface StateMachineDef {
  key: string;
  states: StateDef[];
  initial: string;
  transitions: TransitionDef[];
}

export interface ViewGroupDef {
  key: string;
  label_key: string;
  field_keys: string[];
}

export interface ViewTabDef {
  key: string;
  label_key: string;
  group_keys: string[];
}

export interface ViewDef {
  key: string;
  groups: ViewGroupDef[];
  tabs: ViewTabDef[];
}

export interface EntitySchemaDefinitionDoc {
  fields: FieldDef[];
  derived: DerivedDef[];
  constraints: ConstraintDef[];
  state_machines: StateMachineDef[];
  views: ViewDef[];
}

export interface SchemaResponse {
  id: string;
  key: string;
  version: number;
  workspace_id: string | null;
  definition: EntitySchemaDefinitionDoc;
}

export interface SchemaValidationIssue {
  field_path: string;
  message: string;
}

/** §16.5's "start from blank exists but is explicitly secondary" -- a guided minimal
 * template (one identity field), never a truly empty field list, so a blank canvas is
 * still an immediately-renderable schema. */
export function blankSchemaDefinition(): EntitySchemaDefinitionDoc {
  return {
    fields: [
      {
        key: "name",
        type: "string",
        indexed: false,
        tags: ["identity"],
        tag_metadata: {},
        enum: null,
        minimum: null,
        maximum: null,
        scope_key: null,
      },
    ],
    derived: [],
    constraints: [],
    state_machines: [],
    views: [],
  };
}
