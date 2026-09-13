import { useEffect, useRef, useState } from "react";

export interface SSEMessageEvent {
  kind: string;
  payload: unknown;
}

export type SSEStatus = "connecting" | "live" | "reconnecting";

interface UseSSEResult {
  /** Live token deltas for the turn currently streaming (ephemeral — cleared on each new
   * assistant message's first `message` event, see below). */
  liveText: string;
  /** Durable events replayed from the database on connect/reconnect, and delivered live
   * thereafter -- see `api.streaming.sse.sse_stream`'s catch-up/live-subscribe split. */
  events: SSEMessageEvent[];
  /** Who is preparing a reply right now (ephemeral typing cue), or null. */
  typing: string | null;
  /** Fine-grained connection state so the UI can tell a first connect ("connecting") from
   * a live stream ("live") from a transient drop the browser is auto-recovering from
   * ("reconnecting") -- the last is not an error, EventSource reconnects on its own. */
  status: SSEStatus;
  /** Kept for existing callers: true only while the stream is actually open. */
  connected: boolean;
}

/**
 * Wraps the browser's native `EventSource` for a session's stream (T0.9).
 *
 * `Last-Event-ID` resume needs no manual handling here: it's a *native* EventSource
 * behaviour — the browser remembers the last `id:` field it saw and automatically sends
 * it back as the `Last-Event-ID` request header on its own automatic reconnect after a
 * dropped connection. That's exactly what `api/streaming/sse.py`'s catch-up logic reads.
 *
 * Durable events are de-duplicated by their `id:` (the `event_seq`): a reconnect that the
 * browser resumes *without* a `Last-Event-ID` (e.g. React StrictMode's dev double-mount
 * creates a fresh EventSource) would otherwise replay the whole catch-up log again and
 * show every message twice. `chunk` events carry no id and are ephemeral, so they are
 * never deduped.
 *
 * Auth: `EventSource` cannot send a custom `Authorization` header, so the stream relies
 * on the httponly session cookie (`withCredentials: true`) that `/auth/login` sets — not
 * the Bearer token `stores/auth.ts` uses for REST calls.
 */
export function useSSE(sessionId: string | null): UseSSEResult {
  const [liveText, setLiveText] = useState("");
  const [events, setEvents] = useState<SSEMessageEvent[]>([]);
  // Ephemeral "who is preparing a reply" cue (kind: "typing", event_seq -1) -- never
  // durable, never deduped; cleared when the completed message lands.
  const [typing, setTyping] = useState<string | null>(null);
  const [status, setStatus] = useState<SSEStatus>("connecting");
  const sourceRef = useRef<EventSource | null>(null);
  // Highest durable event id already appended -- survives a same-session reconnect so
  // replayed catch-up rows are dropped instead of duplicated.
  const lastSeqRef = useRef<number>(-1);

  useEffect(() => {
    if (!sessionId) return;

    // New session id: start its log fresh.
    setEvents([]);
    setTyping(null);
    setLiveText("");
    setStatus("connecting");
    lastSeqRef.current = -1;

    const source = new EventSource(`/api/sessions/${sessionId}/stream`, {
      withCredentials: true,
    });
    sourceRef.current = source;

    source.onopen = () => setStatus("live");
    // EventSource retries on its own after an error; reflect that as "reconnecting" rather
    // than a hard failure so the UI doesn't read a routine blip as broken.
    source.onerror = () => setStatus((s) => (s === "live" ? "reconnecting" : "connecting"));

    source.addEventListener("chunk", (e) => {
      const data = JSON.parse((e as MessageEvent).data) as { text: string };
      setLiveText((prev) => prev + data.text);
    });

    source.addEventListener("message", (e) => {
      const me = e as MessageEvent;
      const data = JSON.parse(me.data) as SSEMessageEvent;
      // Ephemeral cues first: EventSource RETAINS the previous `lastEventId` when an
      // event carries no id, so a typing cue would otherwise inherit the last
      // message's seq and be dropped by the dedupe guard below.
      if (data.kind === "typing") {
        setTyping((data.payload as { name?: string })?.name ?? null);
        return;
      }
      const seq = me.lastEventId ? Number(me.lastEventId) : NaN;
      if (!Number.isNaN(seq)) {
        if (seq <= lastSeqRef.current) return; // already have this durable event
        lastSeqRef.current = seq;
      }
      setEvents((prev) => [...prev, data]);
      // A completed message event ends the turn that liveText was accumulating.
      setLiveText("");
      setTyping(null);
    });

    return () => {
      source.close();
      sourceRef.current = null;
    };
  }, [sessionId]);

  return { liveText, events, status, typing, connected: status === "live" };
}
