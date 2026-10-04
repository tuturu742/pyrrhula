import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";
import type { SSEMessageEvent } from "@/lib/sse/useSSE";

interface PhaseBannerProps {
  sessionId: string;
  events: SSEMessageEvent[];
}

interface PhaseTransitionPayload {
  from: string;
  to: string | null;
}

/**
 * current phase + session status, folded live from `phase_transition` events on
 * top of the session's snapshot at mount (`GET /sessions/{id}`, its own addition --
 * the SSE stream only ever replays/streams *events*, it never hands a caller the
 * already-current state on connect). Pause/resume act on the same snapshot.
 */
export function PhaseBanner({ sessionId, events }: PhaseBannerProps) {
  const t = useLabel();
  const queryClient = useQueryClient();

  const { data: session } = useQuery({
    queryKey: ["session", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions/{session_id}", {
        params: { path: { session_id: sessionId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const latestPhase = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const event = events[i];
      if (event.kind === "phase_transition") {
        const payload = event.payload as PhaseTransitionPayload;
        if (payload.to) return payload.to;
      }
    }
    return session?.current_phase ?? null;
  }, [events, session]);

  const pause = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/sessions/{session_id}/pause", {
        params: { path: { session_id: sessionId } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
  });

  const resume = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/sessions/{session_id}/resume", {
        params: { path: { session_id: sessionId } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["session", sessionId] }),
  });

  const pendingAwaits = useQuery({
    queryKey: ["session-awaits", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions/{session_id}/awaits", {
        params: { path: { session_id: sessionId } },
      });
      if (error) throw error;
      return data;
    },
    enabled: session?.status === "awaiting",
    refetchInterval: 10000,
  });

  const satisfy = useMutation({
    mutationFn: async (awaitStateId: string) => {
      const { error } = await apiClient.POST("/sessions/{session_id}/await/satisfy", {
        params: { path: { session_id: sessionId } },
        body: { await_state_id: awaitStateId },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["session-awaits", sessionId] });
      void queryClient.invalidateQueries({ queryKey: ["session", sessionId] });
    },
  });

  const [showFork, setShowFork] = useState(false);

  // Two different mechanisms park a session on a person, and neither said so. An open
  // await sets status "awaiting"; a free-mode actor leaves status "active" while the
  // interpreter reports awaiting_human, which the session row now records. Both mean the
  // same thing to whoever is reading the page — it is your move — so both say it.
  //
  // A third parks it on nobody: a phase that delegated work waits for those jobs, and the
  // session continues by itself when the last one finishes. That used to read "Waiting for
  // you ... blocked until someone confirms it is done" with an "awaiting" status while an
  // agent was busy in its container -- an instruction to do something nobody needed to do.
  const allPending = pendingAwaits.data ?? [];
  const delegated = allPending.filter((a) => a.await_kind === "delegated_work");
  const pending = allPending.filter((a) => a.await_kind !== "delegated_work");
  const working = session?.status === "awaiting" && delegated.length > 0 && pending.length === 0;
  const waitingOnYou =
    !working && (session?.status === "awaiting" || session?.awaiting === "human");

  return (
    <div className="flex flex-col gap-2">
      {working ? (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-sky-500/50 bg-sky-500/10 px-3 py-2 text-sm">
          <span className="font-medium text-sky-700 dark:text-sky-400">Working</span>
          <span className="text-muted-foreground">
            Delegated work is running in its container. The session continues on its own
            when it finishes — nothing is needed from you.
          </span>
        </div>
      ) : null}
      {waitingOnYou ? (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-amber-500/60 bg-amber-500/10 px-3 py-2 text-sm">
          <span className="font-medium text-amber-700 dark:text-amber-400">
            Waiting for you
          </span>
          <span className="text-muted-foreground">
            {pending.length > 0
              ? "This phase is blocked until someone confirms it is done."
              : "This phase takes a written turn rather than a generated one — post a message to continue."}
          </span>
          {pending.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => satisfy.mutate(a.id)}
              disabled={satisfy.isPending}
              className="rounded-md border border-amber-500/60 bg-background px-2 py-1 text-xs font-medium text-amber-700 hover:bg-amber-500/10 disabled:opacity-50 dark:text-amber-400"
            >
              {satisfy.isPending ? "Continuing…" : "Done — continue"}
            </button>
          ))}
        </div>
      ) : null}
    <div className="relative flex items-center justify-between rounded-md border border-border px-3 py-2">
      <div className="flex items-center gap-3 text-sm">
        <span className="rounded-full bg-secondary px-2 py-0.5 font-medium text-secondary-foreground">
          {latestPhase ? t(`phase.${latestPhase}`) : "…"}
        </span>
        {session && (
          <span
            className={
              session.status === "paused"
                ? "text-destructive"
                : working
                  ? "text-sky-600 dark:text-sky-400"
                  : session.status === "awaiting"
                    ? "text-amber-500"
                    : "text-muted-foreground"
            }
          >
            {working ? "working" : session.status}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        {session?.status === "paused" ? (
          <button
            type="button"
            onClick={() => resume.mutate()}
            disabled={resume.isPending}
            className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
          >
            Resume
          </button>
        ) : (
          <button
            type="button"
            onClick={() => pause.mutate()}
            disabled={pause.isPending}
            className="rounded-md border border-border px-2 py-1 text-xs disabled:opacity-50"
          >
            Pause
          </button>
        )}
        <button
          type="button"
          onClick={() => setShowFork((v) => !v)}
          className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent"
        >
          Fork…
        </button>
      </div>
      {showFork && <ForkPanel sessionId={sessionId} onClose={() => setShowFork(false)} />}
    </div>
    </div>
  );
}

function ForkPanel({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const { data: checkpoints, isLoading } = useQuery({
    queryKey: ["session-checkpoints", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/sessions/{session_id}/checkpoints", {
        params: { path: { session_id: sessionId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const fork = useMutation({
    mutationFn: async (checkpointId: string) => {
      const { data, error } = await apiClient.POST("/sessions/{session_id}/fork", {
        params: { path: { session_id: sessionId } },
        body: { checkpoint_id: checkpointId },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (forked) => {
      if (forked) window.location.assign(`/sessions/${forked.id}`);
    },
  });

  return (
    <div className="absolute z-10 mt-2 flex w-72 flex-col gap-2 rounded-md border border-border bg-background p-3 shadow-md">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">Fork from checkpoint</span>
        <button type="button" onClick={onClose} className="text-xs text-muted-foreground">
          close
        </button>
      </div>
      {isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
      {checkpoints?.length === 0 && (
        <p className="text-xs text-muted-foreground">
          No checkpoints yet -- checkpoints are written at each phase transition.
        </p>
      )}
      <ul className="flex flex-col gap-1">
        {checkpoints?.map((cp) => (
          <li key={cp.id} className="flex items-center justify-between text-xs">
            <span>
              {cp.phase} (seq {cp.event_seq})
            </span>
            <button
              type="button"
              onClick={() => fork.mutate(cp.id)}
              disabled={fork.isPending}
              className="rounded-md border border-border px-2 py-0.5 disabled:opacity-50"
            >
              Fork here
            </button>
          </li>
        ))}
      </ul>
      {fork.error !== null && <p className="text-xs text-destructive">Fork failed.</p>}
    </div>
  );
}
