import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useLabel } from "@/lib/vocabulary/useLabel";

/**
 * The cast's live state during a session: every entity in the workspace that runs a state
 * machine (a character sheet's health/condition track, a work item's lifecycle) with its
 * current state shown as a chip. Reads the name-level entity list -- the same
 * `fsm_states` the assembler drives from resolutions -- so an overseer watching a fight
 * sees Bram go healthy -> wounded -> unconscious without opening each sheet.
 */
export function CharactersPanel({
  workspaceId,
  sessionId,
}: {
  workspaceId: string | undefined;
  sessionId?: string;
}) {
  const t = useLabel();
  const { data: entities } = useQuery({
    enabled: Boolean(workspaceId),
    queryKey: ["session-entities", workspaceId],
    // Poll: state machines advance mid-session as resolutions land.
    refetchInterval: 5000,
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/entities", {
        params: { query: { workspace_id: workspaceId! } },
      });
      if (error) throw error;
      return data;
    },
  });

  const withState = (entities ?? []).filter(
    (e) => e.fsm_states && Object.keys(e.fsm_states).length > 0,
  );
  if (withState.length === 0) return null;

  return (
    // Sticky on a wide screen: the cast's state is what a reader scrolling a long
    // transcript keeps wanting to glance at, and it used to scroll away with the top of
    // the page. Laid out as a compact row there, so it pins without eating the view.
    <section className="flex flex-col gap-2 lg:sticky lg:top-0 lg:z-10 lg:-mx-2 lg:rounded-md lg:bg-background/95 lg:px-2 lg:py-2 lg:backdrop-blur">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        Cast
      </h2>
      <ul className="flex flex-col gap-2 lg:flex-row lg:flex-wrap">
        {withState.map((e) => (
          <li
            key={e.id}
            className="flex items-center justify-between gap-2 rounded-md border border-border bg-background px-3 py-2"
          >
            <Link
              to={`/workspaces/${workspaceId}/entities/${e.id}`}
              // The sheet's back link returns here, not to the personas list.
              state={
                sessionId
                  ? { from: { to: `/sessions/${sessionId}`, label: "Back to the session" } }
                  : undefined
              }
              className="truncate text-sm font-medium hover:underline"
              title={`Open ${e.name}'s sheet`}
            >
              {e.name}
            </Link>
            <div className="flex shrink-0 flex-wrap items-center gap-1">
              {Object.entries(e.fsm_states as Record<string, string>).map(([machine, state]) => (
                <span
                  key={machine}
                  title={machine}
                  className={
                    "rounded-full px-2 py-0.5 text-[11px] font-medium " +
                    stateTone(state)
                  }
                >
                  {t(`status.${state}`) || t(`condition.${state}`) || state}
                </span>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Severity read at a glance: green for healthy/steady, amber for hurt, red for down. */
function stateTone(state: string): string {
  const s = state.toLowerCase();
  if (["healthy", "steady", "fine", "ok"].includes(s))
    return "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400";
  if (["dead", "unconscious", "panicked", "petrified", "destroyed"].includes(s))
    return "bg-destructive/15 text-destructive";
  if (["wounded", "bloodied", "shaken", "poisoned", "stunned", "prone"].includes(s))
    return "bg-amber-500/15 text-amber-600 dark:text-amber-400";
  return "bg-muted text-muted-foreground";
}
