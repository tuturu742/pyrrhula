import { useState } from "react";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { useWorkspaceVocabulary } from "@/lib/vocabulary/useWorkspaceVocabulary";
import { useDirectorViewAccess } from "@/features/director-view/useDirectorViewAccess";
import { SessionStatusDot } from "@/features/session/SessionStatusDot";
import { SetupChecklist } from "./SetupChecklist";
import { AutoMergeCard, ConductRulesCard, SecretsGateCard, InheritedSettingsCard } from "./SecretsGateCard";
import { MembersCard } from "./MembersCard";
import { ClockCard } from "./ClockCard";
import { McpServersCard } from "./McpServersCard";
import { VisibilityCard } from "./VisibilityCard";
import { KnowledgeBudgetCard } from "./KnowledgeBudgetCard";
import { VocabularySwitcher } from "@/lib/vocabulary/VocabularySwitcher";
import { relativeTime, sessionDisplayName } from "@/features/session/session-format";

/**
 * Session launch surface. A session is created from an explicit roster: one supervisor
 * persona hosts, and one or more participant personas join. The interpreter resolves each
 * phase's actors from this roster (by persona_type), so only the selected personas act. An
 * optional agenda steers the supervisor.
 */
export function WorkspaceDetailPage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const navigate = useNavigate();
  const t = useLabel();
  const queryClient = useQueryClient();
  useWorkspaceVocabulary(workspaceId);
  const { data: directorAccess } = useDirectorViewAccess(workspaceId);

  const [definitionId, setDefinitionId] = useState<string>("");
  const [supervisorId, setSupervisorId] = useState<string>("");
  const [participantIds, setParticipantIds] = useState<Set<string>>(new Set());
  const [agenda, setAgenda] = useState<string>("");
  const [sessionName, setSessionName] = useState<string>("");
  const [turnPolicy, setTurnPolicy] = useState<"auto" | "directed">("auto");
  const [repoIds, setRepoIds] = useState<Set<string>>(new Set());
  // Sessions is the page's job; configuration lives behind the Settings tab so the
  // default view is what people came for (user feedback: the cards buried it).
  const [tab, setTab] = useState<"sessions" | "settings">("sessions");

  const { data: personas, isLoading } = useQuery({
    queryKey: ["workspace-personas", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/agents", {
        params: { query: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!workspaceId,
  });

  const { data: currentWorkflow } = useQuery({
    queryKey: ["workflow-current"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/workflows/current");
      if (error) throw error;
      return data;
    },
  });
  const repoAccess = currentWorkflow?.workflow?.repo_access ?? false;

  const { data: repos } = useQuery({
    queryKey: ["repos"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos");
      if (error) throw error;
      return data;
    },
    enabled: repoAccess,
  });

  const { data: definitions } = useQuery({
    queryKey: ["launchable-process-definitions", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/process-definitions", {
        params: { query: {} },
      });
      if (error) throw error;
      const latestByKey = new Map<string, NonNullable<typeof data>[number]>();
      for (const d of data ?? []) {
        if (d.workspace_id !== null && d.workspace_id !== workspaceId) continue;
        const existing = latestByKey.get(d.key);
        if (!existing || existing.version < d.version) latestByKey.set(d.key, d);
      }
      return [...latestByKey.values()];
    },
    enabled: !!workspaceId,
  });

  // The tenant workflow's featured flows come first in the picker.
  const featured = currentWorkflow?.workflow?.featured_process_keys ?? [];
  const orderedDefinitions = [...(definitions ?? [])].sort((a, b) => {
    const fa = featured.includes(a.key) ? 0 : 1;
    const fb = featured.includes(b.key) ? 0 : 1;
    return fa - fb || a.name.localeCompare(b.name);
  });

  const supervisors = (personas ?? []).filter((p) => p.persona_type === "supervisor");
  const participants = (personas ?? []).filter((p) => p.persona_type === "participant");
  const effectiveSupervisor = supervisorId || supervisors[0]?.id || "";
  const canStart = Boolean(definitionId && effectiveSupervisor && participantIds.size > 0);

  function toggleParticipant(id: string) {
    setParticipantIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const createSession = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/sessions", {
        body: {
          workspace_id: workspaceId!,
          supervisor_persona_id: effectiveSupervisor,
          participant_persona_ids: [...participantIds],
          agenda_md: agenda.trim() === "" ? null : agenda,
          name: sessionName.trim() === "" ? null : sessionName.trim(),
          turn_policy: turnPolicy,
          process_definition_id: definitionId,
          repo_ids: [...repoIds],
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (session) => {
      if (session) navigate(`/sessions/${session.id}`);
    },
  });

  const { data: sessions } = useQuery({
    queryKey: ["workspace-sessions", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions", {
        params: { query: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!workspaceId,
    // Keep the traffic-lights honest: activity is time-derived, so refresh gently.
    refetchInterval: 10000,
  });

  const archiveSession = useMutation({
    mutationFn: async (sessionId: string) => {
      const { error } = await apiClient.DELETE("/sessions/{session_id}", {
        params: { path: { session_id: sessionId } },
      });
      if (error) throw error;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["workspace-sessions", workspaceId] }),
  });

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{t("entity.workspace")}</h1>
        <div className="flex items-center gap-3">
          <Link
            to={`/workspaces/${workspaceId}/agents`}
            className="rounded-md border border-border px-3 py-1.5 text-sm"
          >
            Manage personas
          </Link>
          <Link
            to={`/workspaces/${workspaceId}/entities`}
            className="rounded-md border border-border px-3 py-1.5 text-sm"
          >
            Entities
          </Link>
          <Link
            to={`/workspaces/${workspaceId}/secrets`}
            className="rounded-md border border-border px-3 py-1.5 text-sm"
          >
            Manage {t("entity.secret")}s
          </Link>
          {directorAccess?.can_inspect && (
            <Link
              to={`/workspaces/${workspaceId}/director-view`}
              className="rounded-md border border-border px-3 py-1.5 text-sm"
            >
              {t("role.overseer")} view
            </Link>
          )}
          {repoAccess && (
            <Link
              to={`/workspaces/${workspaceId}/repo-graph`}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
            >
              Repo graph
            </Link>
          )}
          <Link
            to={`/workspaces/${workspaceId}/export`}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
          >
            Export / import
          </Link>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        {(["sessions", "settings"] as const).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={
              "rounded-t-md px-4 py-2 text-sm " +
              (tab === key
                ? "border border-b-0 border-border bg-background font-medium"
                : "text-muted-foreground hover:text-foreground")
            }
          >
            {key === "sessions" ? `${t("entity.session")}s` : "Settings"}
          </button>
        ))}
      </div>

      {tab === "settings" && (
        <>
          <SecretsGateCard workspaceId={workspaceId!} />
          <ConductRulesCard workspaceId={workspaceId!} />
          <AutoMergeCard workspaceId={workspaceId!} />
          <InheritedSettingsCard workspaceId={workspaceId!} />
          <MembersCard workspaceId={workspaceId!} />
          <VisibilityCard workspaceId={workspaceId!} />
          <KnowledgeBudgetCard workspaceId={workspaceId!} />
          <McpServersCard workspaceId={workspaceId!} />
          <div className="flex flex-wrap items-center gap-4">
            <VocabularySwitcher workspaceId={workspaceId!} />
            <ClockCard workspaceId={workspaceId!} />
          </div>
        </>
      )}

      {tab === "sessions" && (
        <>
      <SetupChecklist workspaceId={workspaceId!} />
      <section className="flex flex-col gap-4 rounded-md border border-border p-4">
        <h2 className="text-lg font-medium">Start a {t("entity.session")}</h2>

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Process definition</span>
          <select
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={definitionId}
            onChange={(e) => setDefinitionId(e.target.value)}
          >
            <option value="">(choose a flow…)</option>
            {orderedDefinitions.map((d) => (
              <option key={d.id} value={d.id}>
                {featured.includes(d.key) ? "★ " : ""}{d.name} (v{d.version})
              </option>
            ))}
          </select>
          <span className="text-xs text-muted-foreground">
            <Link to="/process-definitions" className="underline">
              Manage definitions
            </Link>
          </span>
        </label>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Supervisor (hosts the session)</span>
            <select
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={effectiveSupervisor}
              onChange={(e) => setSupervisorId(e.target.value)}
              disabled={supervisors.length === 0}
            >
              {supervisors.length === 0 && <option value="">(no supervisor persona)</option>}
              {supervisors.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>

          <div className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Participants (pick one or more)</span>
            <div className="flex max-h-40 flex-col gap-1 overflow-auto rounded-md border border-input p-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
              {participants.length === 0 && (
                <span className="text-xs text-muted-foreground">No participant personas yet.</span>
              )}
              {participants.map((p) => (
                <label key={p.id} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={participantIds.has(p.id)}
                    onChange={() => toggleParticipant(p.id)}
                  />
                  {p.name}
                </label>
              ))}
            </div>
          </div>
        </div>

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Flow mode</span>
          <select
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={turnPolicy}
            onChange={(e) => setTurnPolicy(e.target.value as "auto" | "directed")}
          >
            <option value="auto">Autonomous — personas run the discussion themselves</option>
            <option value="directed">Managed — you conduct each turn</option>
          </select>
          <span className="text-xs text-muted-foreground">
            You can switch this live from inside the session.
          </span>
        </label>

        {repoAccess && (
          <div className="flex flex-col gap-1 text-sm">
            <span className="font-medium">Repos for this session</span>
            <span className="text-xs text-muted-foreground">
              Delegated coding work may only target the repos selected here.
            </span>
            <div className="flex max-h-32 flex-col gap-1 overflow-auto rounded-md border border-input p-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
              {(repos ?? []).length === 0 && (
                <span className="text-xs text-muted-foreground">
                  No repos registered yet — see the Repos page.
                </span>
              )}
              {repos?.map((r) => (
                <label key={r.id} className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={repoIds.has(r.id)}
                    onChange={() =>
                      setRepoIds((prev) => {
                        const next = new Set(prev);
                        if (next.has(r.id)) next.delete(r.id);
                        else next.add(r.id);
                        return next;
                      })
                    }
                  />
                  {r.name}
                  <span className="font-mono text-xs text-muted-foreground">{r.key}</span>
                </label>
              ))}
            </div>
          </div>
        )}

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Name (optional)</span>
          <input
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={sessionName}
            onChange={(e) => setSessionName(e.target.value)}
            placeholder="e.g. Campaign kickoff — round 2"
          />
        </label>

        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Agenda (optional)</span>
          <textarea
            className="min-h-20 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={agenda}
            onChange={(e) => setAgenda(e.target.value)}
            placeholder="What should the supervisor drive the session toward? e.g.&#10;1. Frame the problem&#10;2. Gather each participant's proposal&#10;3. Decide"
          />
        </label>

        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={!canStart || createSession.isPending}
            onClick={() => createSession.mutate()}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {createSession.isPending ? "Starting…" : `Start ${t("entity.session")}`}
          </button>
          {!definitionId && (
            <span className="text-xs text-muted-foreground">Choose a process definition.</span>
          )}
          {definitionId && supervisors.length === 0 && (
            <span className="text-xs text-destructive">Add a supervisor persona to host.</span>
          )}
          {definitionId && effectiveSupervisor && participantIds.size === 0 && (
            <span className="text-xs text-destructive">Select at least one participant.</span>
          )}
        </div>
        {createSession.isError && (
          <p className="text-xs text-destructive">
            Could not start: {(createSession.error as Error)?.message ?? "unknown error"}
          </p>
        )}
        {isLoading && <span className="text-xs text-muted-foreground">Loading personas…</span>}
      </section>

      <section className="flex flex-col gap-3 rounded-md border border-border p-4">
        <h2 className="text-lg font-medium">{t("entity.session")}s</h2>
        {sessions?.length === 0 && (
          <p className="text-sm text-muted-foreground">No {t("entity.session")}s yet.</p>
        )}
        <ul className="flex flex-col gap-2">
          {sessions?.map((s) => (
            <li key={s.id} className="flex items-center gap-2">
              <Link
                to={`/sessions/${s.id}`}
                className="flex flex-1 items-center justify-between gap-3 rounded-md border border-border px-4 py-2 hover:bg-accent"
              >
                <span className="flex min-w-0 items-center gap-2.5">
                  <SessionStatusDot activity={s.activity} />
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium">
                      {sessionDisplayName(s)}
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {s.created_at &&
                        new Date(s.created_at).toLocaleString([], {
                          dateStyle: "medium",
                          timeStyle: "short",
                        })}
                      {s.turn_policy === "directed" ? " · managed" : ""}
                    </span>
                  </span>
                </span>
                <span className="shrink-0 text-right text-xs text-muted-foreground">
                  <span className="block">{t(`phase.${s.current_phase}`)}</span>
                  <span className="block">
                    {s.activity === "running"
                      ? "active now"
                      : s.last_event_at
                        ? `last activity ${relativeTime(s.last_event_at)}`
                        : s.activity === "stopped"
                          ? "finished"
                          : "waiting"}
                  </span>
                </span>
              </Link>
              <ConfirmButton
                title="Archive"
                description={`Archive this ${t("entity.session")}? Reversible, not a permanent delete.`}
                confirmLabel="Archive"
                destructive
                onConfirm={() => archiveSession.mutate(s.id)}
              >
                <button
                type="button"
                disabled={archiveSession.isPending}
                className="shrink-0 rounded-md border border-destructive/50 px-3 py-1 text-sm text-destructive disabled:opacity-50"
              >
                Archive
              </button>
              </ConfirmButton>
            </li>
          ))}
        </ul>
      </section>
        </>
      )}
    </div>
  );
}
