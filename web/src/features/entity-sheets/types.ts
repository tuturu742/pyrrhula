/** Wire shapes for F3.10's entity view -- kept as a small, hand-written mirror of
 * `packages/api/routes/entities.py`'s response models (the generated `schema.ts` types
 * these structurally match) so the widget components below have simple, direct types
 * to import without every one of them reaching into the generated schema file. */

export interface EntityFieldView {
  key: string;
  type: string;
  value: unknown;
  tags: string[];
  tag_metadata: Record<string, unknown>;
  modifier?: unknown;
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

export interface EntityView {
  id: string;
  key: string;
  name: string;
  schema_id: string;
  version: number;
  fields: EntityFieldView[];
  derived: Record<string, unknown>;
  fsm_states: Record<string, string>;
  views: ViewDef[];
}

export interface HistoryEntry {
  id: string;
  field_path: string;
  old_value: unknown;
  new_value: unknown;
  cause: string;
  created_at: string;
}
