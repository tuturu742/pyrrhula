/**
 * D1.2: hand-written mirror of the backend's authored-document shape
 * (`packages/core/process/dsl/schema.py`'s `ProcessDefinitionDSL` and friends).
 *
 * This can't come from the generated OpenAPI schema (`schema.ts`) because the wire type
 * of `DefinitionResponse.definition` is `{ [key: string]: unknown }` — Pydantic's
 * `dict[str, object]` on `create_definition`/`get_definition` erases the DSL's own
 * structure at the API boundary (the backend re-validates it server-side via
 * `/process-definitions/validate` regardless, which is the actual source of truth for
 * "is this a legal document"). This module is a *authoring convenience* type for the
 * editor's own state — it must be kept in sync with `schema.py` by hand when that file
 * changes, deliberately narrower/duplicated rather than trying to derive a structural
 * type from an untyped `dict`.
 */

export const KNOWN_AGENT_ROLES = ["facilitator", "participant", "informational"] as const;
export type AgentRole = (typeof KNOWN_AGENT_ROLES)[number];

export const KNOWN_ANY_OF_TOKENS = [
  "facilitator_agent",
  "participant_agent",
  "informational_agent",
  "human_participant",
  "human_overseer",
] as const;
export type AnyOfToken = (typeof KNOWN_ANY_OF_TOKENS)[number];

export type StateVarType = "integer" | "string" | "boolean" | "number";

export interface StateVarSpec {
  type: StateVarType;
  default: number | string | boolean;
}

export type ActorOrder = "declared" | "initiative" | "free";
export type ActorMode = "free" | "generate" | "generate_as";

export interface ActorSpec {
  persona_type?: AgentRole;
  any_of?: AnyOfToken[];
  human_participant?: "all";
  mode: ActorMode;
  order?: ActorOrder;
  from?: string;
  max_turns?: number;
}

export type SecretsVisibility = "held_by_actor" | "none";

export interface VisibilitySpec {
  knowledge_classes: string[];
  scopes: string[];
  entity_fields: "all" | string[];
  secrets: SecretsVisibility;
}

export type BudgetSpill = "proportional" | "none";

export interface BudgetSpec {
  ratio: Record<string, number>;
  max_tokens: number;
  spill?: BudgetSpill;
}

export interface GateSpec {
  on?: string;
  when?: string;
  else?: boolean;
  to: string;
}

export interface EffectSpec {
  set: string;
  to: string;
}

export interface AwaitSpec {
  type: "human_input";
  timeout: string;
  on_timeout: string;
}

export interface PhaseSpec {
  label_key: string;
  /** Task instructions injected into every acting persona's turn in this phase. */
  prompt?: string;
  actors: ActorSpec[];
  visibility: VisibilitySpec;
  budget?: BudgetSpec;
  gates?: GateSpec[];
  effects?: EffectSpec[];
  await?: AwaitSpec;
  flags?: string[];
  tools?: string[];
  on_complete?: string;
}

export interface ProcessDefinitionDSL {
  name: string;
  vocabulary_overlay: string;
  state?: Record<string, StateVarSpec>;
  initial_phase: string;
  phases: Record<string, PhaseSpec>;
}

/** A brand-new phase, structurally valid but empty enough to prompt the author to fill
 * it in — visibility has no legal all-empty default (INV structural: mandatory, no
 * defaults), so this uses the narrowest legal shape rather than omitting the block. */
export function blankPhase(): PhaseSpec {
  return {
    label_key: "",
    actors: [],
    visibility: {
      knowledge_classes: [],
      scopes: [],
      entity_fields: "all",
      secrets: "none",
    },
    gates: [],
    effects: [],
    flags: [],
    tools: [],
  };
}

export function blankDefinition(): ProcessDefinitionDSL {
  return {
    name: "",
    vocabulary_overlay: "rpg_v1",
    state: {},
    initial_phase: "start",
    phases: { start: blankPhase() },
  };
}
