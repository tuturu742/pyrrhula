/**
 * Vocabulary overlay resolver.
 *
 * Core code and the API only ever emit `label_key`s (e.g. `"role.facilitator"`); the UI
 * resolves those through an overlay to get the domain-specific display string. Nothing in
 * this codebase ever hardcodes "Arbiter" or "Rulebook" outside this file and the overlay
 * seed data (`tests/architecture/test_vocabulary_lint.py` is the CI check for that rule).
 *
 * The real overlay is tenant/workspace-configurable data served by the backend
 * (`vocabulary_overlay` table, `GET /workspaces/{id}/vocabulary-overlay`) and switched
 * live via `useVocabularyStore`/`useWorkspaceVocabulary`. `DEFAULT_LABELS` below is the
 * frontend's own baked-in copy of the system `rpg_v1` overlay -- used as the fallback
 * chain's last-but-one tier (workspace override -> tenant default -> **this** -> key
 * itself) for the first paint before any async fetch resolves, and for pages with no
 * workspace context at all (login, workspace list).
 *
 * Rule for contributors (mirrors the backend's vocabulary rule): if you're
 * about to write "Arbiter", "Rulebook", "World", or any other RPG-overlay noun directly in
 * a `.tsx` file, stop — add the entry here (and to the overlay migration's seed data) and
 * call `useLabel()`/`t(key)` instead.
 */

export const DEFAULT_LABELS: Record<string, string> = {
  "entity.tenant": "Account",
  "entity.workspace": "World / Campaign",
  "entity.process_definition": "Session Flow / Turn Structure",
  "entity.phase": "Scene / Turn",
  "phase.deliberation": "Discussion Phase",
  "phase.action": "Turn Phase",
  "role.facilitator": "Arbiter",
  "role.participant": "Player Character bot / NPC bot",
  "role.informational": "Oracle / Sage",
  "role.human_participant": "Player",
  "role.overseer": "Director / Table Owner",
  "entity.knowledge_source": "Rulebook / Lorebook / Miscellany",
  "class.rules": "Rulebook",
  "class.lore": "Lorebook",
  "class.misc": "Miscellany",
  "entity.knowledge_entry": "Entry / Article",
  "entity.entity": "Character / NPC",
  "entity.entity_schema": "Character Sheet Template",
  "entity.state_machine": "Status Track",
  "entity.deterministic_tool": "Dice Roller / Randomizer",
  "entity.effectful_action": "Table Action",
  "entity.resolution_record": "Roll Result",
  "entity.rule_system": "Game System",
  "entity.scope": "Table Knowledge / GM-only / Faction",
  "entity.secret": "Secret / Hidden Motive",
  "entity.behavior_profile": "Personality Dials",
  "entity.disclosure_decision": "(hidden)",
  "entity.context_manifest": "What the Arbiter Knew",
  "entity.session": "Session / Game",
  "entity.checkpoint": "Save Point",
  "entity.report": "Campaign Recap / Session Log",
  "entity.pack": "Game System Pack",
  "phase.arbiter_narration": "Arbiter Narration",
  "phase.discussion": "Discussion Phase",
  "phase.turn": "Turn Phase",
  "phase.resolution": "Resolution",
  "phase.feedback": "Feedback",
  "phase.arbiter_narrate": "Arbiter Narration",
  "phase.player_act": "Player Turn",
  "phase.resolve": "Resolution",
  // #7: dual-mode round-table phases.
  "phase.framing": "Framing",
  "phase.regroup": "Regroup",
  "phase.synthesis": "Synthesis",
  // generic sheet-renderer chrome, not pack vocabulary -- same value regardless
  // of overlay, so it lives only here (no per-overlay divergence to seed in a
  // migration).
  "sheet.default_group": "Other",
};

/** Collected in dev mode only -- the "missing-key report" subtask. Read via
 * `getMissingKeysReport()`, e.g. from `MissingVocabularyKeysBadge` in `AppShell`. */
const _missingKeys = new Set<string>();

export function getMissingKeysReport(): string[] {
  return [..._missingKeys].sort();
}

/**
 * Resolves a core `label_key` to its display string. `labels` is the currently active
 * overlay's table (from `useVocabularyStore`); falls back to `DEFAULT_LABELS`, then to
 * the key itself (never a crash) -- a missing label should be visibly odd in the UI
 * during development, not fatal.
 */
export function label(labelKey: string, labels: Record<string, string> = DEFAULT_LABELS): string {
  const resolved = labels[labelKey] ?? DEFAULT_LABELS[labelKey];
  if (resolved === undefined) {
    _missingKeys.add(labelKey);
    if (import.meta.env.DEV) {
      console.warn(`[vocabulary] missing label for key "${labelKey}"`);
    }
    return labelKey;
  }
  return resolved;
}
