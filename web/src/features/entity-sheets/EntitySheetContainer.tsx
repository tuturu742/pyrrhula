import { useEffect, useMemo, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSSE } from "@/lib/sse/useSSE";
import { getEntityView } from "./api";
import { SheetView } from "./SheetView";

export interface EntitySheetContainerProps {
  workspaceId: string;
  entityId: string;
  labelKeyPrefix: string;
  /** A live session's id, if this sheet is open during one -- live updates (F3.10)
   * only apply when a session is in progress; a sheet viewed outside any session just
   * shows the entity's current state, refetched on demand. */
  sessionId?: string | null;
  showHistory?: boolean;
}

/**
 * F3.10's live-update wiring: an `entity_state_changed` SSE event for *this* entity
 * (emitted whenever a session mutates it -- the backend side of that emission is not
 * yet wired into any live turn loop, matching every other "seam built, not yet called"
 * gap in this codebase, e.g. `core.assembler.context_assembler`'s own `entity_state_
 * renderer`) invalidates the query, so the sheet refetches without a page reload.
 */
export function EntitySheetContainer({
  workspaceId,
  entityId,
  labelKeyPrefix,
  sessionId,
  showHistory,
}: EntitySheetContainerProps) {
  const queryClient = useQueryClient();
  const queryKey = useMemo(
    () => ["entity-view", workspaceId, entityId],
    [workspaceId, entityId],
  );
  const { events } = useSSE(sessionId ?? null);
  const lastHandledCount = useRef(0);

  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn: () => getEntityView(workspaceId, entityId),
  });

  useEffect(() => {
    if (events.length === lastHandledCount.current) return;
    const newEvents = events.slice(lastHandledCount.current);
    lastHandledCount.current = events.length;
    const relevant = newEvents.some(
      (e) =>
        e.kind === "entity_state_changed" &&
        typeof e.payload === "object" &&
        e.payload !== null &&
        (e.payload as { entity_id?: string }).entity_id === entityId,
    );
    if (relevant) {
      void queryClient.invalidateQueries({ queryKey });
    }
  }, [events, entityId, queryClient, queryKey]);

  if (isLoading) return <div className="text-sm text-muted-foreground">Loading…</div>;
  if (error || !data)
    return <div role="alert" className="text-sm text-destructive">Could not load this entity.</div>;

  return (
    <SheetView entity={data} labelKeyPrefix={labelKeyPrefix} showHistory={showHistory} />
  );
}
