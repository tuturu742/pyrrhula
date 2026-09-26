import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQueries, useQuery } from "@tanstack/react-query";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { useDirectorViewStore } from "@/stores/directorView";
import {
  PermissionDeniedError,
  inspectSecret,
  listSecretHolders,
  listSecretsForWorkspace,
} from "./api";

const NO_HOLDERS_BUCKET = "(no holders yet)";

interface RevealedSecret {
  content: string;
  hintText: string | null;
  behavioralDirective: string | null;
}

/**
 * the overseer console's landing page -- secrets grouped by holder, gist-level by
 * default. Plaintext is never fetched for a secret until its own "Reveal plaintext"
 * button is clicked: each click is exactly one `inspectSecret()` call, exactly one new
 * `audit_log` row, and exactly one increment of the session's own inspection counter
 * (the design decision this page is built around -- a page that pre-loads every
 * plaintext would turn the audit log into noise).
 */
export function SecretsByHolderPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const t = useLabel();
  const recordInspection = useDirectorViewStore((s) => s.recordInspection);
  const [revealed, setRevealed] = useState<Record<string, RevealedSecret>>({});

  const {
    data: secrets,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["director-view-secrets", workspaceId],
    queryFn: () => listSecretsForWorkspace(workspaceId!),
    enabled: !!workspaceId,
  });

  const holderQueries = useQueries({
    queries: (secrets ?? []).map((secret) => ({
      queryKey: ["director-view-holders", workspaceId, secret.id],
      queryFn: () => listSecretHolders(workspaceId!, secret.id),
      enabled: !!workspaceId,
    })),
  });

  const reveal = useMutation({
    mutationFn: (secretId: string) => inspectSecret(workspaceId!, secretId),
    onSuccess: (view) => {
      recordInspection();
      setRevealed((prev) => ({
        ...prev,
        [view.id]: {
          content: view.content,
          hintText: view.hint_text,
          behavioralDirective: view.behavioral_directive,
        },
      }));
    },
  });

  if (!workspaceId) return null;

  if (error instanceof PermissionDeniedError) {
    return (
      <p className="text-destructive" role="alert">
        You do not have permission to inspect {t("entity.secret")}s in this{" "}
        {t("entity.workspace")}.
      </p>
    );
  }

  // Wait for every secret's holder list to settle before grouping -- rendering a secret
  // under a transient "no holders yet" bucket and then re-parenting it once its real
  // holders arrive would unmount and remount its "Reveal plaintext" button mid-render,
  // silently orphaning any reference (e.g. a pending click) held against the old node.
  const holdersSettled = holderQueries.every((q) => q.isSuccess || q.isError);

  const secretsByHolder = new Map<string, { secretId: string; gist: string }[]>();
  if (holdersSettled) {
    (secrets ?? []).forEach((secret, index) => {
      const holders = holderQueries[index]?.data ?? [];
      const holderKeys =
        holders.length === 0 ? [NO_HOLDERS_BUCKET] : holders.map((h) => h.holder_principal_id);
      for (const holderKey of holderKeys) {
        const bucket = secretsByHolder.get(holderKey) ?? [];
        bucket.push({ secretId: secret.id, gist: secret.gist });
        secretsByHolder.set(holderKey, bucket);
      }
    });
  }

  return (
    <div className="flex flex-col gap-4">
      {(isLoading || (secrets !== undefined && secrets.length > 0 && !holdersSettled)) && (
        <p className="text-muted-foreground">Loading…</p>
      )}
      {secrets?.length === 0 && (
        <p className="text-muted-foreground">
          No {t("entity.secret")}s in this {t("entity.workspace")} yet.
        </p>
      )}
      {holdersSettled &&
        [...secretsByHolder.entries()].map(([holderKey, items]) => (
        <div key={holderKey} className="rounded-md border border-border p-3">
          <h2 className="text-sm font-medium text-muted-foreground">
            {holderKey === NO_HOLDERS_BUCKET ? (
              holderKey
            ) : (
              <>
                Holder:{" "}
                <Link to={`agents/${holderKey}/beliefs`} className="underline hover:text-foreground">
                  {holderKey}
                </Link>
              </>
            )}
          </h2>
          <ul className="mt-2 flex flex-col gap-2">
            {items.map(({ secretId, gist }) => (
              <li
                key={`${holderKey}-${secretId}`}
                className="flex flex-col gap-1 rounded-md bg-muted p-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium">{gist}</span>
                  <div className="flex items-center gap-2">
                    <Link to={`secrets/${secretId}/timeline`} className="text-xs underline">
                      Timeline
                    </Link>
                    <button
                      type="button"
                      disabled={reveal.isPending}
                      onClick={() => reveal.mutate(secretId)}
                      className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
                    >
                      Reveal plaintext
                    </button>
                  </div>
                </div>
                {revealed[secretId] && (
                  <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
                    <div>{revealed[secretId].content}</div>
                    {revealed[secretId].behavioralDirective && (
                      <div className="mt-1 text-muted-foreground">
                        Directive: {revealed[secretId].behavioralDirective}
                      </div>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
