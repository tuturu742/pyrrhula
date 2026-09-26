import { useEffect, useMemo, useState } from "react";
import { BackLink } from "@/components/BackLink";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams, Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import type { ProcessDefinitionDSL, PhaseSpec, StateVarSpec } from "./dsl";
import { blankPhase } from "./dsl";
import { ProcessCanvas, type FieldError } from "./ProcessCanvas";
import { PhaseInspector } from "./PhaseInspector";
import { StateVarsEditor } from "./StateVarsEditor";
import { VersionHistoryPanel } from "./VersionHistoryPanel";

const BUDGET_RATIO_TOLERANCE = 0.02;

function budgetRatioIssues(dsl: ProcessDefinitionDSL): string[] {
  const problems: string[] = [];
  for (const [phaseKey, phase] of Object.entries(dsl.phases)) {
    if (!phase.budget) continue;
    const sum = Object.values(phase.budget.ratio).reduce((a, b) => a + b, 0);
    if (Math.abs(sum - 1.0) > BUDGET_RATIO_TOLERANCE) {
      problems.push(`${phaseKey}: budget ratios sum to ${sum.toFixed(3)}, not ~1.0`);
    }
  }
  return problems;
}

function uniquePhaseKey(phases: Record<string, PhaseSpec>): string {
  let n = 1;
  while (`phase_${n}` in phases) n += 1;
  return `phase_${n}`;
}

/**
 * the editor itself. Two entry modes -- "new" (?template=<key>, from the list
 * page's template gallery, "never an empty canvas") and "edit"
 * (:definitionId, loads that version's document as the working draft; publishing always
 * creates a new version, per its own "publish = new immutable version" convention --
 * there is no in-place update endpoint to call instead).
 */
