import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmButton } from "@/components/ConfirmButton";

const ROLES = ["steward", "participant", "facilitator", "overseer", "viewer"] as const;

const ROLE_HINTS: Record<(typeof ROLES)[number], string> = {
  steward: "builds and watches — a solo owner's seat (facilitator + overseer)",
  facilitator: "conducts sessions, authors secrets / knowledge / flows",
  overseer: "inspects held secrets and the audit (Director's View)",
  participant: "acts in sessions",
  viewer: "reads only",
};

/** Human teammates in this workspace. Personas join via persona creation; this card is
 * people. A workspace's creator is a steward — the combined seat that can both build the
 * room and watch it — so a solo user needs no second account; multi-human rooms split
 * facilitator and overseer where that separation matters. */
export function MembersCard({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<(typeof ROLES)[number]>("participant");

  const members = useQuery({
    queryKey: ["workspace-members", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/members", {
        params: { path: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const add = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/workspaces/{workspace_id}/members", {
        params: { path: { workspace_id: workspaceId } },
        body: { email: email.trim(), role },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setEmail("");
      void queryClient.invalidateQueries({ queryKey: ["workspace-members", workspaceId] });
    },
    onError: (e) =>
      toast.error(
        String((e as { detail?: string })?.detail ?? "Could not add that person."),
      ),
  });

  const remove = useMutation({
    mutationFn: async (principalId: string) => {
      const { error } = await apiClient.DELETE(
        "/workspaces/{workspace_id}/members/{principal_id}",
        { params: { path: { workspace_id: workspaceId, principal_id: principalId } } },
      );
      if (error) throw error;
    },
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["workspace-members", workspaceId] }),
    onError: () => toast.error("Could not remove that member."),
  });

  const humans = (members.data ?? []).filter((m) => !m.is_persona);

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <h2 className="text-sm font-medium">Members</h2>
      {members.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {humans.length === 0 && members.isSuccess && (
        <p className="text-sm text-muted-foreground">
          Just you so far — add a teammate by their login email.
        </p>
      )}
      <ul className="flex flex-col gap-1.5">
        {humans.map((m) => (
          <li key={m.principal_id} className="flex items-center justify-between gap-2 text-sm">
            <span>
              {m.display_name}
              {m.email && (
                <span className="ml-2 text-xs text-muted-foreground">{m.email}</span>
              )}
            </span>
            <span className="flex items-center gap-2">
              <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                {m.role}
              </span>
              <ConfirmButton
                title="Remove member"
                description={`Remove ${m.display_name} from this workspace? They keep their account and can be re-added any time.`}
                confirmLabel="Remove"
                destructive
                onConfirm={() => remove.mutate(m.principal_id)}
              >
                <Button variant="ghost" size="sm" className="text-destructive">
                  Remove
                </Button>
              </ConfirmButton>
            </span>
          </li>
        ))}
      </ul>
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (email.trim()) add.mutate();
        }}
      >
        <Input
          type="email"
          placeholder="teammate@example.com"
          className="w-64"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <select
          className="rounded-md border border-input bg-transparent px-2 py-1.5 text-sm"
          value={role}
          onChange={(e) => setRole(e.target.value as (typeof ROLES)[number])}
          title={ROLE_HINTS[role]}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <Button type="submit" variant="outline" disabled={!email.trim() || add.isPending}>
          Add member
        </Button>
      </form>
      <p className="mt-1 text-xs text-muted-foreground">{role}: {ROLE_HINTS[role]}</p>
    </div>
  );
}
