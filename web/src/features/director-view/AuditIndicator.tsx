import { useDirectorViewStore } from "@/stores/directorView";

/**
 * Persistent, not dismissible (E2.11's own design decision) -- rendered once by
 * `DirectorViewLayout` so every route under the feature carries it, rather than each page
 * remembering to render its own copy.
 */
export function AuditIndicator() {
  const count = useDirectorViewStore((s) => s.inspectionCount);
  return (
    <div
      data-testid="audit-indicator"
      role="status"
      className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300"
    >
      Inspections are logged -- {count} plaintext {count === 1 ? "inspection" : "inspections"}{" "}
      this session.
    </div>
  );
}
