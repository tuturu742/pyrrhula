import { create } from "zustand";

interface DirectorViewState {
  inspectionCount: number;
  recordInspection: () => void;
}

/**
 * backs the persistent "inspections are logged" indicator. Counts this browser
 * session's own plaintext expansions (`OverseerService.inspect()` calls) -- deliberately
 * not persisted across reloads, since it's a running total for the current visit to the
 * feature, not a lifetime count (the durable record of every inspection is `audit_log`
 * itself, not this store).
 */
export const useDirectorViewStore = create<DirectorViewState>()((set) => ({
  inspectionCount: 0,
  recordInspection: () => set((s) => ({ inspectionCount: s.inspectionCount + 1 })),
}));
