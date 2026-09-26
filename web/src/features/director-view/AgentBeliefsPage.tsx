import { useParams } from "react-router-dom";
import { BackLink } from "@/components/BackLink";
import { useQuery } from "@tanstack/react-query";
import { PermissionDeniedError, agentBeliefs } from "./api";

/** "what does agent X currently believe" -- the agent's holder set joined with
 * each secret's own disclosure state. */
export function AgentBeliefsPage() {
  const { workspaceId, agentPrincipalId } = useParams<{
    workspaceId: string;
    agentPrincipalId: string;
  }>();

  const {
    data: beliefs,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["director-view-beliefs", workspaceId, agentPrincipalId],
    queryFn: () => agentBeliefs(workspaceId!, agentPrincipalId!),
    enabled: !!workspaceId && !!agentPrincipalId,
  });

  if (!workspaceId || !agentPrincipalId) return null;

  if (error instanceof PermissionDeniedError) {
    return (
      <p className="text-destructive" role="alert">
        You do not have permission to view this agent's beliefs.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <BackLink to={`/workspaces/${workspaceId}/director-view`} label="All holders" />
      <h2 className="text-lg font-medium">What this agent currently believes</h2>
      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {beliefs?.length === 0 && (
        <p className="text-muted-foreground">This agent holds no secrets.</p>
      )}
      <ul className="flex flex-col gap-2">
        {beliefs?.map((belief) => (
          <li key={belief.secret_id} className="rounded-md border border-border p-2 text-sm">
            secret {belief.secret_id} · {belief.holder_kind} · {belief.disclosure_state}
          </li>
        ))}
      </ul>
    </div>
  );
}
