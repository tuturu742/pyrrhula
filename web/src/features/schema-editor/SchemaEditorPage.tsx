import { useEffect, useState } from "react";
import { BackLink } from "@/components/BackLink";
import { toast } from "sonner";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams, Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import type { CreateSchemaFailure, ValidateSchemaResult } from "./api";
import { createSchema, getSchema, listSchemaTemplates, validateSchema } from "./api";
import { blankSchemaDefinition, type EntitySchemaDefinitionDoc, type SchemaValidationIssue } from "./types";
import { FieldListEditor } from "./FieldListEditor";
import { FsmEditor } from "./FsmEditor";
import { SchemaVersionHistoryPanel } from "./SchemaVersionHistoryPanel";

/**
 * the editor: two entry modes, matching `ProcessEditorPage`'s own split -- "new"
 * (`?template=<key>`, from `SchemaListPage`'s gallery, "never an empty schema" per
 * ) and "edit" (`:schemaId`, loads that version's document as the working draft).
 * Saving always creates a new immutable version (`entity_schema` has no in-place
 * update); there is no "publish" step distinct from save, since an entity schema has no
 * draft/published split the way a process definition might.
 */
export function SchemaEditorPage() {
  const { schemaId } = useParams<{ schemaId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isNew = schemaId === undefined;

  const [defKey, setDefKey] = useState("");
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [definition, setDefinition] = useState<EntitySchemaDefinitionDoc | null>(null);
  const [currentVersion, setCurrentVersion] = useState<number | null>(null);
  const [issues, setIssues] = useState<SchemaValidationIssue[]>([]);

  const { data: templates } = useQuery({
    queryKey: ["entity-schema-templates"],
    queryFn: listSchemaTemplates,
    enabled: isNew,
  });

  const { data: existing, isError: loadFailed } = useQuery({
    queryKey: ["entity-schema", schemaId],
    queryFn: () => getSchema(schemaId!),
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

  // Seed the working draft once -- from the chosen template, "start from blank"'s
  // guided minimal template, or the loaded version -- never re-seed on refetch.
  useEffect(() => {
    if (definition !== null) return;
    if (isNew) {
      const templateKey = searchParams.get("template");
      if (templateKey) {
        const tpl = templates?.find((t) => t.key === templateKey);
        if (!tpl) return;
        setDefinition(tpl.definition);
        setDefKey(tpl.key);
      } else if (templates !== undefined) {
        // No ?template= at all means "start from blank" was chosen explicitly --
        // still not an empty field list.
        setDefinition(blankSchemaDefinition());
      } else {
        return;
      }
      const ws = searchParams.get("workspace");
      setWorkspaceId(ws && ws.trim() !== "" ? ws : null);
    } else if (existing) {
      setDefinition(existing.definition);
      setDefKey(existing.key);
      setWorkspaceId(existing.workspace_id);
      setCurrentVersion(existing.version);
    }
  }, [isNew, templates, existing, definition, searchParams]);

  // Live validation, debounced -- its own acceptance criterion: an invalid CEL
  // expression surfaces a live, expression-anchored error before save is even attempted.
  useEffect(() => {
    if (!definition) return;
    const handle = setTimeout(() => {
      void validateSchema(definition).then((result: ValidateSchemaResult) => setIssues(result.issues));
    }, 500);
    return () => clearTimeout(handle);
  }, [definition]);

  const save = useMutation({
    mutationFn: async () => {
      if (!definition) throw new Error("no draft");
      return createSchema(defKey, workspaceId, definition);
    },
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["entity-schemas"] });
      void queryClient.invalidateQueries({ queryKey: ["entity-schema-versions"] });
      navigate(`/schemas/${created.id}`);
    },
    onError: (error: unknown) => {
      const failure = error as CreateSchemaFailure;
      if (failure?.detail?.issues) setIssues(failure.detail.issues);
      else toast.error("That didn't save — please try again.");
    },
  });

  const canSave = definition !== null && defKey.trim() !== "" && issues.length === 0;

  if (!definition) {
    if (loadFailed) {
      return (
        <div className="flex flex-col items-start gap-3 py-12">
          <BackLink to={"/schemas"} label="All schemas" />
          <p className="text-sm text-destructive">
            This item could not be loaded — it may have been archived, or the link is stale.
          </p>
          <Link to="/schemas" className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent">
            Back to the schemas list
          </Link>
        </div>
      );
    }
    return <p className="text-muted-foreground">Loading…</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-wrap items-end gap-3">
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
        </div>
        <div className="flex flex-col items-end gap-1">
          <button
            type="button"
            disabled={!canSave || save.isPending}
            onClick={() => save.mutate()}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {save.isPending ? "Saving…" : "Save new version"}
          </button>
          {currentVersion !== null && (
            <span className="text-xs text-muted-foreground">
              Editing on top of v{currentVersion} -- saving creates v{currentVersion + 1}
            </span>
          )}
        </div>
      </div>

      {issues.length > 0 && (
        <details open className="rounded-md border border-destructive bg-destructive/10 p-2 text-sm">
          <summary className="cursor-pointer text-destructive">{issues.length} validation issue(s)</summary>
          <ul className="mt-2 flex flex-col gap-1">
            {issues.map((i, idx) => (
              <li key={idx} className="text-destructive">
                <span className="font-mono text-xs">{i.field_path}</span>: {i.message}
              </li>
            ))}
          </ul>
        </details>
      )}

      <FieldListEditor
        fields={definition.fields}
        derived={definition.derived}
        constraints={definition.constraints}
        issues={issues}
        onChangeFields={(fields) => setDefinition((d) => (d ? { ...d, fields } : d))}
        onChangeDerived={(derived) => setDefinition((d) => (d ? { ...d, derived } : d))}
        onChangeConstraints={(constraints) => setDefinition((d) => (d ? { ...d, constraints } : d))}
      />

      <section className="flex flex-col gap-2">
        <h2 className="text-lg font-medium">State machines</h2>
        <FsmEditor
          stateMachines={definition.state_machines}
          issues={issues}
          onChange={(state_machines) => setDefinition((d) => (d ? { ...d, state_machines } : d))}
        />
      </section>

      <SchemaVersionHistoryPanel
        workspaceId={workspaceId}
        defKey={defKey}
        currentVersion={currentVersion}
        onLoadVersion={(loadedDefinition, version) => {
          setDefinition(loadedDefinition);
          setCurrentVersion(version);
        }}
      />
    </div>
  );
}
