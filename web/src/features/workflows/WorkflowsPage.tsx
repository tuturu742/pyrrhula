import { useMemo, useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { DEFAULT_LABELS } from "@/lib/vocabulary/labels";

/**
 * Moddable workflows: list the system templates + this tenant's own workflows, select the
 * tenant's current one, and author custom workflows (labels, persona names, featured flows,
 * repo access). System templates are read-only — the Clone action seeds a new editable one.
 */

interface Workflow {
  key: string;
  name: string;
  is_system: boolean;
  overlay_key?: string | null;
  persona_type_labels?: Record<string, string>;
  label_overrides?: Record<string, string>;
  featured_process_keys?: string[];
  repo_access?: boolean;
}

const COMMON_LABEL_KEYS = [
  "entity.workspace",
  "entity.session",
  "entity.knowledge_source",
  "role.facilitator",
  "role.participant",
  "role.overseer",
  "phase.framing",
  "phase.discussion",
  "phase.synthesis",
];

export function WorkflowsPage() {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<Workflow | null>(null);
  const [creating, setCreating] = useState(false);

  const { data: workflows, isLoading: pageLoading, isError: pageError } = useQuery({
    queryKey: ["workflows"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workflows");
      if (error) throw error;
      return data as Workflow[];
    },
  });

  const { data: current } = useQuery({
    queryKey: ["workflow-current"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workflows/current");
      if (error) throw error;
      return data;
    },
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["workflows"] });
    queryClient.invalidateQueries({ queryKey: ["workflow-current"] });
  };

  const selectWorkflow = useMutation({
    mutationFn: async (workflow_key: string | null) => {
      const { error } = await apiClient.PUT("/workflows/current", { body: { workflow_key } });
      if (error) throw error;
    },
    onSuccess: invalidate,
  });

  const deleteWorkflow = useMutation({
    mutationFn: async (key: string) => {
      const { error } = await apiClient.DELETE("/workflows/{key}", {
        params: { path: { key } },
      });
      if (error) throw error;
    },
    onSuccess: invalidate,
  });

  if (pageLoading) {
    return (
      <div className="flex flex-col gap-3 py-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }
  if (pageError) {
    return <p className="py-8 text-sm text-destructive">This page could not load — please refresh or try again.</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Workflows</h1>
        <button
          type="button"
          onClick={() => {
            setEditing(null);
            setCreating(true);
          }}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
        >
          New workflow
        </button>
      </div>

      <section className="flex flex-col gap-2">
        {workflows?.map((w) => {
          const isCurrent = current?.workflow_key === w.key;
          return (
            <div
              key={`${w.is_system ? "sys" : "own"}-${w.key}`}
              className="flex items-center justify-between rounded-md border border-border px-4 py-3"
            >
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">{w.name}</span>
                <span className="font-mono text-xs text-muted-foreground">{w.key}</span>
                {w.is_system && (
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-xs">system</span>
                )}
                {w.repo_access && (
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-xs">repo access</span>
                )}
                {isCurrent && (
                  <span className="rounded bg-primary/15 px-1.5 py-0.5 text-xs font-medium">
                    current
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2 text-sm">
                {!isCurrent && (
                  <button
                    type="button"
                    onClick={() => selectWorkflow.mutate(w.key)}
                    disabled={selectWorkflow.isPending}
                    className="rounded-md border border-border px-2.5 py-1 text-xs disabled:opacity-50"
                  >
                    Use this
                  </button>
                )}
                {w.is_system ? (
                  <button
                    type="button"
                    onClick={() => {
                      setEditing({ ...w, is_system: false, key: `my-${w.key}`, name: `My ${w.name}` });
                      setCreating(true);
                    }}
                    className="rounded-md border border-border px-2.5 py-1 text-xs"
                  >
                    Clone
                  </button>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={() => {
                        setEditing(w);
                        setCreating(false);
                      }}
                      className="rounded-md border border-border px-2.5 py-1 text-xs"
                    >
                      Edit
                    </button>
                    <ConfirmButton
                title="Delete"
                description={`Delete workflow "${w.name}"?`}
                confirmLabel="Delete"
                destructive
                onConfirm={() => deleteWorkflow.mutate(w.key)}
              >
                <button
                      type="button"
                      disabled={isCurrent || deleteWorkflow.isPending}
                      className="rounded-md border border-destructive/50 px-2.5 py-1 text-xs text-destructive disabled:opacity-50"
                    >
                      Delete
                    </button>
              </ConfirmButton>
                  </>
                )}
              </div>
            </div>
          );
        })}
        {selectWorkflow.isError && (
          <p className="text-xs text-destructive">
            Could not switch: {(selectWorkflow.error as Error)?.message ?? "not permitted"}
          </p>
        )}
      </section>

      {(creating || editing) && (
        <WorkflowEditor
          seed={editing}
          isNew={creating}
          onDone={() => {
            setEditing(null);
            setCreating(false);
            invalidate();
          }}
          onCancel={() => {
            setEditing(null);
            setCreating(false);
          }}
        />
      )}
    </div>
  );
}

function DictEditor({
  value,
  onChange,
  keySuggestions,
  keyPlaceholder,
  valuePlaceholder,
}: {
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  keySuggestions?: string[];
  keyPlaceholder: string;
  valuePlaceholder: string;
}) {
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const entries = Object.entries(value);
  const listId = useMemo(() => `dict-${Math.random().toString(36).slice(2, 8)}`, []);

  return (
    <div className="flex flex-col gap-1.5">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-center gap-2">
          <span className="w-56 shrink-0 truncate font-mono text-xs text-muted-foreground">{k}</span>
          <input
            className="flex-1 rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={v}
            onChange={(e) => onChange({ ...value, [k]: e.target.value })}
          />
          <button
            type="button"
            onClick={() => {
              const next = { ...value };
              delete next[k];
              onChange(next);
            }}
            className="text-xs text-muted-foreground"
          >
            remove
          </button>
        </div>
      ))}
      <div className="flex items-center gap-2">
        <input
          className="w-56 shrink-0 rounded-md border border-input bg-transparent px-2 py-1 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder={keyPlaceholder}
          list={keySuggestions ? listId : undefined}
        />
        {keySuggestions && (
          <datalist id={listId}>
            {keySuggestions.map((k) => (
              <option key={k} value={k} />
            ))}
          </datalist>
        )}
        <input
          className="flex-1 rounded-md border border-input bg-transparent px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          placeholder={valuePlaceholder}
        />
        <button
          type="button"
          disabled={!newKey.trim() || !newValue.trim()}
          onClick={() => {
            onChange({ ...value, [newKey.trim()]: newValue });
            setNewKey("");
            setNewValue("");
          }}
          className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
        >
          add
        </button>
      </div>
    </div>
  );
}

function WorkflowEditor({
  seed,
  isNew,
  onDone,
  onCancel,
}: {
  seed: Workflow | null;
  isNew: boolean;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [key, setKey] = useState(seed?.key ?? "");
  const [name, setName] = useState(seed?.name ?? "");
  const [personaLabels, setPersonaLabels] = useState<Record<string, string>>(
    seed?.persona_type_labels ?? {},
  );
  const [labelOverrides, setLabelOverrides] = useState<Record<string, string>>(
    seed?.label_overrides ?? {},
  );
  const [featured, setFeatured] = useState<Set<string>>(
    new Set(seed?.featured_process_keys ?? []),
  );
  const [repoAccess, setRepoAccess] = useState<boolean>(seed?.repo_access ?? false);

  const { data: definitions } = useQuery({
    queryKey: ["all-process-definitions"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions", {
        params: { query: {} },
      });
      if (error) throw error;
      const keys = new Set<string>();
      for (const d of data ?? []) keys.add(d.key);
      return [...keys].sort();
    },
  });

  const save = useMutation({
    mutationFn: async () => {
      const shared = {
        name,
        persona_type_labels: personaLabels,
        label_overrides: labelOverrides,
        featured_process_keys: [...featured],
        repo_access: repoAccess,
      };
      if (isNew) {
        const { error } = await apiClient.POST("/workflows", {
          body: { key, overlay_key: seed?.overlay_key ?? null, ...shared },
        });
        if (error) throw error;
      } else {
        const { error } = await apiClient.PATCH("/workflows/{key}", {
          params: { path: { key } },
          body: { ...shared, clear_overlay: false },
        });
        if (error) throw error;
      }
    },
    onSuccess: onDone,
  });

  return (
    <section className="flex flex-col gap-4 rounded-md border border-border p-4">
      <h2 className="text-lg font-medium">{isNew ? "New workflow" : `Edit ${name}`}</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Name</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Key</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 font-mono disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={key}
            onChange={(e) => setKey(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "-"))}
            disabled={!isNew}
            placeholder="my-workflow"
          />
        </label>
      </div>

      <div className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Persona type names</span>
        <span className="text-xs text-muted-foreground">
          What the supervisor / participant roles are called in this workflow.
        </span>
        <DictEditor
          value={personaLabels}
          onChange={setPersonaLabels}
          keySuggestions={["supervisor", "participant"]}
          keyPlaceholder="supervisor"
          valuePlaceholder="Game Master"
        />
      </div>

      <div className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Label overrides</span>
        <span className="text-xs text-muted-foreground">
          Rename anything in the UI (applied when this workflow is selected).
        </span>
        <DictEditor
          value={labelOverrides}
          onChange={setLabelOverrides}
          keySuggestions={[...COMMON_LABEL_KEYS, ...Object.keys(DEFAULT_LABELS)]}
          keyPlaceholder="entity.session"
          valuePlaceholder="Meeting"
        />
      </div>

      <div className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Featured flows</span>
        <span className="text-xs text-muted-foreground">
          Process definitions shown first when starting a session.
        </span>
        <div className="flex max-h-32 flex-col gap-1 overflow-auto rounded-md border border-input p-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
          {definitions?.length === 0 && (
            <span className="text-xs text-muted-foreground">No definitions yet.</span>
          )}
          {definitions?.map((k) => (
            <label key={k} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={featured.has(k)}
                onChange={() =>
                  setFeatured((prev) => {
                    const next = new Set(prev);
                    if (next.has(k)) next.delete(k);
                    else next.add(k);
                    return next;
                  })
                }
              />
              <span className="font-mono text-xs">{k}</span>
            </label>
          ))}
        </div>
      </div>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={repoAccess}
          onChange={(e) => setRepoAccess(e.target.checked)}
        />
        <span className="font-medium">Repo access</span>
        <span className="text-xs text-muted-foreground">
          Sessions under this workflow may select registered repos and delegate coding work.
        </span>
      </label>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={!name.trim() || (isNew && key.trim().length < 2) || save.isPending}
          onClick={() => save.mutate()}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-4 py-2 text-sm hover:bg-accent disabled:opacity-50"
        >
          Cancel
        </button>
        {save.isError && (
          <span className="text-xs text-destructive">
            {(save.error as Error)?.message ?? "could not save"}
          </span>
        )}
      </div>
    </section>
  );
}
