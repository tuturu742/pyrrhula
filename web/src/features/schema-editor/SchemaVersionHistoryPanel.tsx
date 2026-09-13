import { useQuery } from "@tanstack/react-query";
import type { EntitySchemaDefinitionDoc } from "./types";
import { listSchemaVersions } from "./api";

interface SchemaVersionHistoryPanelProps {
  workspaceId: string | null;
  defKey: string;
  currentVersion: number | null;
  onLoadVersion: (definition: EntitySchemaDefinitionDoc, version: number) => void;
}

/**
 * F3.11's version history, A1.8/D1.1's presentation pattern -- every version of this
 * key, newest first (`GET /entities/schemas/versions`, added alongside this panel since
 * `list_latest_schemas` deliberately can't serve this).
 */
export function SchemaVersionHistoryPanel({
  workspaceId,
  defKey,
  currentVersion,
  onLoadVersion,
}: SchemaVersionHistoryPanelProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["entity-schema-versions", workspaceId, defKey],
    queryFn: () => listSchemaVersions(defKey, workspaceId),
    enabled: defKey.trim() !== "",
  });

  const versions = (data ?? []).sort((a, b) => b.version - a.version);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      <h3 className="text-sm font-medium">Version history</h3>
      {isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
      {versions.length === 0 && !isLoading && <p className="text-xs text-muted-foreground">Not saved yet.</p>}
      <ul className="flex flex-col gap-1">
        {versions.map((v) => (
          <li key={v.id} className="flex items-center justify-between text-xs">
            <span>
              v{v.version}
              {v.version === currentVersion && " (current draft base)"}
            </span>
            <button
              type="button"
              onClick={() => onLoadVersion(v.definition, v.version)}
              className="rounded-md border border-border px-2 py-0.5"
            >
              View / restore
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