export function ProcessEditorPage() {
  const { definitionId } = useParams<{ definitionId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isNew = definitionId === undefined;

  const [defKey, setDefKey] = useState("");
  const [defName, setDefName] = useState("");
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [definition, setDefinition] = useState<ProcessDefinitionDSL | null>(null);
  const [selectedPhaseKey, setSelectedPhaseKey] = useState<string | null>(null);
  const [currentVersion, setCurrentVersion] = useState<number | null>(null);
  const [issues, setIssues] = useState<FieldError[]>([]);

  const { data: templates } = useQuery({
    queryKey: ["process-definition-templates"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions/templates");
      if (error) throw error;
      return data;
    },
    enabled: isNew,
  });

  const { data: existing, isError: loadFailed } = useQuery({
    queryKey: ["process-definition", definitionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions/{definition_id}", {
        params: { path: { definition_id: definitionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !isNew,
  });

  const { data: workspaces } = useQuery({
    queryKey: ["workspaces"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workspaces");
      if (error) throw error;
      return data;
    },
  });

  // Seed the working draft once, either from the chosen template (new) or the loaded
  // version (edit) -- never re-seed on refetch, or the author's in-progress edits would
  // vanish out from under them.
  useEffect(() => {
    if (definition !== null) return;
    if (isNew) {
      const templateKey = searchParams.get("template");
      const tpl = templates?.find((t) => t.key === templateKey) ?? templates?.[0];
      if (!tpl) return;
      const dsl = tpl.definition as unknown as ProcessDefinitionDSL;
      setDefinition(dsl);
      setDefKey(tpl.key);
      setDefName(tpl.name);
      setSelectedPhaseKey(dsl.initial_phase);
      const ws = searchParams.get("workspace");
      setWorkspaceId(ws && ws.trim() !== "" ? ws : null);
    } else if (existing) {
      const dsl = existing.definition as unknown as ProcessDefinitionDSL;
      setDefinition(dsl);
      setDefKey(existing.key);
      setDefName(existing.name);
      setWorkspaceId(existing.workspace_id);
      setCurrentVersion(existing.version);
      setSelectedPhaseKey(dsl.initial_phase);
    }
  }, [isNew, templates, existing, definition, searchParams]);

  // Live validation (the dry-run /validate), debounced so every keystroke doesn't
  // fire a request -- surfaced as field-addressed errors on the canvas + inspector.
  useEffect(() => {
    if (!definition) return;
    const handle = setTimeout(() => {
      void apiClient
        .POST("/process-definitions/validate", { body: { definition: definition as unknown as Record<string, unknown> } })
        .then(({ data }) => {
          if (data) setIssues(data.issues.map((i) => ({ fieldPath: i.field_path, message: i.message })));
        });
    }, 500);
    return () => clearTimeout(handle);
  }, [definition]);

  const publish = useMutation({
    mutationFn: async () => {
      if (!definition) throw new Error("no draft");
      const { data, error } = await apiClient.POST("/process-definitions", {
        body: {
          key: defKey,
          name: defName,
          definition: definition as unknown as Record<string, unknown>,
          workspace_id: workspaceId,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (created) => {
      if (!created) return;
      void queryClient.invalidateQueries({ queryKey: ["process-definitions"] });
      void queryClient.invalidateQueries({ queryKey: ["process-definition-versions"] });
      navigate(`/process-definitions/${created.id}`);
    },
  });

  const ratioProblems = useMemo(() => (definition ? budgetRatioIssues(definition) : []), [definition]);
  const canPublish =
    definition !== null && defKey.trim() !== "" && defName.trim() !== "" && ratioProblems.length === 0;

  if (!definition) {
    if (loadFailed) {
      return (
        <div className="flex flex-col items-start gap-3 py-12">
          <BackLink to={"/process-definitions"} label="All definitions" />
          <p className="text-sm text-destructive">
            This item could not be loaded — it may have been archived, or the link is stale.
          </p>
          <Link to="/process-definitions" className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent">
            Back to the definitions list
          </Link>
        </div>
      );
    }
    return <p className="text-muted-foreground">Loading…</p>;
  }

  function updatePhase(phaseKey: string, phase: PhaseSpec) {
    setDefinition((d) => (d ? { ...d, phases: { ...d.phases, [phaseKey]: phase } } : d));
  }

  function renamePhase(oldKey: string, newKey: string) {
    setDefinition((d) => {
      if (!d || !newKey.trim() || newKey === oldKey || newKey in d.phases) return d;
      const phases: Record<string, PhaseSpec> = {};
      for (const [k, p] of Object.entries(d.phases)) {
        const targetKey = k === oldKey ? newKey : k;
        phases[targetKey] = {
          ...p,
          on_complete: p.on_complete === oldKey ? newKey : p.on_complete,
          gates: p.gates?.map((g) => (g.to === oldKey ? { ...g, to: newKey } : g)),
          await: p.await?.on_timeout === oldKey ? { ...p.await, on_timeout: newKey } : p.await,
        };
      }
      const initial_phase = d.initial_phase === oldKey ? newKey : d.initial_phase;
      return { ...d, initial_phase, phases };
    });
    setSelectedPhaseKey((k) => (k === oldKey ? newKey : k));
  }

  function deletePhase(phaseKey: string) {
    setDefinition((d) => {
      if (!d) return d;
      const phases = { ...d.phases };
      delete phases[phaseKey];
      return { ...d, phases };
    });
    setSelectedPhaseKey((k) => (k === phaseKey ? null : k));
  }

  function addPhase() {
    setDefinition((d) => {
      if (!d) return d;
      const key = uniquePhaseKey(d.phases);
      return { ...d, phases: { ...d.phases, [key]: blankPhase() } };
    });
  }

  function setInitial(phaseKey: string) {
    setDefinition((d) => (d ? { ...d, initial_phase: phaseKey } : d));
  }

  function setState(state: Record<string, StateVarSpec>) {
    setDefinition((d) => (d ? { ...d, state } : d));
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">Name</span>
            <input
              className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={defName}
              onChange={(e) => setDefName(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">Key</span>
            <input
              className="rounded-md border border-input bg-transparent px-2 py-1 font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={defKey}
              onChange={(e) => setDefKey(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">Workspace</span>
            <select
              className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={workspaceId ?? ""}
              onChange={(e) => setWorkspaceId(e.target.value === "" ? null : e.target.value)}
            >
              <option value="">(tenant template)</option>
              {workspaces?.map((ws) => (
                <option key={ws.id} value={ws.id}>
                  {ws.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">vocabulary_overlay</span>
            <input
              className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={definition.vocabulary_overlay}
              onChange={(e) =>
                setDefinition((d) => (d ? { ...d, vocabulary_overlay: e.target.value } : d))
              }
            />
          </label>
        </div>
        <div className="flex flex-col items-end gap-1">
          <button
            type="button"
            disabled={!canPublish || publish.isPending}
            onClick={() => publish.mutate()}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {publish.isPending ? "Publishing…" : "Publish new version"}
          </button>
          {currentVersion !== null && (
            <span className="text-xs text-muted-foreground">
              Editing on top of v{currentVersion} -- publishing creates v{currentVersion + 1}
            </span>
          )}
        </div>
      </div>

      {ratioProblems.length > 0 && (
        <ul className="rounded-md border border-destructive bg-destructive/10 p-2 text-sm text-destructive">
          {ratioProblems.map((p, i) => (
            <li key={i}>{p}</li>
          ))}
        </ul>
      )}
      {publish.error !== null && (
        <p className="text-sm text-destructive">Publish failed -- see validation issues below.</p>
      )}
      {issues.length > 0 && (
        <details className="rounded-md border border-border p-2 text-sm">
          <summary className="cursor-pointer text-muted-foreground">
            {issues.length} validation issue(s)
          </summary>
          <ul className="mt-2 flex flex-col gap-1">
            {issues.map((i, idx) => (
              <li key={idx} className="text-destructive">
                <span className="font-mono text-xs">{i.fieldPath}</span>: {i.message}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium">Canvas</h2>
        <button
          type="button"
          onClick={addPhase}
          className="rounded-md border border-border px-3 py-1 text-sm hover:bg-accent disabled:opacity-50"
        >
          + New phase
        </button>
      </div>
      <ProcessCanvas
        definition={definition}
        workspaceId={workspaceId}
        defKey={defKey || "untitled"}
        selectedPhaseKey={selectedPhaseKey}
        onSelectPhase={setSelectedPhaseKey}
        fieldErrors={issues}
      />

      {selectedPhaseKey && definition.phases[selectedPhaseKey] && (
        <PhaseInspector
          definition={definition}
          phaseKey={selectedPhaseKey}
          fieldErrors={issues}
          onChangePhase={updatePhase}
          onRenamePhase={renamePhase}
          onDeletePhase={deletePhase}
          onSetInitial={setInitial}
        />
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-medium">Session state</h2>
        <StateVarsEditor state={definition.state ?? {}} onChange={setState} />
      </section>

      <VersionHistoryPanel
        workspaceId={workspaceId}
        defKey={defKey}
        currentVersion={currentVersion}
        onLoadVersion={(dsl, version) => {
          setDefinition(dsl);
          setCurrentVersion(version);
          setSelectedPhaseKey(dsl.initial_phase);
        }}
      />
    </div>
  );
}
