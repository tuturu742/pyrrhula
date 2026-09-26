import { create } from "zustand";
import { DEFAULT_LABELS } from "@/lib/vocabulary/labels";

interface VocabularyState {
  overlayKey: string;
  labels: Record<string, string>;
  setOverlay: (overlayKey: string, labels: Record<string, string>) => void;
}

/**
 * the currently active vocabulary overlay, live-swappable. Not persisted --
 * re-resolved per workspace on load via `useWorkspaceVocabulary` (`GET /workspaces/{id}/
 * vocabulary-overlay`), same as any other server-owned data. Every component calling
 * `useLabel()` subscribes to this store, so `setOverlay` (called by
 * `VocabularySwitcher` on a successful switch, and by `useWorkspaceVocabulary` on load)
 * relabels the entire UI live with no reload.
 */
export const useVocabularyStore = create<VocabularyState>()((set) => ({
  overlayKey: "rpg_v1",
  labels: DEFAULT_LABELS,
  setOverlay: (overlayKey, labels) => set({ overlayKey, labels }),
}));
