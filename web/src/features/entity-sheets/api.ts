import { apiClient } from "@/lib/api-client/client";
import type { EntityView, HistoryEntry } from "./types";

async function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): Promise<T> {
  if (result.data === undefined) {
    throw result.error ?? new Error(`request failed with status ${result.response.status}`);
  }
  return result.data;
}

export async function getEntityView(workspaceId: string, entityId: string): Promise<EntityView> {
  const result = await apiClient.GET("/entities/{entity_id}", {
    params: { path: { entity_id: entityId }, query: { workspace_id: workspaceId } },
  });
  return unwrap(result) as unknown as Promise<EntityView>;
}

export async function getEntityHistory(
  entityId: string,
  fieldPath?: string,
): Promise<HistoryEntry[]> {
  const result = await apiClient.GET("/entities/{entity_id}/history", {
    params: {
      path: { entity_id: entityId },
      query: fieldPath ? { field_path: fieldPath } : undefined,
    },
  });
  return unwrap(result) as unknown as Promise<HistoryEntry[]>;
}
