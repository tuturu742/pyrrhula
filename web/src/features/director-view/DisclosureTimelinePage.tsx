import { useParams } from "react-router-dom";
import { BackLink } from "@/components/BackLink";
import { useQuery } from "@tanstack/react-query";
import { PermissionDeniedError, disclosureTimeline } from "./api";

/** `secret_disclosure_event` stream for one secret, oldest first, with a mode
 * badge (full/hint/inferred/leaked) per row. */
export function DisclosureTimelinePage() {
  const { workspaceId, secretId } = useParams<{ workspaceId: string; secretId: string }>();

  const {
    data: events,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["director-view-timeline", workspaceId, secretId],
    queryFn: () => disclosureTimeline(workspaceId!, secretId!),
    enabled: !!workspaceId && !!secretId,
  });

  if (!workspaceId || !secretId) return null;

  if (error instanceof PermissionDeniedError) {
    return (
      <p className="text-destructive" role="alert">
        You do not have permission to view this disclosure timeline.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <BackLink to={`/workspaces/${workspaceId}/director-view`} label="All holders" />
      <h2 className="text-lg font-medium">Disclosure timeline</h2>
      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {events?.length === 0 && (
        <p className="text-muted-foreground">No disclosure events yet.</p>
      )}
      <ul className="flex flex-col gap-2">
        {events?.map((event) => (
          <li
            key={`${event.session_id}-${event.event_seq}`}
            className="flex items-center gap-2 rounded-md border border-border p-2 text-sm"
          >
            <span className="rounded-full border border-border px-2 py-0.5 text-xs uppercase">
              {event.mode}
            </span>
            <span className="text-muted-foreground">
              session {event.session_id} · turn {event.event_seq}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
