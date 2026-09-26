import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { listSchemaTemplates, listSchemas } from "./api";

/**
 * F3.11 hub, mirroring `ProcessDefinitionListPage`'s exact split: existing schemas in
 * the current scope to keep editing, or the template gallery to start a new one.
 * "Start from blank" is present but visually secondary ( discipline 2) -- it's a
 * plain link at the bottom of the templates section, not a competing first option.
 */
export function SchemaListPage() {
  const t = useLabel();
  const navigate = useNavigate();
  const [workspaceId, setWorkspaceId] = useState<string>("");

  const { data: workspaces } = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces");
      if (error) throw error;
      return data;
    },
  });

  const { data: schemas, isLoading } = useQuery({
    queryKey: ["entity-schemas", workspaceId],
    queryFn: () => listSchemas(workspaceId || null),
  });

  const { data: templates } = useQuery({
    queryKey: ["entity-schema-templates"],
    queryFn: listSchemaTemplates,
  });

  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Entity Schemas</h1>
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          {t("entity.workspace")}
          <select
            className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
          >
            <option value="">(tenant templates -- no workspace)</option>
            {workspaces?.map((ws) => (
              <option key={ws.id} value={ws.id}>
                {ws.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Existing schemas</h2>
        {isLoading && <p className="text-muted-foreground">Loading…</p>}
        {(schemas?.length ?? 0) === 0 && !isLoading && (
          <p className="text-muted-foreground">None yet in this scope -- start from a template below.</p>
        )}
        <ul className="flex flex-col gap-2">
          {schemas?.map((s) => (
            <li key={s.id}>
              <Link
                to={`/schemas/${s.id}`}
                className="flex items-center justify-between rounded-md border border-border px-4 py-3 hover:bg-accent"
              >
                <div className="font-mono">{s.key}</div>
                <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">
                  v{s.version}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-medium">Start from a template</h2>
        <div className="grid grid-cols-2 gap-3">
          {templates?.map((tpl) => (
            <button
              key={tpl.key}
              type="button"
              onClick={() =>
                navigate(
                  `/schemas/new?template=${encodeURIComponent(tpl.key)}${
                    workspaceId ? `&workspace=${encodeURIComponent(workspaceId)}` : ""
                  }`,
                )
              }
              className="flex flex-col items-start gap-1 rounded-md border border-border p-4 text-left hover:bg-accent"
            >
              <span className="font-mono font-medium">{tpl.key}</span>
              <span className="text-xs text-muted-foreground">{tpl.definition.fields.length} fields</span>
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() =>
            navigate(`/schemas/new${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ""}`)
          }
          className="self-start text-xs text-muted-foreground underline"
        >
          Start from blank instead
        </button>
      </section>
    </div>
  );
}
