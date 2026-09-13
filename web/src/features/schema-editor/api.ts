import { apiClient } from "@/lib/api-client/client";
import type { EntitySchemaDefinitionDoc, SchemaResponse, SchemaValidationIssue } from "./types";

async function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): Promise<T> {
  if (result.data === undefined) {
    throw result.error ?? new Error(`request failed with status ${result.response.status}`);
  }
  return result.data;
}

export interface ValidateSchemaResult {
  valid: boolean;
  issues: SchemaValidationIssue[];
}

export async function validateSchema(
  definition: EntitySchemaDefinitionDoc,
): Promise<ValidateSchemaResult> {
  const result = await apiClient.POST("/entities/schemas/validate", {
    body: { definition: definition as unknown as Record<string, unknown> },
  });
  return unwrap(result) as unknown as Promise<ValidateSchemaResult>;
}

export async function listSchemaTemplates(): Promise<SchemaResponse[]> {
  const result = await apiClient.GET("/entities/schemas/templates");
  return unwrap(result) as unknown as Promise<SchemaResponse[]>;
}

export async function listSchemas(workspaceId: string | null): Promise<SchemaResponse[]> {
  const result = await apiClient.GET("/entities/schemas", {
    params: { query: workspaceId ? { workspace_id: workspaceId } : {} },
  });
  return unwrap(result) as unknown as Promise<SchemaResponse[]>;
}

export async function listSchemaVersions(key: string, workspaceId: string | null): Promise<SchemaResponse[]> {
  const result = await apiClient.GET("/entities/schemas/versions", {
    params: { query: workspaceId ? { key, workspace_id: workspaceId } : { key } },
  });
  return unwrap(result) as unknown as Promise<SchemaResponse[]>;
}

export async function getSchema(schemaId: string): Promise<SchemaResponse> {
  const result = await apiClient.GET("/entities/schemas/{schema_id}", {
    params: { path: { schema_id: schemaId } },
  });
  return unwrap(result) as unknown as Promise<SchemaResponse>;
}

export interface CreateSchemaFailure {
  detail: { issues: SchemaValidationIssue[] };
}

export async function createSchema(
  key: string,
  workspaceId: string | null,
  definition: EntitySchemaDefinitionDoc,
): Promise<SchemaResponse> {
  const result = await apiClient.POST("/entities/schemas", {
    body: {
      key,
      workspace_id: workspaceId,
      definition: definition as unknown as Record<string, unknown>,
    },
  });
  return unwrap(result) as unknown as Promise<SchemaResponse>;
}
