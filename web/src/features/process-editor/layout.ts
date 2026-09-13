import type { ProcessDefinitionDSL } from "./dsl";

export interface NodePosition {
  x: number;
  y: number;
}

const COLUMN_WIDTH = 260;
const ROW_HEIGHT = 140;

/**
 * Positions aren't part of the authored DSL document (schema.py's models are all
 * `extra="forbid"`, and layout is presentation, not process-definition content — nothing
 * in B1.2's interpreter cares where a phase node sits on screen). So layout is computed
 * client-side and persisted separately in localStorage, keyed by workspace+key, rather
 * than smuggled into the document that gets published.
 */
export function autoLayout(dsl: ProcessDefinitionDSL): Record<string, NodePosition> {
  const depths = new Map<string, number>();
  const frontier: Array<[string, number]> = dsl.phases[dsl.initial_phase]
    ? [[dsl.initial_phase, 0]]
    : [];
  while (frontier.length > 0) {
    const next = frontier.shift();
    if (!next) break;
    const [key, depth] = next;
    if (depths.has(key) && depths.get(key)! <= depth) continue;
    depths.set(key, depth);
    const phase = dsl.phases[key];
    if (!phase) continue;
    for (const target of phaseTransitionTargets(phase)) {
      if (dsl.phases[target]) frontier.push([target, depth + 1]);
    }
  }
  // Any phase not reachable from initial_phase (e.g. mid-edit, dangling) still needs a
  // position so it isn't invisible while the author fixes it up.
  let maxDepth = 0;
  for (const d of depths.values()) maxDepth = Math.max(maxDepth, d);
  for (const key of Object.keys(dsl.phases)) {
    if (!depths.has(key)) depths.set(key, maxDepth + 1);
  }

  const columns = new Map<number, string[]>();
  for (const [key, depth] of depths) {
    const col = columns.get(depth) ?? [];
    col.push(key);
    columns.set(depth, col);
  }

  const positions: Record<string, NodePosition> = {};
  for (const [depth, keys] of columns) {
    keys.sort();
    keys.forEach((key, row) => {
      positions[key] = { x: depth * COLUMN_WIDTH, y: row * ROW_HEIGHT };
    });
  }
  return positions;
}

function phaseTransitionTargets(phase: ProcessDefinitionDSL["phases"][string]): string[] {
  const targets: string[] = [];
  if (phase.on_complete) targets.push(phase.on_complete);
  for (const gate of phase.gates ?? []) targets.push(gate.to);
  if (phase.await?.on_timeout) targets.push(phase.await.on_timeout);
  return targets;
}

function storageKey(workspaceId: string | null, defKey: string): string {
  return `pyrrhula:process-editor:layout:${workspaceId ?? "template"}:${defKey}`;
}

export function loadSavedLayout(
  workspaceId: string | null,
  defKey: string,
): Record<string, NodePosition> | null {
  try {
    const raw = localStorage.getItem(storageKey(workspaceId, defKey));
    if (!raw) return null;
    return JSON.parse(raw) as Record<string, NodePosition>;
  } catch {
    return null;
  }
}

export function saveLayout(
  workspaceId: string | null,
  defKey: string,
  positions: Record<string, NodePosition>,
): void {
  try {
    localStorage.setItem(storageKey(workspaceId, defKey), JSON.stringify(positions));
  } catch {
    // localStorage can throw (quota, private mode) -- layout persistence is a nicety,
    // never worth failing the editor over.
  }
}
