import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";

/**
 * How held secrets reach their holders in secret-capable phases: a trust level the
 * workspace owner picks. Cross-persona exclusion is structural in every mode — no
 * persona ever sees another's secret. The choice is only about a persona's OWN briefs.
 */
const SECRET_MODES = [
  {
    value: "excluded",
    label: "Excluded (default)",
    hint:
      "Held secrets never enter any agent's context. Nothing can leak — and nothing " +
      "can be hinted at or dramatically revealed either.",
  },
  {
    value: "trust",
    label: "Trusted to the model",
    hint:
      "Each persona gets its own briefs in context, directive and all, and the model " +
      "plays them. No extra calls, best drama — for models smart enough to keep " +
      "character. What a persona says about its own secret is the model's judgement, " +
      "and speaking one aloud does not update who officially knows it.",
  },
  {
    value: "gate",
    label: "Gated",
    hint:
      "A small classifier decides per turn whether each secret may be concealed, " +
      "hinted at, or revealed, and the verdict is enforced by exclusion. One extra " +
      "model call per secret-holding turn — for models you don't trust with the " +
      "plaintext, and for tables where a reveal must change who knows what.",
  },
] as const;

export function SecretsGateCard({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["workspace-settings", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });
  const settings = data?.settings as Record<string, unknown> | undefined;
  const mode =
    (settings?.secret_mode as string | undefined) ??
    (settings?.secrets_gate ? "gate" : "excluded");

  const save = useMutation({
    onError: () => toast.error("That didn't save — please try again."),
    mutationFn: async (next: string) => {
      const { error } = await apiClient.PATCH("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
        body: { secret_mode: next },
      });
      if (error) throw error;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["workspace-settings", workspaceId] }),
  });

  return (
    <div className="rounded-lg border p-4">
      <div className="text-sm font-medium">Secret handling</div>
      <p className="mt-1 text-xs text-muted-foreground">
        How each persona&apos;s own held secrets reach its context. In every mode, no
        persona ever sees another&apos;s secret — that part is enforced by the system,
        not chosen here.
      </p>
      <div className="mt-3 flex flex-col gap-3">
        {SECRET_MODES.map((m) => (
          <div key={m.value}>
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="radio"
                name="secret-mode"
                checked={mode === m.value}
                disabled={save.isPending}
                onChange={() => save.mutate(m.value)}
              />
              {m.label}
            </label>
            <p className="ml-5 mt-0.5 text-xs text-muted-foreground">{m.hint}</p>
          </div>
        ))}
      </div>
    </div>
  );
}


/** Free-text conduct/style rules appended to every agent turn in this workspace —
 * formatting is content the owner controls, not hardcoded behavior. Flows can still
 * override per phase via the process editor's phase prompt. */
export function ConductRulesCard({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);

  const settings = useQuery({
    queryKey: ["workspace-settings", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });
  const stored = String(
    (settings.data?.settings as Record<string, unknown> | undefined)?.conduct_rules ?? "",
  );

  const save = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PATCH("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
        body: { conduct_rules: draft ?? stored },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setDraft(null);
      void queryClient.invalidateQueries({ queryKey: ["workspace-settings", workspaceId] });
    },
    onError: () => toast.error("Could not save the conduct rules."),
  });

  return (
    <div className="flex flex-col gap-2 rounded-lg border p-4">
      <div className="text-sm font-medium">
        Conduct rules{" "}
        <span className="font-normal text-muted-foreground">
          — appended to every agent turn here. Style, length, table etiquette; flows can
          override per phase in the process editor.
        </span>
      </div>
      <textarea
        className="min-h-24 rounded-md border border-input bg-transparent p-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        placeholder={
          "e.g. Reply in short paragraphs (2–4 sentences) with blank lines between them.\n" +
          "Never write another character's dialogue or actions.\n" +
          "Do not prefix replies with your name."
        }
        value={draft ?? stored}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div>
        <button
          type="button"
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
          disabled={draft === null || save.isPending}
          onClick={() => save.mutate()}
        >
          Save rules
        </button>
      </div>
    </div>
  );
}


