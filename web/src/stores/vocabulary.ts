import { create } from "zustand";
import { DEFAULT_LABELS } from "@/lib/vocabulary/labels";

interface Overlay {
  key: string;
  labels: Record<string, string>;
}

interface VocabularyState {
  /** The overlay every `useLabel()` call reads: the workspace's while one is in scope,
   * else the tenant default, else the shipped defaults. */
  overlayKey: string;
  labels: Record<string, string>;
  tenant: Overlay | null;
  workspace: Overlay | null;
  setTenantOverlay: (overlayKey: string, labels: Record<string, string>) => void;
  setWorkspaceOverlay: (overlayKey: string, labels: Record<string, string>) => void;
  clearWorkspaceOverlay: () => void;
  /** Live switch from `VocabularySwitcher`: replaces whichever layer is active. */
  setOverlay: (overlayKey: string, labels: Record<string, string>) => void;
}

function effective(tenant: Overlay | null, workspace: Overlay | null): Overlay {
  return workspace ?? tenant ?? { key: "rpg_v1", labels: DEFAULT_LABELS };
}

/**
 * the currently active vocabulary overlay, live-swappable. Not persisted --
 * re-resolved from the server like any other server-owned data: the tenant default once
 * per shell mount (`useTenantVocabulary`, `GET /tenant/vocabulary-overlay`) and the
 * workspace's own on every page with a workspace in scope (`useWorkspaceVocabulary`,
 * `GET /workspaces/{id}/vocabulary-overlay`), which clears itself when that page
 * unmounts so the workspace list, the persona picker and the schema library fall back
 * to the tenant's vocabulary rather than the last workspace's -- or, on a fresh load,
 * the RPG defaults. Every component calling `useLabel()` subscribes to this store, so
 * any of these relabels the entire UI live with no reload.
 */
export const useVocabularyStore = create<VocabularyState>()((set) => ({
  overlayKey: "rpg_v1",
  labels: DEFAULT_LABELS,
  tenant: null,
  workspace: null,
  setTenantOverlay: (overlayKey, labels) =>
    set((s) => {
      const tenant = { key: overlayKey, labels };
      const eff = effective(tenant, s.workspace);
      return { tenant, overlayKey: eff.key, labels: eff.labels };
    }),
  setWorkspaceOverlay: (overlayKey, labels) =>
    set((s) => {
      const workspace = { key: overlayKey, labels };
      const eff = effective(s.tenant, workspace);
      return { workspace, overlayKey: eff.key, labels: eff.labels };
    }),
  clearWorkspaceOverlay: () =>
    set((s) => {
      const eff = effective(s.tenant, null);
      return { workspace: null, overlayKey: eff.key, labels: eff.labels };
    }),
  setOverlay: (overlayKey, labels) =>
    set((s) => {
      const layer = { key: overlayKey, labels };
      return s.workspace
        ? { workspace: layer, overlayKey, labels }
        : { tenant: layer, overlayKey, labels };
    }),
}));
