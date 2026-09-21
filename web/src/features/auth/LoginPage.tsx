import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useAuthStore } from "@/stores/auth";
import { usePublicConfig } from "./usePublicConfig";

export function LoginPage() {
  const navigate = useNavigate();
  const { loading: configLoading, config } = usePublicConfig();
  const location = useLocation();
  const setToken = useAuthStore((s) => s.setToken);
  const setTenantSlug = useAuthStore((s) => s.setTenantSlug);
  const storedSlug = useAuthStore((s) => s.tenantSlug);
  const [tenant, setTenant] = useState(storedSlug ?? "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      // Store the slug FIRST: the client middleware stamps X-Pyrrhula-Tenant from the
      // store, and a stale slug from a previous session must not outlive this submit.
      // In single-tenant mode there is no slug to send -- the server resolves its one
      // organization -- and clearing the store matters most there, since a slug left
      // over from a multi-tenant session would otherwise be stamped on every later
      // request.
      const slug = config.single_tenant ? "" : tenant;
      setTenantSlug(slug || null);
      const { data, error: apiError } = await apiClient.POST("/auth/login", {
        ...(slug ? { params: { header: { "x-pyrrhula-tenant": slug } } } : {}),
        body: { email, password },
      });
      if (apiError || !data) {
        setError(
          config.single_tenant
            ? "Invalid email or password."
            : "Invalid organization, email, or password.",
        );
        return;
      }
      setToken(data.access_token);
      // A deep link that bounced through the guard goes back where it was headed;
      // everyone else goes to "/", which decides. Admins used to be sent straight to
      // /admin/tenants from here, which meant two places knew where an admin lands and
      // only one of them learned that a deployment with no retrieval model should start
      // at Models instead (see useAdminLanding).
      const returnTo = (location.state as { returnTo?: string } | null)
        ?.returnTo;
      navigate(returnTo || "/");
    } finally {
      setSubmitting(false);
    }
  }

  if (configLoading) return null;

  return (
    <div className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-6 px-4">
      <h1 className="text-2xl font-semibold tracking-tight">
        Sign in to Pyrrhula
      </h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        {!config.single_tenant && (
          <label className="flex flex-col gap-1 text-sm">
            Organization
            <input
              className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              placeholder="acme-robotics"
              required
            />
          </label>
        )}
        <label className="flex flex-col gap-1 text-sm">
          Email
          <input
            type="email"
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Password
          <input
            type="password"
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="text-sm text-muted-foreground">
        No account?{" "}
        <Link to="/register" className="underline">
          Register
        </Link>
      </p>
    </div>
  );
}