export function AutoMergeCard({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["workspace-settings", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });
  const on = Boolean(
    (settings.data?.settings as Record<string, unknown> | undefined)?.allow_automerge,
  );

  const save = useMutation({
    mutationFn: async (next: boolean) => {
      const { error } = await apiClient.PATCH("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
        body: { allow_automerge: next },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspace-settings", workspaceId] });
    },
    onError: () => toast.error("Could not change the auto-merge setting."),
  });

  return (
    <div className="flex flex-col gap-2 rounded-lg border p-4">
      <label className="flex items-center gap-2 text-sm font-medium">
        <input
          type="checkbox"
          checked={on}
          disabled={save.isPending}
          onChange={(e) => save.mutate(e.target.checked)}
        />
        Allow agents to merge approved pull requests
      </label>
      <p className="text-sm text-muted-foreground">
        {on
          ? "On — when the reviewer approves a PR, the merger agent merges it under its own git identity."
          : "Off (default) — agents review and approve, but a human merges. An approved PR is marked ready and waits."}{" "}
        The host's own branch protection still applies on top of this.
      </p>
    </div>
  );
}

/**
 * Settings that resolve through the chain (workspace → tenant → deployment default).
 *
 * Blank means inherit, and the placeholder shows what is actually in force, so nobody has
 * to guess whether an empty box means "off" or "not set here". Saving a blank field CLEARS
 * the workspace's override rather than storing an empty value — storing one would read as
 * a deliberate choice of nothing.
 */
export function InheritedSettingsCard({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["workspace-settings", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const stored = (settings.data?.settings ?? {}) as Record<string, unknown>;
  const [rounds, setRounds] = useState<string | null>(null);
  const [moderation, setModeration] = useState<string | null>(null);

  const roundsValue = rounds ?? (stored.max_review_rounds != null ? String(stored.max_review_rounds) : "");
  const moderationValue = moderation ?? (stored.moderation_model != null ? String(stored.moderation_model) : "");

  const save = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PATCH("/workspaces/{workspace_id}/settings", {
        params: { path: { workspace_id: workspaceId } },
        body: {
          // null clears the override; the resolver then falls back to the tenant.
          max_review_rounds: roundsValue.trim() === "" ? null : Number(roundsValue),
          moderation_model: moderationValue.trim() === "" ? null : moderationValue.trim(),
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setRounds(null);
      setModeration(null);
      toast.success("Workspace settings saved.");
      void queryClient.invalidateQueries({ queryKey: ["workspace-settings", workspaceId] });
    },
    onError: () => toast.error("Could not save the workspace settings."),
  });

  return (
    <div className="flex flex-col gap-3 rounded-lg border p-4">
      <div>
        <h3 className="text-sm font-medium">Workspace overrides</h3>
        <p className="text-sm text-muted-foreground">
          Leave a field blank to inherit the organisation&apos;s value.
        </p>
      </div>

      <label className="flex flex-col gap-1 text-sm">
        Review rounds
        <input
          className="w-40 rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          inputMode="numeric"
          placeholder="inherit"
          value={roundsValue}
          onChange={(e) => setRounds(e.target.value.replace(/[^0-9]/g, ""))}
        />
        <span className="text-xs text-muted-foreground">
          How many times a reviewer may send work back before it waits for a human.
        </span>
      </label>

      <label className="flex flex-col gap-1 text-sm">
        Moderation model
        <input
          className="w-72 rounded-md border border-input bg-transparent px-2 py-1 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          placeholder="inherit"
          value={moderationValue}
          onChange={(e) => setModeration(e.target.value)}
        />
        <span className="text-xs text-muted-foreground">
          Provider/model that screens authored content, e.g. <code>openai/gpt-5.6-luna</code>.
          Blank inherits; if nothing is set anywhere, content is not screened.
        </span>
      </label>

      <div>
        <Button size="sm" variant="outline" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}
