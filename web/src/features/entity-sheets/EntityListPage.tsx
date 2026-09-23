import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { BackLink } from "@/components/BackLink";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";

/**
 * Every entity in the workspace, grouped by schema.
 *
 * The sheet route existed and nothing listed what it could open: an entity was reachable
 * only through a persona that happened to be bound to it, or through the session cast
 * panel, which shows a row only while a state machine is running on it. So an entity
 * created outside a session, one nothing had transitioned yet, and anything imported
 * from a `.pyr` were all in the database, correct, and unreachable in the UI.
 */
export function EntityListPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const t = useLabel();
  const [schemaFilter, setSchemaFilter] = useState<string>("");

  const { data, isLoading, error } = useQuery({
    enabled: Boolean(workspaceId),
    queryKey: ["workspace-entities", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/entities", {
        params: { query: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      return data;
    },
  });

  const entities = useMemo(() => data ?? [], [data]);
  const schemaKeys = useMemo(
    () => Array.from(new Set(entities.map((e) => e.schema_key))).sort(),
    [entities],
  );
  const shown = schemaFilter ? entities.filter((e) => e.schema_key === schemaFilter) : entities;

  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <BackLink to={`/workspaces/${workspaceId}`} label={`Back to ${t("entity.workspace")}`} />
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">{t("entity.entity") || "Entities"}</h1>
        {schemaKeys.length > 1 && (
          <select
            value={schemaFilter}
            onChange={(e) => setSchemaFilter(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm"
            aria-label="Filter by schema"
          >
            <option value="">All schemas</option>
            {schemaKeys.map((key) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        )}
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error != null && (
        <p className="text-sm text-destructive">Could not load this {t("entity.workspace")}’s entities.</p>
      )}
      {!isLoading && error == null && shown.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Nothing here yet. Entities appear once a session creates one, a pack seeds one, or
          a <code>.pyr</code> bundle brings one in.
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {shown.map((e) => (
          <li
            key={e.id}
            className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
          >
            <div className="flex min-w-0 flex-col">
              <Link
                to={`/workspaces/${workspaceId}/entities/${e.id}`}
                className="truncate text-sm font-medium hover:underline"
              >
                {e.name}
              </Link>
              <span className="truncate text-xs text-muted-foreground">{e.schema_key}</span>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-1">
              {Object.entries(e.fsm_states as Record<string, string>).map(([machine, state]) => (
                <span
                  key={machine}
                  title={machine}
                  className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                >
                  {t(`status.${state}`) || t(`condition.${state}`) || state}
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
