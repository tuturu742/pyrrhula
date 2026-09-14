import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { DraftWithAssistant } from "./DraftWithAssistant";
import { BehaviorSliders } from "./BehaviorSliders";
import { PersonaScopePicker } from "./PersonaScopePicker";

interface ExistingAgent {
  id: string;
  key: string;
  name: string;
  persona_type: string;
  persona_md: string;
  entity_id: string | null;
  agent_id: string;
  params: { [key: string]: unknown };
}

interface AgentEditorProps {
  workspaceId: string;
  // "Agents" here are model connections (the renamed model_profile) a persona binds to.
  modelProfiles: Array<{ id: string; name: string }>;
  existingAgent?: ExistingAgent;
  onSaved?: () => void;
  onCancel?: () => void;
}

/**
 * D1.5: create/edit one agent -- name, role type (overlay-labelled), persona markdown,
 * model profile link, optional entity link, and a stubbed behavior-profile section
 * (E2.x, Phase 2 -- the layout is reserved, not built).
 */
export function AgentEditor({
  workspaceId,
  modelProfiles,
  existingAgent,
  onSaved,
  onCancel,
}: AgentEditorProps) {
  const queryClient = useQueryClient();
  const isEditing = existingAgent !== undefined;

  const [key, setKey] = useState(existingAgent?.key ?? "");
  const [name, setName] = useState(existingAgent?.name ?? "");
  const [agentRole, setAgentRole] = useState(existingAgent?.persona_type ?? "participant");
  const [personaMd, setPersonaMd] = useState(existingAgent?.persona_md ?? "");
  const [modelProfileId, setModelProfileId] = useState(
    existingAgent?.agent_id ?? modelProfiles[0]?.id ?? "",
  );
  const [entityId, setEntityId] = useState(existingAgent?.entity_id ?? "");
  // Per-persona generation overrides (JSON), merged over the connection's params --
  // distinct voices on a shared connection: a temperature spread, a seed, penalties.
  const [genParams, setGenParams] = useState(() => {
    const existing = existingAgent?.params ?? {};
    return Object.keys(existing).length ? JSON.stringify(existing, null, 2) : "";
  });
  const [genParamsError, setGenParamsError] = useState<string | null>(null);

  function parsedParams(): Record<string, unknown> {
    if (!genParams.trim()) return {};
    try {
      const value = JSON.parse(genParams) as Record<string, unknown>;
      setGenParamsError(null);
      return value;
    } catch {
      setGenParamsError("Must be a JSON object.");
      throw new Error("invalid persona params");
    }
  }

  const save = useMutation({
    mutationFn: async () => {
      if (isEditing) {
        const { data, error } = await apiClient.PATCH("/agents/{persona_id}", {
          params: { path: { persona_id: existingAgent.id } },
          body: {
            name,
            persona_type: agentRole,
            persona_md: personaMd,
            entity_id: entityId.trim() === "" ? null : entityId.trim(),
            agent_id: modelProfileId,
            params: parsedParams(),
          },
        });
        if (error) throw error;
        return data;
      }
      const { data, error } = await apiClient.POST("/agents", {
        body: {
          workspace_id: workspaceId,
          key,
          name,
          persona_type: agentRole,
          persona_md: personaMd,
          entity_id: entityId.trim() === "" ? null : entityId.trim(),
          agent_id: modelProfileId,
          web_search: false,
          params: parsedParams(),
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["agents", workspaceId] });
      onSaved?.();
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!key.trim() || !name.trim() || !modelProfileId) return;
    save.mutate();
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-3 rounded-md border border-border p-4"
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Key (stable identifier)">
          <input
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            disabled={isEditing}
            required
          />
        </Field>
        <Field label="Name">
          <input
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <Field label="Persona type">
          <select
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={agentRole}
            onChange={(e) => setAgentRole(e.target.value)}
          >
            <option value="supervisor">Supervisor</option>
            <option value="participant">Participant</option>
            <option value="informational">Informational (assistant)</option>
          </select>
        </Field>
        <Field label="Connection (model agent)">
          <select
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={modelProfileId}
            onChange={(e) => setModelProfileId(e.target.value)}
            required
          >
            <option value="">(select a model profile)</option>
            {modelProfiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <Field label="Persona (markdown)">
        <textarea
          className="min-h-32 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={personaMd}
          onChange={(e) => setPersonaMd(e.target.value)}
          placeholder="Describe how this agent should speak and act…"
        />
        <DraftWithAssistant
          workspaceId={workspaceId}
          task="draft_persona"
          subject={name || key}
          onDraft={setPersonaMd}
        />
      </Field>

      <Field
        label="Generation overrides (JSON)"
        hint={
          genParamsError ??
          "Merged over the connection's params — e.g. a temperature or seed that keeps " +
            "this persona's voice distinct on a shared connection."
        }
      >
        <textarea
          className="min-h-16 w-full rounded-md border border-input bg-transparent px-2 py-1 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={genParams}
          onChange={(e) => setGenParams(e.target.value)}
          placeholder='{"temperature": 0.9}'
          spellCheck={false}
        />
      </Field>

      <Field
        label="Entity link"
        hint="Link this persona to a character/entity record by id, or leave blank."
      >
        <input
          className="w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={entityId}
          onChange={(e) => setEntityId(e.target.value)}
          placeholder="(none)"
        />
      </Field>

      {isEditing && agentRole === "informational" ? (
        <>
          <fieldset className="rounded-md border border-dashed border-border p-3 opacity-60">
            <legend className="px-1 text-sm font-medium text-muted-foreground">
              Personality dials
            </legend>
            <p className="text-xs text-muted-foreground">
              The assistant is an informational helper — it never takes a session turn and
              never passes through the disclosure gate, so the behavior dials do not apply
              to it. It is always cooperative and never adversarial.
            </p>
          </fieldset>
          <PersonaScopePicker
            workspaceId={workspaceId}
            personaId={existingAgent!.id}
            personaType={agentRole}
          />
        </>
      ) : isEditing ? (
        <>
          <BehaviorSliders personaId={existingAgent!.id} />
          <PersonaScopePicker
            workspaceId={workspaceId}
            personaId={existingAgent!.id}
            personaType={agentRole}
          />
        </>
      ) : (
        <fieldset className="rounded-md border border-dashed border-border p-3 opacity-60">
          <legend className="px-1 text-sm font-medium text-muted-foreground">
            Behavior profile
          </legend>
          <p className="text-xs text-muted-foreground">
            Save the persona first, then set its disposition dials here.
          </p>
        </fieldset>
      )}

      {save.error !== null && (
        <p className="text-sm text-destructive">Failed to save persona.</p>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={save.isPending}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          {isEditing ? "Save changes" : "Create persona"}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-border px-4 py-2 text-sm hover:bg-accent disabled:opacity-50"
          >
            Cancel
          </button>
        )}
      </div>
    </form>
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
