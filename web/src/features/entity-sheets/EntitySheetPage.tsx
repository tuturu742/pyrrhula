import { useLocation, useParams } from "react-router-dom";
import { BackLink } from "@/components/BackLink";
import { useWorkspaceVocabulary } from "@/lib/vocabulary/useWorkspaceVocabulary";
import { EntitySheetContainer } from "./EntitySheetContainer";

/** Route mount for the entity sheet — the widget system was fully built and never
 * reachable from any screen. Linked from personas with a bound entity. */
export function EntitySheetPage() {
  const { workspaceId, entityId } = useParams<{ workspaceId: string; entityId: string }>();
  // Where the reader came from, when the link that brought them here said so (the
  // session's cast panel does). A sheet opened mid-session used to send "back" to the
  // personas list, which is not where the reader was.
  const from = (useLocation().state as { from?: { to: string; label: string } } | null)?.from;
  // The sheet's group headings are overlay keys (`group.attributes`); without the
  // workspace's overlay loaded they rendered as the keys themselves.
  useWorkspaceVocabulary(workspaceId);
  if (!workspaceId || !entityId) return null;
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <BackLink
        to={from?.to ?? `/workspaces/${workspaceId}/agents`}
        label={from?.label ?? "Back to personas"}
      />
      <EntitySheetContainer
        workspaceId={workspaceId}
        entityId={entityId}
        labelKeyPrefix="sheet"
        showHistory
      />
    </div>
  );
}
