import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { HolderManager } from "./HolderManager";
import type { components } from "@/lib/api-client/schema";

type SecretResponse = components["schemas"]["SecretResponse"];

const SUBJECT_KINDS = ["entity", "agent", "workspace", "knowledge_entry"] as const;

/**
 * E2.2: the editor for the four faces of a secret (`content`/`gist`/`hint_text`/
 * `behavioral_directive`), subject + scope pickers, holder management, and AI-assisted
 * drafting of the directive/hint (draft-and-approve, never autopilot -- the proposal
 * only ever writes on explicit "Accept & save", a separate `PATCH` call this component
 * makes itself, not something `POST .../draft` does).
 *
 * `secretId === null` is create mode; otherwise this edits the existing secret in place.
 * Content/hint/directive are only ever populated by the backend for a principal passing
 * `secret:author` -- a non-author viewing this component would see blank fields with no
 * way to tell "empty" from "hidden from me", which is a real gap for a shared UI, but
 * this editor is reachable only from the authoring surface (E2.2's stated scope), never
 * offered to a non-author.
 */
export function SecretEditor({
  secretId,
  workspaceId,
  onSaved,
  onCancel,
}: {
  secretId: string | null;
  workspaceId: string;
  onSaved: () => void;
  onCancel?: () => void;
}) {
  const t = useLabel();
  const queryClient = useQueryClient();
  const isEditing = secretId !== null;

  const { data: existing } = useQuery({
    queryKey: ["secret", secretId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/secrets/{secret_id}", {
        params: { path: { secret_id: secretId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: isEditing,
  });

  const [subjectKind, setSubjectKind] = useState<(typeof SUBJECT_KINDS)[number]>("entity");
  const [subjectId, setSubjectId] = useState("");
  const [content, setContent] = useState("");
  const [gist, setGist] = useState("");
  const [hintText, setHintText] = useState("");
  const [behavioralDirective, setBehavioralDirective] = useState("");
  const [scopeKey, setScopeKey] = useState("workspace_public");
  const [publication, setPublication] = useState<"guarded" | "publishable">("guarded");

  useEffect(() => {
    if (!existing) return;
    setSubjectKind(existing.subject_kind as (typeof SUBJECT_KINDS)[number]);
    setSubjectId(existing.subject_id);
    setContent(existing.content ?? "");
    setGist(existing.gist);
    setHintText(existing.hint_text ?? "");
    setBehavioralDirective(existing.behavioral_directive ?? "");
    setScopeKey(existing.scope_key);
    setPublication(existing.publication === "publishable" ? "publishable" : "guarded");
  }, [existing]);

  const save = useMutation({
    mutationFn: async (): Promise<SecretResponse | undefined> => {
      if (isEditing) {
        const { data, error } = await apiClient.PATCH("/secrets/{secret_id}", {
          params: { path: { secret_id: secretId! } },
          body: {
            content,
            gist,
            hint_text: hintText,
            behavioral_directive: behavioralDirective,
            publication,
          },
        });
        if (error) throw error;
        return data;
      }
      const { data, error } = await apiClient.POST("/secrets", {
        body: {
          workspace_id: workspaceId,
          subject_kind: subjectKind,
          subject_id: subjectId,
          content,
          gist,
          scope_key: scopeKey,
          publication,
          hint_text: hintText || undefined,
          behavioral_directive: behavioralDirective || undefined,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["secrets", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["secret", secretId] });
      onSaved();
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!content.trim() || !gist.trim()) return;
    if (!isEditing && !subjectId.trim()) return;
    save.mutate();
  }

  const showEmptyDirectiveWarning =
    behavioralDirective.trim() === "" && content.trim() !== "";

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-3 rounded-md border border-border p-4"
    >
      <h2 className="text-lg font-medium">
        {isEditing ? `Edit ${t("entity.secret")}` : `New ${t("entity.secret")}`}
      </h2>

      {!isEditing && (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Subject kind">
            <select
              className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={subjectKind}
              onChange={(e) => setSubjectKind(e.target.value as (typeof SUBJECT_KINDS)[number])}
            >
              {SUBJECT_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {kind}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Subject id" hint="the entity/agent/workspace/knowledge entry this attaches to">
            <input
              className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={subjectId}
              onChange={(e) => setSubjectId(e.target.value)}
              placeholder={subjectKind === "workspace" ? workspaceId : undefined}
              required
            />
          </Field>
        </div>
      )}

      <Field label="Scope key">
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={scopeKey}
          onChange={(e) => setScopeKey(e.target.value)}
          disabled={isEditing}
        />
      </Field>

      <Field
        label="Publication"
        hint="guarded keeps the plaintext inside this deployment: authors only, and export just into a password-sealed bundle. Publishable says this was written to be handed out -- a character brief, a sample case -- so anyone in the workspace can read it and it can travel in a plain .pyr. It does not change what any agent sees: that is still decided by who holds the secret."
      >
        <select
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={publication}
          onChange={(e) => setPublication(e.target.value as "guarded" | "publishable")}
        >
          <option value="guarded">Guarded (default)</option>
          <option value="publishable">Publishable — written to be shared</option>
        </select>
      </Field>

      <Field
        label="Gist"
        hint="the only field the disclosure gate ever sees -- write it so a small model can judge topical relevance without learning the fact"
      >
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={gist}
          onChange={(e) => setGist(e.target.value)}
          required
        />
      </Field>

      <Field label="Content" hint="the fact itself -- encrypted at rest, visible only to authors">
        <textarea
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          rows={3}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          required
        />
      </Field>

      <Field label="Hint text" hint="a bounded, vague phrase suitable for a partial disclosure">
        <input
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={hintText}
          onChange={(e) => setHintText(e.target.value)}
        />
      </Field>

      <Field
        label="Behavioral directive"
        hint="how a holder acts on this fact without revealing it -- the field that makes concealment produce a motivated agent, not a lobotomised one"
      >
        <textarea
          className="w-full rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          rows={2}
          value={behavioralDirective}
          onChange={(e) => setBehavioralDirective(e.target.value)}
        />
      </Field>
      {showEmptyDirectiveWarning && (
        <p className="text-xs text-amber-600">
          No behavioral directive yet — concealment will produce a flat agent, not a
          motivated one.
        </p>
      )}

      {isEditing && secretId && (
        <DraftAssist
          secretId={secretId}
          onAccepted={(directive, hint) => {
            setBehavioralDirective(directive);
            setHintText(hint);
          }}
        />
      )}

      {save.error !== null && (
        <p className="text-xs text-destructive">Failed to save {t("entity.secret")}.</p>
      )}
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={save.isPending}
          className="self-start rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          {isEditing ? "Save changes" : `Create ${t("entity.secret")}`}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
          >
            Cancel
          </button>
        )}
      </div>

      {isEditing && secretId && (
        <>
          <div className="text-xs text-muted-foreground">
            Disclosure state: {existing?.disclosure_state ?? "…"} · version {existing?.version ?? "…"}
          </div>
          <HolderManager secretId={secretId} />
        </>
      )}
    </form>
  );
}

function DraftAssist({
  secretId,
  onAccepted,
}: {
  secretId: string;
  onAccepted: (directive: string, hint: string) => void;
}) {
  const [modelProfileId, setModelProfileId] = useState("");
  const [proposal, setProposal] = useState<{ behavioral_directive: string; hint_text: string } | null>(
    null,
  );

  const { data: profiles } = useQuery({
    queryKey: ["model-profiles"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles");
      if (error) throw error;
      return data;
    },
  });

  const draft = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/secrets/{secret_id}/draft", {
        params: { path: { secret_id: secretId } },
        body: { agent_id: modelProfileId },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => setProposal(data ?? null),
  });

  return (
    <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
      <h3 className="text-sm font-medium">AI-assisted drafting</h3>
      <p className="text-xs text-muted-foreground">
        Proposes a directive and a hint from the current content. Nothing saves until you
        accept below.
      </p>
      <div className="flex items-center gap-2">
        <select
          className="flex-1 rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={modelProfileId}
          onChange={(e) => setModelProfileId(e.target.value)}
        >
          <option value="">Select a model profile…</option>
          {profiles?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!modelProfileId || draft.isPending}
          onClick={() => {
            setProposal(null);
            draft.mutate();
          }}
          className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
        >
          {draft.isPending ? "Drafting…" : "Draft with AI"}
        </button>
      </div>
      {draft.error !== null && (
        <p className="text-xs text-destructive">
          Drafting failed — the model's draft may have echoed the secret's content
          verbatim and was discarded, or the call itself failed.
        </p>
      )}
      {proposal && (
        <div className="flex flex-col gap-2 rounded-md bg-muted p-2">
          <div className="text-xs">
            <span className="font-medium">Proposed directive: </span>
            {proposal.behavioral_directive}
          </div>
          <div className="text-xs">
            <span className="font-medium">Proposed hint: </span>
            {proposal.hint_text}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => {
                onAccepted(proposal.behavioral_directive, proposal.hint_text);
                setProposal(null);
              }}
              className="rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground"
            >
              Accept into form
            </button>
            <button
              type="button"
              onClick={() => setProposal(null)}
              className="rounded-md border border-border px-2 py-1 text-xs"
            >
              Discard
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </label>
  );
}
