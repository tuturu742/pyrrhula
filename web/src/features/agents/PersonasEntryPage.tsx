import { useQuery } from "@tanstack/react-query";
import { Link, Navigate } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";

/**
 * Top-nav entry for persona management. Personas are workspace-scoped; a tenant with one
 * workspace (the common case) goes straight to its personas page, several get a picker.
 */
export function PersonasEntryPage() {
  const t = useLabel();
  const { data: workspaces, isLoading, isError } = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces");
      if (error) throw error;
      return data;
    },
  });

  if (isLoading) return <p className="text-muted-foreground">Loading…</p>;
  if (isError)
    return (
      <p className="py-8 text-sm text-destructive">
        Could not load your {t("entity.workspace").toLowerCase()}s — please refresh.
      </p>
    );
  if (workspaces && workspaces.length === 0) {
    return (
      <div className="flex flex-col gap-3 py-8">
        <h1 className="text-xl font-semibold">Personas</h1>
        <p className="text-sm text-muted-foreground">
          Personas live inside a {t("entity.workspace").toLowerCase()} — create one first.
        </p>
        <Link to="/" className="w-fit rounded-md border border-border px-4 py-2 text-sm hover:bg-accent">
          Go to {t("entity.workspace")}s
        </Link>
      </div>
    );
  }
  if (workspaces && workspaces.length === 1) {
    return <Navigate to={`/workspaces/${workspaces[0].id}/agents`} replace />;
  }
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Personas</h1>
      <p className="text-sm text-muted-foreground">
        Pick the {t("entity.workspace").toLowerCase()} whose personas to manage:
      </p>
      <ul className="flex flex-col gap-2">
        {workspaces?.map((w) => (
          <li key={w.id}>
            <Link
              to={`/workspaces/${w.id}/agents`}
              className="block rounded-md border border-border px-4 py-2 hover:bg-accent"
            >
              {w.name ?? w.key ?? w.id}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
