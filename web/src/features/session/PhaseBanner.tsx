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
 * D1.3: current phase + session status, folded live from `phase_transition` events on
 * top of the session's snapshot at mount (`GET /sessions/{id}`, D1.3's own addition --
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

  return (
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
                : session.status === "awaiting"
                  ? "text-amber-500"
                  : "text-muted-foreground"
            }
          >
            {session.status}
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
        {session?.status === "awaiting" &&
          (pendingAwaits.data ?? []).map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => satisfy.mutate(a.id)}
              disabled={satisfy.isPending}
              className="rounded-md border border-amber-500/60 px-2 py-1 text-xs text-amber-600 hover:bg-amber-500/10 disabled:opacity-50 dark:text-amber-400"
              title={`Blocked on ${a.await_kind} since ${new Date(a.created_at).toLocaleString()}`}
            >
              Mark {a.await_kind} satisfied
            </button>
          ))}
      </div>
      {showFork && <ForkPanel sessionId={sessionId} onClose={() => setShowFork(false)} />}
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
