import { apiClient } from "@/lib/api-client/client";

/**
 * Thrown for any 403 from an `/overseer/*` or `/secrets` call. The distinguishing shape
 * this feature's own acceptance criterion cares about: a denied principal must see a
 * *permission* error, never an empty list -- so every page here must be able to tell
 * "denied" apart from "genuinely nothing here" and render each one differently.
 */
export class PermissionDeniedError extends Error {
  constructor(message = "permission denied") {
    super(message);
    this.name = "PermissionDeniedError";
  }
}

async function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): Promise<T> {
  if (result.response.status === 403) {
    throw new PermissionDeniedError();
  }
  if (result.data === undefined) {
    throw result.error ?? new Error(`request failed with status ${result.response.status}`);
  }
  return result.data;
}

export async function checkAccess(workspaceId: string) {
  const result = await apiClient.GET("/overseer/access", {
    params: { query: { workspace_id: workspaceId } },
  });
  return unwrap(result);
}

export async function listSecretsForWorkspace(workspaceId: string) {
  const result = await apiClient.GET("/secrets", {
    params: { query: { workspace_id: workspaceId } },
  });
  return unwrap(result);
}

export async function listSecretHolders(workspaceId: string, secretId: string) {
  const result = await apiClient.GET("/overseer/secrets/{secret_id}/holders", {
    params: { path: { secret_id: secretId }, query: { workspace_id: workspaceId } },
  });
  return unwrap(result);
}

/** The workspace's personas, for naming holders: a holder is a principal id on the
 * wire, and `principal_id` is what maps it back to a name. */
export async function listWorkspacePersonas(workspaceId: string) {
  const result = await apiClient.GET("/workspaces/{workspace_id}/agents", {
    params: { path: { workspace_id: workspaceId } },
  });
  return unwrap(result);
}

/** The plaintext read -- each call is exactly one `OverseerService.inspect()` invocation
 * and therefore exactly one new audit row; callers must call this once per deliberate
 * expansion, never speculatively/in bulk. */
export async function inspectSecret(workspaceId: string, secretId: string) {
  const result = await apiClient.GET("/overseer/secrets/{secret_id}", {
    params: { path: { secret_id: secretId }, query: { workspace_id: workspaceId } },
  });
  return unwrap(result);
}

export async function disclosureTimeline(workspaceId: string, secretId: string) {
  const result = await apiClient.GET("/overseer/secrets/{secret_id}/timeline", {
    params: { path: { secret_id: secretId }, query: { workspace_id: workspaceId } },
  });
  return unwrap(result);
}

export async function agentBeliefs(workspaceId: string, agentPrincipalId: string) {
  const result = await apiClient.GET("/overseer/agents/{agent_principal_id}/beliefs", {
    params: { path: { agent_principal_id: agentPrincipalId }, query: { workspace_id: workspaceId } },
  });
  return unwrap(result);
}
