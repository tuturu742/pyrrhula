import { useState } from "react";
import { toast } from "sonner";
import { BackLink } from "@/components/BackLink";
import { Markdown } from "@/components/Markdown";
import { NameDialog } from "@/components/NameDialog";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useSSE, type SSEMessageEvent } from "@/lib/sse/useSSE";
import { useLabel } from "@/lib/vocabulary/useLabel";
import { useWorkspaceVocabulary } from "@/lib/vocabulary/useWorkspaceVocabulary";
import { PhaseBanner } from "./PhaseBanner";
import { WorkPanel } from "./WorkPanel";
import { CharactersPanel } from "./CharactersPanel";
import { ReportPanel } from "./ReportPanel";
import { SessionStatusDot } from "./SessionStatusDot";
import { relativeTime, sessionDisplayName } from "./session-format";
import { ResolutionWidget } from "./ResolutionWidget";
import { OverrideBadge } from "./OverrideBadge";
import { ContextInspectorPanel } from "@/features/context-inspector/ContextInspectorPanel";
import {
  absoluteUrl,
  usePreviewActions,
  usePreviews,
  useRepoPullRequests,
} from "@/features/previews/usePreviews";

/** How a turn came to happen -- shown as a small chip so a reader can tell an
 * autonomous turn from one a person (or a script) asked for. */
const TRIGGER_LABELS: Record<string, string> = {
  scheduler: "auto",
  conducted: "conducted",
  driver: "driver",
};
const TRIGGER_HINTS: Record<string, string> = {
  scheduler: "The flow's scheduler chose this speaker autonomously.",
  conducted: "A person asked for this persona's turn.",
  driver: "An automated conductor (script or benchmark) requested this turn.",
};

interface MessagePayload {
  id?: string;
  role: "user" | "assistant";
  content: string;
  tool_calls_made?: number;
  /** Author display name: the persona's name (or the human's), carried by the backend so
   * the log shows who actually spoke instead of a generic role label. */
  author?: string;
  /** ISO timestamp stamped when the turn was committed (present on replay too). */
  created_at?: string;
  /** What caused this turn: the autonomous scheduler, a human conducting, or an
   * automated driver (benchmark/script). Makes the transcript self-describing. */
  triggered_by?: "scheduler" | "conducted" | "driver" | "unknown" | string;
}

/** a `human_override` event carries the same fields a `message` does, plus who/how. */
interface HumanOverridePayload extends MessagePayload {
  agent_id: string;
  rewrite_applied: boolean;
}

interface PhaseTransitionPayload {
  from: string;
  to: string | null;
}

interface AwaitPayload {
  outcome: "satisfied" | "timed_out";
  to: string | null;
}

interface ErrorPayload {
  message: string;
}

/** A call to a registered MCP server, recorded at the moment it happened. The model's
 * request and the server's answer (bounded), or why there was no answer -- so a reader
 * can check what a persona narrates against what its tool actually said. */
interface ToolCallPayload {
  server_key: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  outcome: "completed" | "failed" | "refused" | string;
  author?: string;
  created_at?: string;
  result?: string;
  result_truncated?: boolean;
  message?: string;
}

const TOOL_OUTCOME_CLASS: Record<string, string> = {
  completed: "text-emerald-600",
  failed: "text-destructive",
  refused: "text-amber-600",
};

interface ExecEnvironmentPayload {
  action: string; // created | reused
  noun: string; // container | pod | Fargate task | environment
  name: string;
  image?: string;
  engine?: string;
  task?: string;
  actor?: string;
  url?: string;
}

/**
 * the live play surface -- message stream over the shared SSE hook (the
 * reconnect-via-Last-Event-ID already works natively through the browser's EventSource,
 * see useSSE's docstring), the phase banner, phase-transition/await markers rendered
 * inline in the stream, the resolution widget (INV-7) + contradiction badge per
 * assistant message, and a human-input box gated on whether a turn is in flight or the
 * session is paused.
 */
const ENV_DOT: Record<string, string> = {
  running: "bg-green-500 animate-pulse",
  idle: "bg-yellow-500",
  kill_requested: "bg-orange-500 animate-pulse",
  completed: "bg-muted-foreground/40",
  failed: "bg-red-500",
  killed: "bg-muted-foreground/40",
  removed: "bg-muted-foreground/40",
};

/** The session's exec environments (delegated coding work), live: which
 * container/pod/task exists for this run, is it still up, and a way to kill it.
 * The full cross-session panel lives on the Repos page; this is the per-run cue. */
function SessionEnvironments({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const { data: envs } = useQuery({
    queryKey: ["session-environments", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos/exec-environments", {
        params: { query: { include_finished: true } },
      });
      if (error) throw error;
      return data.filter((e) => e.session_id === sessionId);
    },
    refetchInterval: 10000,
  });
  const kill = useMutation({
    mutationFn: async (environmentId: string) => {
      const { error } = await apiClient.POST(
        "/repos/exec-environments/{environment_id}/kill",
        { params: { path: { environment_id: environmentId } } },
      );
      if (error) throw error;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["session-environments", sessionId] }),
  });

  if (!envs || envs.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-muted-foreground">Environments:</span>
      {envs.map((e) => (
        <span
          key={e.id}
          className="inline-flex items-center gap-1.5 rounded bg-secondary px-2 py-0.5"
          title={`${e.image}${e.spawned_by_label ? ` · last used by ${e.spawned_by_label}` : ""}`}
        >
          <span className={`h-2 w-2 rounded-full ${ENV_DOT[e.status] ?? "bg-muted-foreground/40"}`} />
          <span className="font-mono">{e.name}</span>
          <span className="text-muted-foreground">
            {e.status}
            {e.last_exit_code != null && e.status !== "running" ? ` · exit ${e.last_exit_code}` : ""}
          </span>
          {["running", "idle"].includes(e.status) && (
            <button
              type="button"
              disabled={kill.isPending}
              onClick={() => kill.mutate(e.id)}
              title="Tear this environment down"
              className="ml-0.5 text-destructive hover:underline disabled:opacity-50"
            >
              kill
            </button>
          )}
        </span>
      ))}
    </div>
  );
}

