import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NameDialog } from "@/components/NameDialog";
import { Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { UsageLimitsCard } from "./UsageLimitsCard";

export function WorkspaceListPage() {
  const t = useLabel();
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces");
      if (error) throw error;
      return data;
    },
  });

  const createWorkspace = useMutation({
    mutationFn: async (name: string) => {
      const { data, error } = await apiClient.POST("/workspaces", { body: { name } });
      if (error) throw error;
      return data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{t("entity.workspace")}s</h1>
        <NameDialog
          title={"New " + t("entity.workspace").toLowerCase()}
          placeholder="Name"
          submitLabel="Create"
          onSubmit={(name) => createWorkspace.mutate(name)}
        >
          <button
            type="button"
            disabled={createWorkspace.isPending}
            className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
          >
            New {t("entity.workspace").toLowerCase()}
          </button>
        </NameDialog>
      </div>
      {createWorkspace.error !== null && (
        <p className="text-sm text-destructive">
          Could not create it — only organization owners can create {t("entity.workspace").toLowerCase()}s.
        </p>
      )}
      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {error !== null && <p className="text-destructive">Failed to load workspaces.</p>}
      {data?.length === 0 && (
        <p className="text-muted-foreground">
          No {t("entity.workspace")}s yet — create your first one above.
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {data?.map((ws) => (
          <li key={ws.id}>
            <Link
              to={`/workspaces/${ws.id}`}
              className="block rounded-md border border-border px-4 py-3 hover:bg-accent"
            >
              <div className="font-medium">{ws.name}</div>
              <div className="text-sm text-muted-foreground">{ws.key}</div>
            </Link>
          </li>
        ))}
      </ul>
      <UsageLimitsCard />
    </div>
  );
}
