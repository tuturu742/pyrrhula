import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

/**
 * E2.2: who knows the secret. `holder_kind` is `author|discovered|told` (E2.1's schema) --
 * a facilitator adding a holder here is almost always recording "told", since "author" is
 * set automatically at creation and "discovered" is meant to come from in-session events,
 * not manual entry, but the field is left free so a facilitator can correct history.
 */
export function HolderManager({ secretId }: { secretId: string }) {
  const queryClient = useQueryClient();
  const [holderPrincipalId, setHolderPrincipalId] = useState("");
  const [holderKind, setHolderKind] = useState("told");

  const { data: holders, isLoading } = useQuery({
    queryKey: ["secret-holders", secretId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/secrets/{secret_id}/holders", {
        params: { path: { secret_id: secretId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const addHolder = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/secrets/{secret_id}/holders", {
        params: { path: { secret_id: secretId } },
        body: { holder_principal_id: holderPrincipalId, holder_kind: holderKind },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setHolderPrincipalId("");
      void queryClient.invalidateQueries({ queryKey: ["secret-holders", secretId] });
    },
  });

  const removeHolder = useMutation({
    mutationFn: async (holderId: string) => {
      const { error } = await apiClient.DELETE("/secrets/{secret_id}/holders/{holder_id}", {
        params: { path: { secret_id: secretId, holder_id: holderId } },
      });
      if (error) throw error;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["secret-holders", secretId] }),
  });

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      <h3 className="text-sm font-medium">Holders</h3>
      {isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
      {holders?.length === 0 && (
        <p className="text-xs text-muted-foreground">No holders yet.</p>
      )}
      <ul className="flex flex-col gap-1">
        {holders?.map((holder) => (
          <li key={holder.id} className="flex items-center justify-between text-sm">
            <span>
              {holder.holder_name ?? holder.holder_principal_id}{" "}
              <span className="text-xs text-muted-foreground">({holder.holder_kind})</span>
            </span>
            <button
              type="button"
              onClick={() => removeHolder.mutate(holder.id)}
              className="text-xs text-destructive hover:underline"
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!holderPrincipalId.trim()) return;
          addHolder.mutate();
        }}
        className="flex items-end gap-2"
      >
        <div className="flex flex-1 flex-col gap-1">
          <label className="text-xs text-muted-foreground" htmlFor="holder-principal-id">
            Principal id
          </label>
          <input
            id="holder-principal-id"
            className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={holderPrincipalId}
            onChange={(e) => setHolderPrincipalId(e.target.value)}
          />
        </div>
        <select
          className="rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={holderKind}
          onChange={(e) => setHolderKind(e.target.value)}
        >
          <option value="author">author</option>
          <option value="discovered">discovered</option>
          <option value="told">told</option>
        </select>
        <button
          type="submit"
          disabled={addHolder.isPending}
          className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50 hover:bg-accent"
        >
          Add
        </button>
      </form>
      {addHolder.error !== null && (
        <p className="text-xs text-destructive">Failed to add holder.</p>
      )}
    </div>
  );
}
