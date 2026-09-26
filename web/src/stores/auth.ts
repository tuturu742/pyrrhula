import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthState {
  token: string | null;
  tenantSlug: string | null;
  setToken: (token: string | null) => void;
  setTenantSlug: (slug: string | null) => void;
  logout: () => void;
}

/**
 * Session-local auth state. The JWT is also set as an httponly cookie by the
 * backend (see `api.routes.auth`), which is what actually protects it from XSS reading
 * it — this store's copy is only what lets the frontend attach an `Authorization: Bearer`
 * header explicitly (needed for the SSE `EventSource`, which can't send custom headers,
 * so it relies on the cookie instead; REST calls use this token).
 */
export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      tenantSlug: null,
      setToken: (token) => set({ token }),
      setTenantSlug: (tenantSlug) => set({ tenantSlug }),
      logout: () => set({ token: null }),
    }),
    { name: "pyrrhula-auth" },
  ),
);
