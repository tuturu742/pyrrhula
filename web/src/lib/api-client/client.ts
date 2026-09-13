import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";
import { useAuthStore } from "@/stores/auth";

/**
 * The one HTTP client the app uses. `openapi-fetch` types every request/response from the
 * generated `schema.ts` (see package.json's `generate-api-types` script) — nothing in the
 * app hand-writes a response type; if the backend's OpenAPI schema changes and this isn't
 * regenerated, `pnpm typecheck` fails at the call sites instead of silently drifting.
 */
export const apiClient = createClient<paths>({ baseUrl: "/api" });

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    const token = useAuthStore.getState().token;
    if (token) {
      request.headers.set("Authorization", `Bearer ${token}`);
    }
    // Multi-tenant deployments: which organization this browser session belongs to
    // (single-tenant deployments resolve their default without it). NEVER override a
    // header the caller set explicitly -- the login form names its organization, and
    // stamping the PREVIOUS session's stored slug over it sent logins to the wrong
    // tenant ("invalid email or password" for perfectly good credentials).
    const tenantSlug = useAuthStore.getState().tenantSlug;
    if (tenantSlug && !request.headers.has("X-Pyrrhula-Tenant")) {
      request.headers.set("X-Pyrrhula-Tenant", tenantSlug);
    }
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401) {
      useAuthStore.getState().logout();
    }
    return response;
  },
};

apiClient.use(authMiddleware);
