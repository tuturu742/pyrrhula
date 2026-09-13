import { useParams } from "react-router-dom";
import { BackLink } from "@/components/BackLink";
import { EntitySheetContainer } from "./EntitySheetContainer";

/** Route mount for the entity sheet — the widget system was fully built and never
 * reachable from any screen. Linked from personas with a bound entity. */
export function EntitySheetPage() {
  const { workspaceId, entityId } = useParams<{ workspaceId: string; entityId: string }>();
  if (!workspaceId || !entityId) return null;
  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <BackLink to={`/workspaces/${workspaceId}/agents`} label="Back to personas" />
      <EntitySheetContainer
        workspaceId={workspaceId}
        entityId={entityId}
        labelKeyPrefix="sheet"
        showHistory
      />
    </div>
  );
}
