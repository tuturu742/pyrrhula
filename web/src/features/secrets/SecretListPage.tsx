import { useState } from "react";
import { BackLink } from "@/components/BackLink";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { SecretEditor } from "./SecretEditor";

const WORKED_EXAMPLES = [
  {
    overlay: "RPG",
    gist: "the innkeeper has a hidden identity",
    detail: '"the innkeeper is the missing heir" -- a hidden motive worth discovering play.',
  },
  {
    overlay: "Enterprise",
    gist: "pending material information about the Q3 deal",
    detail: "MNPI restricted to the deal team until the public announcement.",
  },
  {
    overlay: "Software delivery",
    gist: "root cause of the outage is not yet public",
    detail:
      '"the outage was caused by our own migration script" -- undisclosed until the postmortem publishes.',
  },
];

/**
 * the authoring hub for a workspace's secrets -- list, create, and select-to-edit,
 * all without leaving this page. Worked examples appear only when the workspace has no
 * secrets yet (one per overlay, per the task's own brief) so an author sees what a good
 * gist/directive pair looks like before writing their first one.
 */
export function SecretListPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const t = useLabel();
  const [selectedSecretId, setSelectedSecretId] = useState<string | null | "new">(null);

  const { data: secrets, isLoading, error } = useQuery({
    queryKey: ["secrets", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/secrets", {
        params: { query: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!workspaceId,
  });

  if (!workspaceId) return null;

  return (
    <div className="flex flex-col gap-4">
      <BackLink to={`/workspaces/${workspaceId}`} label="Back to the workspace" />
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{t("entity.secret")}s</h1>
        <button
          type="button"
          onClick={() => setSelectedSecretId(selectedSecretId === "new" ? null : "new")}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
        >
          {selectedSecretId === "new" ? "Cancel" : `New ${t("entity.secret")}`}
        </button>
      </div>

      {selectedSecretId === "new" && (
        <SecretEditor
          secretId={null}
          workspaceId={workspaceId}
          onSaved={() => setSelectedSecretId(null)}
          onCancel={() => setSelectedSecretId(null)}
        />
      )}

      {selectedSecretId !== null && selectedSecretId !== "new" && (
        <SecretEditor
          secretId={selectedSecretId}
          workspaceId={workspaceId}
          onSaved={() => setSelectedSecretId(null)}
          onCancel={() => setSelectedSecretId(null)}
        />
      )}

      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {error !== null && <p className="text-destructive">Failed to load {t("entity.secret")}s.</p>}

      {secrets?.length === 0 && selectedSecretId === null && (
        <div className="flex flex-col gap-3 rounded-md border border-dashed border-border p-4">
          <p className="text-muted-foreground">
            No {t("entity.secret")}s in this {t("entity.workspace")} yet. A few worked
            examples:
          </p>
          <ul className="flex flex-col gap-2">
            {WORKED_EXAMPLES.map((example) => (
              <li key={example.overlay} className="rounded-md bg-muted p-3 text-sm">
                <div className="text-xs font-medium uppercase text-muted-foreground">
                  {example.overlay}
                </div>
                <div className="font-medium">Gist: {example.gist}</div>
                <div className="text-muted-foreground">{example.detail}</div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <ul className="flex flex-col gap-2">
        {secrets?.map((secret) => (
          <li key={secret.id}>
            <button
              type="button"
              onClick={() => setSelectedSecretId(secret.id)}
              className="flex w-full items-center justify-between rounded-md border border-border px-4 py-3 text-left hover:bg-accent"
            >
              <div>
                <div className="font-medium">{secret.gist}</div>
                <div className="text-sm text-muted-foreground">
                  {secret.subject_name ?? secret.subject_kind} · {secret.disclosure_state}
                </div>
              </div>
              <span className="flex items-center gap-2">
                {secret.publication === "publishable" && (
                  <span
                    className="rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-600 dark:text-emerald-400"
                    title="Written to be shared: readable by anyone in this workspace, and it can travel in an unencrypted bundle. What an agent sees is still decided by who holds it."
                  >
                    publishable
                  </span>
                )}
                {secret.content === null && (
                  <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted-foreground">
                    gist only
                  </span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