export function SessionView() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const { liveText, events, status, typing } = useSSE(sessionId ?? null);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [editingAgenda, setEditingAgenda] = useState(false);
  const [agendaDraft, setAgendaDraft] = useState("");
  const [inspecting, setInspecting] = useState<Set<string>>(new Set());
  const [compareSelection, setCompareSelection] = useState<string[]>([]);
  // Off by default. Comparing what two turns were each given is a real diagnostic --
  // it is how you check that the assembler excluded what it should -- but it is a
  // thing you go looking for, and a tickbox on every single message made the
  // transcript read like a form rather than a conversation.
  const [compareMode, setCompareMode] = useState(false);
  const [comparing, setComparing] = useState<[string, string] | null>(null);
  const t = useLabel();

  function toggleInspect(messageId: string) {
    setInspecting((prev) => {
      const next = new Set(prev);
      if (next.has(messageId)) next.delete(messageId);
      else next.add(messageId);
      return next;
    });
  }

  function toggleCompareSelect(messageId: string) {
    setCompareSelection((prev) => {
      if (prev.includes(messageId)) return prev.filter((id) => id !== messageId);
      if (prev.length >= 2) return [prev[1], messageId];
      return [...prev, messageId];
    });
  }

  const {
    data: session,
    isError: sessionLoadFailed,
    isLoading: sessionLoading,
  } = useQuery({
    queryKey: ["session", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions/{session_id}", {
        params: { path: { session_id: sessionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sessionId,
    // activity/phase are time-derived server-side; keep the header cue honest.
    refetchInterval: 10000,
  });
  const usage = useQuery({
    queryKey: ["session-usage", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions/{session_id}/usage", {
        params: { path: { session_id: sessionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sessionId,
    refetchInterval: 30000,
  });

  useWorkspaceVocabulary(session?.workspace_id);

  const renameSession = useMutation({
    mutationFn: async (name: string | null) => {
      const { error } = await apiClient.PATCH("/sessions/{session_id}", {
        params: { path: { session_id: sessionId! } },
        body: { name },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
  });

  // Repos this session may delegate coding work to (empty for non-coding sessions).
  const { data: sessionRepos } = useQuery({
    queryKey: ["session-repos", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions/{session_id}/repos", {
        params: { path: { session_id: sessionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sessionId,
  });

  const submitMessage = useMutation({
    mutationFn: async (content: string) => {
      const { error } = await apiClient.POST("/sessions/{session_id}/messages", {
        params: { path: { session_id: sessionId! } },
        body: { content },
      });
      if (error) throw error;
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.trim()) return;
    submitMessage.mutate(draft);
    setDraft("");
  }

  const isDirected = session?.turn_policy === "directed";
  // A finished flow leaves status 'active' (the interpreter signals terminal by a
  // phase_transition with no target, it doesn't stamp the row) -- so read the stream for the
  // terminal marker, which is what hides the conductor once the supervisor has synthesized.
  const isTerminal =
    session?.status === "terminal" ||
    events.some(
      (e) => e.kind === "phase_transition" && (e.payload as PhaseTransitionPayload)?.to === null,
    );

  // #7: the pinned roster, so the conductor can direct a turn from (or answer as) a persona.
  const { data: mcpServers } = useQuery({
    queryKey: ["mcp-servers", session?.workspace_id],
    enabled: !!session?.workspace_id,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/mcp-servers", {
        params: { query: { workspace_id: session!.workspace_id } },
      });
      if (error) throw error;
      return data;
    },
  });
  const assetOrigins = (mcpServers ?? [])
    .filter((srv) => srv.url.startsWith("http"))
    .map((srv) => ({ origin: new URL(srv.url).origin, key: srv.key }));

  const { data: roster } = useQuery({
    queryKey: ["session-roster", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions/{session_id}/personas", {
        params: { path: { session_id: sessionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sessionId && isDirected,
  });

  const setPolicy = useMutation({
    mutationFn: async (turn_policy: "auto" | "directed") => {
      const { error } = await apiClient.PATCH("/sessions/{session_id}/turn-policy", {
        params: { path: { session_id: sessionId! } },
        body: { turn_policy },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
  });

  const directedGenerate = useMutation({
    mutationFn: async (persona_id: string) => {
      const { error } = await apiClient.POST("/sessions/{session_id}/turns/generate", {
        params: { path: { session_id: sessionId! } },
        body: { persona_id },
      });
      if (error) throw error;
    },
  });

  const wrapUp = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/sessions/{session_id}/conduct/wrap-up", {
        params: { path: { session_id: sessionId! } },
      });
      if (error) throw error;
    },
  });

  const recap = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/sessions/{session_id}/recap", {
        params: { path: { session_id: sessionId! } },
      });
      if (error) throw error;
    },
    onSuccess: () =>
      toast.success("Recap requested — it will appear in the transcript shortly."),
    onError: () => toast.error("Recap failed to start."),
  });

  // #5: set/clear the agenda the supervisor is steered by. Editable mid-run, not just at launch.
  const setAgenda = useMutation({
    mutationFn: async (agenda_md: string | null) => {
      const { error } = await apiClient.PATCH("/sessions/{session_id}/agenda", {
        params: { path: { session_id: sessionId! } },
        body: { agenda_md },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setEditingAgenda(false);
      queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    },
  });

  // #7 continue: re-open a finished flow (or re-kick a parked one) for more turns.
  const continueRun = useMutation({
    mutationFn: async (rounds: number) => {
      const { error } = await apiClient.POST("/sessions/{session_id}/continue", {
        params: { path: { session_id: sessionId! } },
        body: { rounds },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
  });

  const turnInFlight = liveText !== "";
  const canAct = session?.status !== "paused" && !turnInFlight;
  // Conducting is phase-independent server-side (a directed turn generates at whatever phase
  // the session sits at, terminal included) -- so it is NOT gated on !isTerminal; a finished
  // session can still be conducted for more turns.
  const canConduct = isDirected && !turnInFlight;

  if (!sessionId) return null;
  // A dead or forbidden session id used to render a healthy-looking empty chat --
  // indistinguishable from "no messages yet". Say what actually happened.
  if (sessionLoadFailed) {
    return (
      <div className="flex flex-col items-start gap-3 py-12">
        <BackLink to={`/workspaces/${session?.workspace_id ?? ""}`} label="Back to the workspace" />
        <p className="text-sm text-destructive">
          This session could not be loaded — it may have been archived, or you may not
          have access to it.
        </p>
        <Link
          to="/"
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
        >
          Back to your Workspaces
        </Link>
      </div>
    );
  }
  if (sessionLoading) {
    return <p className="text-muted-foreground">Loading…</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="flex min-w-0 items-center gap-2.5">
          <SessionStatusDot activity={session?.activity} />
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 text-xl font-semibold">
              <span className="truncate">
                {session ? sessionDisplayName(session) : t("entity.session")}
              </span>
              <NameDialog
                title="Rename this session"
                initialValue={session?.name ?? ""}
                submitLabel="Rename"
                onSubmit={(next) => renameSession.mutate(next)}
              >
                <button
                  type="button"
                  title="Rename this session"
                  className="text-sm text-muted-foreground hover:text-foreground"
                >
                  ✎
                </button>
              </NameDialog>
            </h1>
            <p className="text-xs text-muted-foreground">
              {session?.created_at &&
                new Date(session.created_at).toLocaleString([], {
                  dateStyle: "medium",
                  timeStyle: "short",
                })}
              {session && ` · ${t(`phase.${session.current_phase}`)}`}
              {usage.data && usage.data.prompt_tokens + usage.data.completion_tokens > 0 && (
                <>
                  {" · "}
                  {(usage.data.prompt_tokens + usage.data.completion_tokens).toLocaleString()}{" "}
                  tokens
                </>
              )}
              {session &&
                ` · ${
                  session.activity === "running"
                    ? "active now"
                    : session.activity === "stopped"
                      ? "finished"
                      : session.last_event_at
                        ? `last activity ${relativeTime(session.last_event_at)}`
                        : "waiting"
                }`}
            </p>
          </div>
        </div>
        <span
          className={`shrink-0 text-xs ${
            status === "live"
              ? "text-muted-foreground"
              : status === "reconnecting"
                ? "text-amber-500"
                : "text-muted-foreground"
          }`}
        >
          {status === "live" ? "live" : status === "reconnecting" ? "reconnecting…" : "connecting…"}
        </span>
      </div>

      <PhaseBanner sessionId={sessionId} events={events} />
      <CharactersPanel workspaceId={session?.workspace_id} sessionId={sessionId} />
      <WorkPanel sessionId={sessionId} workspaceId={session?.workspace_id} />
      <ReportPanel sessionId={sessionId} />

      {(sessionRepos?.length ?? 0) > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted-foreground">Repos:</span>
          {sessionRepos?.map((r) => (
            <span key={r.id} className="rounded bg-secondary px-2 py-0.5 font-mono">
              {r.key}
              <span className="ml-1 text-muted-foreground">({r.runtime})</span>
            </span>
          ))}
        </div>
      )}

      <SessionEnvironments sessionId={sessionId} />

      {sessionId && (sessionRepos?.length ?? 0) > 0 && (
        <SessionPreviewPanel sessionId={sessionId} repos={sessionRepos ?? []} />
      )}

      {/* #5: the agenda the supervisor is steered by -- shown on every session (not just at
          launch), and editable mid-run. */}
      {session && (
        <div className="flex flex-col gap-2 rounded-md border border-border p-4">
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm font-medium">Agenda</span>
            {!editingAgenda && (
              <button
                type="button"
                onClick={() => {
                  setAgendaDraft(session.agenda_md ?? "");
                  setEditingAgenda(true);
                }}
                className="text-xs text-muted-foreground underline"
              >
                {session.agenda_md ? "Edit" : "Add agenda"}
              </button>
            )}
          </div>
          {editingAgenda ? (
            <div className="flex flex-col gap-2">
              <textarea
                className="min-h-24 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={agendaDraft}
                onChange={(e) => setAgendaDraft(e.target.value)}
                placeholder="What should the supervisor drive the session toward?"
              />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setAgenda.mutate(agendaDraft.trim() === "" ? null : agendaDraft)}
                  disabled={setAgenda.isPending}
                  className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
                >
                  {setAgenda.isPending ? "Saving…" : "Save"}
                </button>
                <button
                  type="button"
                  onClick={() => setEditingAgenda(false)}
                  className="rounded-md border border-border px-3 py-1.5 text-sm"
                >
                  Cancel
                </button>
                {setAgenda.isError && (
                  <span className="text-xs text-destructive">
                    {(setAgenda.error as Error)?.message ?? "could not save"}
                  </span>
                )}
              </div>
            </div>
          ) : session.agenda_md ? (
            <Markdown text={session.agenda_md} className="text-sm text-muted-foreground" />
          ) : (
            <p className="text-sm text-muted-foreground">No agenda set.</p>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <button
          type="button"
          onClick={() => {
            setCompareMode((on) => !on);
            setCompareSelection([]);
            setComparing(null);
          }}
          className="rounded-md border border-border px-2 py-0.5"
        >
          {compareMode ? "Done comparing" : "Compare turns' context"}
        </button>
        {compareMode && <span>pick two turns below</span>}
      </div>

      {compareMode && compareSelection.length === 2 && (
        <button
          type="button"
          onClick={() => setComparing([compareSelection[0], compareSelection[1]])}
          className="self-start rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
        >
          Compare selected turns' context
        </button>
      )}

      {comparing && (
        <section className="flex flex-col gap-2 rounded-md border border-border p-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">Context comparison</h2>
            <button
              type="button"
              onClick={() => setComparing(null)}
              className="text-xs text-muted-foreground"
            >
              close
            </button>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <ContextInspectorPanel messageId={comparing[0]} />
            <ContextInspectorPanel messageId={comparing[1]} />
          </div>
        </section>
      )}

      <div className="flex flex-col gap-3 rounded-md border border-border p-4">
        {events.map((event, i) => (
          <TimelineEvent
            key={i}
            event={event}
            t={t}
            sessionId={sessionId ?? ""}
            assetOrigins={assetOrigins}
            workspaceId={session?.workspace_id ?? null}
            inspecting={inspecting}
            onToggleInspect={toggleInspect}
            compareMode={compareMode}
            compareSelection={compareSelection}
            onToggleCompareSelect={toggleCompareSelect}
          />
        ))}
        {liveText && (
          <div className="text-left">
            <div className="text-xs text-muted-foreground">{typing ?? t("role.facilitator")}</div>
            <div className="inline-block rounded-md bg-secondary px-3 py-2 opacity-70">
              {liveText}
            </div>
          </div>
        )}
        {typing && !liveText && (
          <div className="text-left">
            <div className="text-xs text-muted-foreground">{typing}</div>
            <div className="inline-flex items-center gap-1.5 rounded-md bg-secondary px-3 py-2 text-sm text-muted-foreground">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
              preparing a reply…
            </div>
          </div>
        )}
        {events.length === 0 && !liveText && (
          <p className="text-muted-foreground">No messages yet — say something below.</p>
        )}
      </div>

      {/* #7: flow toggle + conductor. The toggle sits right by the chat box so the overseer
          can hand the discussion back and forth between autonomous and conducted mid-run. Shown
          even once the flow is terminal, so a finished session can be re-opened or conducted. */}
      {session?.process_definition_id && (
        <div className="flex flex-col gap-3 rounded-md border border-border p-4">
          {isTerminal && (
            <div className="flex flex-col gap-2 rounded-md bg-secondary/60 px-3 py-2">
              <span className="text-sm font-medium">This flow has completed.</span>
              <span className="text-xs text-muted-foreground">
                {isDirected
                  ? "Re-open it for more conducted turns, or switch to Autonomous to run more rounds."
                  : "Continue to run more autonomous rounds, or switch to Managed to conduct turns yourself."}
              </span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => continueRun.mutate(1)}
                  disabled={continueRun.isPending || turnInFlight}
                  className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
                >
                  {continueRun.isPending ? "Continuing…" : "Continue"}
                </button>
                {continueRun.isError && (
                  <span className="text-xs text-destructive">
                    {(continueRun.error as Error)?.message ?? "could not continue"}
                  </span>
                )}
              </div>
            </div>
          )}
          <div className="flex items-center justify-between gap-3">
            <div className="flex flex-col">
              <span className="text-sm font-medium">Flow</span>
              <span className="text-xs text-muted-foreground">
                {isDirected
                  ? "Managed — you pick who speaks next."
                  : "Autonomous — personas run the discussion themselves."}
              </span>
            </div>
            <div className="flex overflow-hidden rounded-md border border-border text-sm">
              <button
                type="button"
                onClick={() => !isDirected || setPolicy.mutate("auto")}
                disabled={setPolicy.isPending}
                className={`px-3 py-1.5 disabled:opacity-50 ${
                  !isDirected ? "bg-primary text-primary-foreground" : ""
                }`}
              >
                Autonomous
              </button>
              <button
                type="button"
                onClick={() => isDirected || setPolicy.mutate("directed")}
                disabled={setPolicy.isPending}
                className={`px-3 py-1.5 disabled:opacity-50 ${
                  isDirected ? "bg-primary text-primary-foreground" : ""
                }`}
              >
                Managed
              </button>
            </div>
          </div>
          {setPolicy.isError && (
            <p className="text-xs text-destructive">
              Could not switch flow: {(setPolicy.error as Error)?.message ?? "not permitted"}
            </p>
          )}

          {isDirected && (
            <div className="flex flex-col gap-2 border-t border-border pt-3">
              <span className="text-sm font-medium">Conduct the discussion</span>
              {!roster?.length && (
                <span className="text-xs text-muted-foreground">Loading roster…</span>
              )}
              {roster?.map((p) => (
                <ConductorRow
                  key={p.persona_id}
                  sessionId={sessionId}
                  persona={p}
                  disabled={!canConduct}
                  onGenerate={() => directedGenerate.mutate(p.persona_id)}
                />
              ))}
              <div className="mt-1 flex items-center gap-3">
                <ConfirmButton
                title="Wrap up &amp; synthesize"
                description={"End the discussion and have the supervisor synthesize?"}
                confirmLabel="Wrap up &amp; synthesize"
                destructive
                onConfirm={() => wrapUp.mutate()}
              >
                <button
                  type="button"
                  disabled={!canConduct || wrapUp.isPending}
                  className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
                >
                  Wrap up &amp; synthesize
                </button>
              </ConfirmButton>
                <button
                  type="button"
                  disabled={recap.isPending}
                  onClick={() => recap.mutate()}
                  className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
                >
                  Recap
                </button>
                {turnInFlight && (
                  <span className="text-xs text-muted-foreground">a turn is generating…</span>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          className="flex-1 rounded-md border border-input bg-transparent px-3 py-2 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={canAct ? "Say something…" : "Waiting…"}
          disabled={!canAct}
        />
        <button
          type="submit"
          disabled={!canAct || submitMessage.isPending}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          Send
        </button>
      </form>
    </div>
  );
}

interface RosterPersona {
  persona_id: string;
  name: string;
  persona_type: string;
  is_supervisor: boolean;
}

/** #7: one roster persona in the conductor panel — direct a model turn for them, or answer
 * in their place (reusing the verbatim/voice override). */
function ConductorRow({
  sessionId,
  persona,
  disabled,
  onGenerate,
}: {
  sessionId: string | undefined;
  persona: RosterPersona;
  disabled: boolean;
  onGenerate: () => void;
}) {
  const [answering, setAnswering] = useState(false);

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border/60 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm">
          {persona.name}
          <span className="ml-2 text-xs text-muted-foreground">
            {persona.is_supervisor ? "supervisor" : "participant"}
          </span>
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onGenerate}
            disabled={disabled}
            className="rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
          >
            Generate
          </button>
          <button
            type="button"
            onClick={() => setAnswering((v) => !v)}
            disabled={disabled}
            className="rounded-md border border-border px-2.5 py-1 text-xs disabled:opacity-50"
          >
            {answering ? "Cancel" : "Answer as…"}
          </button>
        </div>
      </div>
      {answering && (
        <AnswerAsComposer
          sessionId={sessionId}
          persona={persona}
          onDone={() => setAnswering(false)}
        />
      )}
    </div>
  );
}

/** #7: answer in a persona's place — verbatim, or through the voice-conformance rewrite,
 * previewed before posting. Thin wrapper over the existing /override/draft + /override. */
function AnswerAsComposer({
  sessionId,
  persona,
  onDone,
}: {
  sessionId: string | undefined;
  persona: RosterPersona;
  onDone: () => void;
}) {
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"verbatim" | "voice">("verbatim");
  const [preview, setPreview] = useState<string | null>(null);

  const draftMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/sessions/{session_id}/override/draft", {
        params: { path: { session_id: sessionId! } },
        body: { persona_id: persona.persona_id, content_md: text, mode },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => setPreview(d?.proposed_content_md ?? text),
  });

  const postMut = useMutation({
    mutationFn: async () => {
      const confirmed = mode === "voice" ? preview ?? text : text;
      const { error } = await apiClient.POST("/sessions/{session_id}/override", {
        params: { path: { session_id: sessionId! } },
        body: {
          persona_id: persona.persona_id,
          original_content_md: text,
          confirmed_content_md: confirmed,
          mode,
        },
      });
      if (error) throw error;
    },
    onSuccess: onDone,
  });

  return (
    <div className="flex flex-col gap-2">
      <textarea
        className="min-h-16 w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setPreview(null);
        }}
        placeholder={`What does ${persona.name} say?`}
      />
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <label className="flex items-center gap-1">
          <input
            type="radio"
            checked={mode === "verbatim"}
            onChange={() => {
              setMode("verbatim");
              setPreview(null);
            }}
          />
          verbatim
        </label>
        <label className="flex items-center gap-1">
          <input
            type="radio"
            checked={mode === "voice"}
            onChange={() => {
              setMode("voice");
              setPreview(null);
            }}
          />
          rewrite in their voice
        </label>
        {mode === "voice" && (
          <button
            type="button"
            onClick={() => draftMut.mutate()}
            disabled={!text.trim() || draftMut.isPending}
            className="rounded-md border border-border px-2 py-0.5 disabled:opacity-50"
          >
            {draftMut.isPending ? "Rewriting…" : "Preview rewrite"}
          </button>
        )}
        <button
          type="button"
          onClick={() => postMut.mutate()}
          disabled={!text.trim() || postMut.isPending || (mode === "voice" && preview === null)}
          className="rounded-md bg-primary px-2.5 py-0.5 font-medium text-primary-foreground disabled:opacity-50"
        >
          Post
        </button>
      </div>
      {mode === "voice" && preview !== null && (
        <div className="rounded-md bg-secondary px-3 py-2 text-sm">{preview}</div>
      )}
      {postMut.isError && (
        <p className="text-xs text-destructive">
          Could not post: {(postMut.error as Error)?.message ?? "unknown error"}
        </p>
      )}
    </div>
  );
}

function TimelineEvent({
  event,
  t,
  sessionId,
  inspecting,
  onToggleInspect,
  compareMode,
  compareSelection,
  onToggleCompareSelect,
  assetOrigins,
  workspaceId,
}: {
  event: SSEMessageEvent;
  t: (key: string) => string;
  sessionId: string;
  inspecting: Set<string>;
  onToggleInspect: (messageId: string) => void;
  compareMode: boolean;
  compareSelection: string[];
  onToggleCompareSelect: (messageId: string) => void;
  assetOrigins: Array<{ origin: string; key: string }>;
  workspaceId: string | null;
}) {
  switch (event.kind) {
    case "message": {
      const m = event.payload as MessagePayload;
      return (
        <div className={m.role === "user" ? "text-right" : "text-left"}>
          <div className="text-xs text-muted-foreground">
            {m.author ?? (m.role === "user" ? t("role.human_participant") : t("role.facilitator"))}
            {m.created_at && (
              <span>
                {" · "}
                {new Date(m.created_at).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                })}
              </span>
            )}
            {m.triggered_by && m.triggered_by !== "unknown" && (
              <span
                className="ml-2 rounded-full bg-secondary px-1.5 py-0.5 text-[10px] text-secondary-foreground"
                title={TRIGGER_HINTS[m.triggered_by] ?? m.triggered_by}
              >
                {TRIGGER_LABELS[m.triggered_by] ?? m.triggered_by}
              </span>
            )}
            {typeof m.tool_calls_made === "number" && m.tool_calls_made > 0 && (
              <span> · {m.tool_calls_made} tool call(s)</span>
            )}
          </div>
          <div className="inline-block max-w-full rounded-md bg-secondary px-3 py-2 text-left">
            <Markdown text={m.content} />
          </div>
          <MessageAssets content={m.content} assetOrigins={assetOrigins} workspaceId={workspaceId} />
          {m.role === "assistant" && m.id && (
            <div className="mt-1 flex flex-col gap-1">
              <ResolutionWidget messageId={m.id} />
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <button
                  type="button"
                  onClick={() => onToggleInspect(m.id!)}
                  className="rounded-md border border-border px-2 py-0.5"
                >
                  {inspecting.has(m.id) ? "Hide context" : "Inspect context"}
                </button>
                {compareMode && (
                  <label className="flex items-center gap-1">
                    <input
                      type="checkbox"
                      checked={compareSelection.includes(m.id)}
                      onChange={() => onToggleCompareSelect(m.id!)}
                    />
                    compare
                  </label>
                )}
              </div>
              {inspecting.has(m.id) && <ContextInspectorPanel messageId={m.id} />}
            </div>
          )}
        </div>
      );
    }
    case "human_override": {
      const o = event.payload as HumanOverridePayload;
      return (
        <div className="text-left">
          <div className="text-xs text-muted-foreground">
            {o.author ?? t("role.facilitator")}
            <OverrideBadge rewriteApplied={o.rewrite_applied} />
          </div>
          <div className="inline-block max-w-full rounded-md bg-secondary px-3 py-2">
            <Markdown text={o.content} />
          </div>
          {o.id && (
            <div className="mt-1 flex flex-col gap-1">
              <ResolutionWidget messageId={o.id} />
            </div>
          )}
        </div>
      );
    }
    case "phase_transition": {
      const p = event.payload as PhaseTransitionPayload;
      return (
        <div className="text-center text-xs text-muted-foreground">
          — {t(`phase.${p.from}`)} → {p.to ? t(`phase.${p.to}`) : "(terminal)"} —
        </div>
      );
    }
    case "preview_ready": {
      const p = event.payload as {
        url?: string;
        artifact_name?: string;
        preview_id?: string;
        repo_id?: string;
      };
      // The whole point of the preview arc: a person reading the conversation can open
      // what the agents built -- and, since the recorded link expires while the
      // transcript does not, ask for a working one without leaving the page.
      return (
        <PreviewReadyCard
          url={p.url ?? ""}
          artifactName={p.artifact_name}
          previewId={p.preview_id}
          repoId={p.repo_id}
          sessionId={sessionId}
        />
      );
    }
    case "exec_environment": {
      const p = event.payload as ExecEnvironmentPayload;
      const name = p.url ? (
        <a
          href={p.url}
          target="_blank"
          rel="noreferrer"
          className="underline decoration-dotted hover:text-foreground"
        >
          {p.name}
        </a>
      ) : (
        <span className="font-mono">{p.name}</span>
      );
      return (
        <div className="text-center text-xs text-muted-foreground">
          — {p.noun} {name} {p.action}
          {p.task ? (
            <>
              {" "}
              for task “{p.task}”
            </>
          ) : null}
          {p.actor ? <> by {p.actor}</> : null}
          {p.image ? <> ({p.image})</> : null} —
        </div>
      );
    }
    case "tool_call": {
      const c = event.payload as ToolCallPayload;
      const args = Object.entries(c.arguments ?? {})
        .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
        .join(", ");
      return (
        <details className="text-left text-xs text-muted-foreground">
          <summary className="cursor-pointer">
            <span className="font-medium">{c.author || "tool"}</span>
            {" → "}
            <span className="font-mono">
              {c.server_key}.{c.tool_name}
            </span>
            {args ? <span className="font-mono"> ({args})</span> : null}
            {" · "}
            <span className={TOOL_OUTCOME_CLASS[c.outcome] ?? ""}>{c.outcome}</span>
            {c.outcome !== "completed" && c.message ? <span> — {c.message}</span> : null}
          </summary>
          {c.result ? (
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-secondary/60 p-2 font-mono">
              {c.result}
              {c.result_truncated ? "\n[…]" : ""}
            </pre>
          ) : null}
        </details>
      );
    }
    case "await": {
      const a = event.payload as AwaitPayload;
      return (
        <div className="text-center text-xs text-amber-500">
          {a.outcome === "satisfied" ? "— await satisfied —" : "— await timed out —"}
        </div>
      );
    }
    case "error": {
      const e = event.payload as ErrorPayload;
      return <div className="text-center text-xs text-destructive">— {e.message} —</div>;
    }
    default:
      return null;
  }
}


/** Images a registered MCP server generated (asset URLs in message text): browsers
 * can't reach the server's own host, so matched URLs render through the workspace's
 * `/mcp-servers/asset/...` window — bounded to operator-registered origins. */
function MessageAssets({
  content,
  assetOrigins,
  workspaceId,
}: {
  content: string;
  assetOrigins: Array<{ origin: string; key: string }>;
  workspaceId: string | null;
}) {
  if (!workspaceId || assetOrigins.length === 0) return null;
  const urls = content.match(/https?:\/\/[^\s`"')\]]+\.(?:png|jpe?g|gif|webp)/g) ?? [];
  const proxied: string[] = [];
  for (const raw of urls) {
    try {
      const u = new URL(raw);
      const server = assetOrigins.find((a) => a.origin === u.origin);
      if (server) {
        proxied.push(
          `/api/mcp-servers/asset/${workspaceId}/${server.key}${u.pathname}`,
        );
      }
    } catch {
      /* unparseable URL in prose — ignore */
    }
  }
  if (proxied.length === 0) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-2">
      {[...new Set(proxied)].map((src) => (
        <a key={src} href={src} target="_blank" rel="noreferrer">
          <img
            src={src}
            alt="generated asset"
            className="max-h-64 rounded-md border border-border"
            loading="lazy"
          />
        </a>
      ))}
    </div>
  );
}

/**
 * The preview of what this session built, offered where the session is being read.
 *
 * The Repos page has the same controls, but a person following a build in the discussion
 * should not have to leave it to get a working link — and the link posted into the
 * transcript is a snapshot that expires, while this reads the live row.
 */
function SessionPreviewPanel({
  sessionId,
  repos,
}: {
  sessionId: string;
  repos: Array<{ id: string; key: string; name: string; artifact_name?: string | null }>;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  // Only a web-build tarball is servable; anything else would deploy to a 404.
  const deployable = repos.filter((r) => (r.artifact_name ?? "").match(/\.(tar\.gz|tgz)$/));
  const { data: previews } = usePreviews({ enabled: deployable.length > 0 });
  if (deployable.length === 0) return null;

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">Play the build</span>
        <span className="text-xs text-muted-foreground">
          a link anyone can open, no account needed
        </span>
      </div>
      {notice && <p className="break-all rounded bg-secondary px-2 py-1 text-xs">{notice}</p>}
      {deployable.map((repo) => (
        <RepoPreviewRow
          key={repo.id}
          repo={repo}
          sessionId={sessionId}
          previews={previews ?? []}
          setNotice={setNotice}
        />
      ))}
    </div>
  );
}

/**
 * One repository's preview controls, including which branch is being previewed.
 *
 * A session that delegates six work items opens six pull requests, and each one now has
 * its own build. Picking between them is the whole point of this row: previews are keyed
 * by repo *and* ref, so two branches are two previews rather than one that keeps
 * replacing itself.
 */
export function RepoPreviewRow({
  repo,
  sessionId,
  previews,
  setNotice,
}: {
  repo: { id: string; key: string; name: string; artifact_name?: string | null };
  sessionId: string;
  previews: Array<{
    id: string;
    repo_id?: string | null;
    git_ref?: string | null;
    status?: string | null;
    expires_at?: string | null;
    last_error?: string | null;
  }>;
  setNotice: (text: string) => void;
}) {
  const { deploy, share, stop } = usePreviewActions(setNotice);
  const { data: pullRequests } = useRepoPullRequests(repo.id);
  // null until someone picks; until then the row shows what is actually up.
  const [chosenRef, setChosenRef] = useState<string | null>(null);

  // Every pull request is listed, but only one with a build behind it can be chosen:
  // deploying a branch that never produced an artifact lands on a 404 that reads as the
  // preview being broken. Hiding the un-buildable ones instead would be worse -- a session
  // with six pull requests and no builds yet would show no selector at all, which reads as
  // the feature being missing rather than as the builds not having run.
  // Previewable first: the ones that can actually be chosen are the point, and on a
  // repository with several rounds of review the buildable branch was buried under the
  // ones still waiting for a green suite.
  const choices = [...(pullRequests ?? [])].sort(
    (a, b) => Number(Boolean(b.previewable)) - Number(Boolean(a.previewable)),
  );
  // Default to a running preview. Defaulting to "the latest build" alone let an older,
  // stopped no-branch preview hide a branch preview that was up and shareable -- the row
  // read "stopped" while the transcript above it said "the build is running".
  const isRunning = (ref: string) =>
    previews.some((p) => p.repo_id === repo.id && (p.git_ref ?? "") === ref && p.status === "running");
  const runningBranch =
    previews.find(
      (p) =>
        p.repo_id === repo.id &&
        p.status === "running" &&
        choices.some((c) => c.branch === (p.git_ref ?? "")),
    )?.git_ref ?? "";
  const gitRef = chosenRef ?? (isRunning("") ? "" : runningBranch);
  const preview = previews.find(
    (p) => p.repo_id === repo.id && (p.git_ref ?? "") === gitRef,
  );
  const running = preview?.status === "running";

  return (
    <div className="flex flex-col gap-1.5 border-t border-border pt-2 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{repo.name}</span>
        {preview && (
          <span
            className={
              running
                ? "rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs text-emerald-600 dark:text-emerald-400"
                : "rounded bg-secondary px-1.5 py-0.5 text-xs text-muted-foreground"
            }
            title={preview.last_error || undefined}
          >
            {preview.status}
          </span>
        )}
        {running && preview?.expires_at && (
          <span className="text-xs text-muted-foreground">
            until {new Date(preview.expires_at).toLocaleTimeString()}
          </span>
        )}
        <span className="grow" />
        {running && preview && (
          <button
            type="button"
            onClick={() => share.mutate(preview.id)}
            disabled={share.isPending}
            className="rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground"
          >
            Copy link
          </button>
        )}
        <button
          type="button"
          onClick={() => deploy.mutate({ repoId: repo.id, sessionId, gitRef })}
          disabled={deploy.isPending}
          className="rounded-md border border-border px-2 py-1 text-xs"
        >
          {preview ? "Redeploy" : "Deploy"}
        </button>
        {running && preview && (
          <ConfirmButton
            title="Stop this preview?"
            description="The container is torn down and the shared link stops working. You can deploy it again from the latest build."
            confirmLabel="Stop"
            destructive
            onConfirm={() => stop.mutate(preview.id)}
          >
            <button type="button" className="rounded-md border border-border px-2 py-1 text-xs">
              Stop
            </button>
          </ConfirmButton>
        )}
      </div>
      {choices.length > 0 && (
        <label className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          Preview
          {/*
            A native select sizes itself to its widest option, and these labels carry a
            work item's title -- which for a real repository runs to a sentence with a
            file path in it. Unbounded, one branch stretched the control past the page.
            The width is capped here and the label is shortened below; the full text
            stays in the option's own tooltip.
          */}
          <select
            value={gitRef}
            onChange={(e) => setChosenRef(e.target.value)}
            className="w-full max-w-xs truncate rounded-md border border-border bg-background px-2 py-1 text-xs sm:w-auto"
          >
            <option value="">the latest build</option>
            {choices.map((pr) => {
              const label = pr.title || pr.branch;
              return (
                <option
                  key={pr.branch}
                  value={pr.branch}
                  disabled={!pr.previewable}
                  title={`${pr.pr_ref || pr.branch} — ${label}`}
                >
                  {pr.pr_ref || pr.branch} — {shorten(label)}
                  {pr.previewable ? "" : "  (no build yet)"}
                </option>
              );
            })}
          </select>
          {gitRef && !preview && <span>not deployed yet</span>}
        </label>
      )}
    </div>
  );
}

/** A one-line label for a dropdown. Work item titles are sentences; a native select
 * cannot ellipsise its own options, so the text is cut before it reaches one. */
function shorten(text: string, max = 52): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 1)}…`;
}

/**
 * The transcript's "the build is running" card.
 *
 * Interactive rather than a plain line, because the URL recorded in the event is a
 * snapshot: the transcript is permanent and the share token is not. Re-issuing asks the
 * server for a fresh one against the same preview; if the container is gone the server
 * says so (409) and redeploying is the way back.
 */
function PreviewReadyCard({
  url,
  artifactName,
  previewId,
  repoId,
  sessionId,
}: {
  url: string;
  artifactName?: string;
  previewId?: string;
  repoId?: string;
  sessionId: string;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const { deploy, share } = usePreviewActions(setNotice);

  return (
    <div className="my-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm">
      <span className="mr-2">▶</span>
      <span className="font-medium">The build is running.</span>{" "}
      {fresh || url ? (
        <a
          href={fresh || url}
          target="_blank"
          rel="noreferrer"
          className="underline underline-offset-2"
        >
          Open the preview
        </a>
      ) : (
        <span className="text-muted-foreground">No link was recorded.</span>
      )}
      {artifactName && (
        <span className="ml-2 text-xs text-muted-foreground">({artifactName})</span>
      )}
      {(previewId || repoId) && (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
          <span className="text-muted-foreground">Link stopped working?</span>
          {previewId && (
            <button
              type="button"
              onClick={() =>
                share.mutate(previewId, {
                  onSuccess: (data) => setFresh(absoluteUrl(data?.url ?? "")),
                })
              }
              disabled={share.isPending}
              className="underline underline-offset-2"
            >
              Get a new link
            </button>
          )}
          {repoId && (
            <button
              type="button"
              onClick={() => deploy.mutate({ repoId, sessionId })}
              disabled={deploy.isPending}
              className="underline underline-offset-2"
            >
              Redeploy
            </button>
          )}
        </div>
      )}
      {notice && <p className="mt-1 break-all text-xs text-muted-foreground">{notice}</p>}
    </div>
  );
}
