import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";
import { useAuthStore } from "@/stores/auth";

/**
 * Two ways in: create a NEW organization (self-serve tenant signup — the default), or
 * join an existing one by its slug (the org owner tells you the slug; you start as a
 * viewer until promoted).
 */
export function RegisterPage() {
  const navigate = useNavigate();
  const setToken = useAuthStore((s) => s.setToken);
  const setTenantSlug = useAuthStore((s) => s.setTenantSlug);
  const [mode, setMode] = useState<"create" | "join">("create");
  const [organization, setOrganization] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      if (mode === "create") {
        const { data, error: apiError } = await apiClient.POST("/auth/signup", {
          body: { organization, email, password, display_name: displayName },
        });
        if (apiError || !data) {
          setError("Signup failed — the email may already be registered.");
          return;
        }
        setTenantSlug(data.tenant_slug);
        setToken(data.access_token);
      } else {
        const { data, error: apiError } = await apiClient.POST("/auth/register", {
          params: { header: { "x-pyrrhula-tenant": organization } },
          body: { email, password, display_name: displayName },
        });
        if (apiError || !data) {
          setError("Joining failed — check the organization slug, or the email is taken.");
          return;
        }
        setTenantSlug(organization);
        setToken(data.access_token);
      }
      navigate("/");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-6 px-4">
      <h1 className="text-2xl font-semibold tracking-tight">
        {mode === "create" ? "Create your organization" : "Join an organization"}
      </h1>
      <div className="flex gap-1 rounded-md border border-border p-1 text-sm">
        <button
          type="button"
          onClick={() => setMode("create")}
          className={`flex-1 rounded px-3 py-1.5 ${mode === "create" ? "bg-secondary font-medium" : "text-muted-foreground"}`}
        >
          New organization
        </button>
        <button
          type="button"
          onClick={() => setMode("join")}
          className={`flex-1 rounded px-3 py-1.5 ${mode === "join" ? "bg-secondary font-medium" : "text-muted-foreground"}`}
        >
          Join existing
        </button>
      </div>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <label className="flex flex-col gap-1 text-sm">
          {mode === "create" ? "Organization name" : "Organization slug"}
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={organization}
            onChange={(e) => setOrganization(e.target.value)}
            placeholder={mode === "create" ? "Acme Robotics" : "acme-robotics"}
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Display name
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            required
          />
        </label>
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
            minLength={8}
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
          {submitting
            ? "Working…"
            : mode === "create"
              ? "Create organization"
              : "Join organization"}
        </button>
      </form>
      <p className="text-sm text-muted-foreground">
        Already have an account? <Link to="/login" className="underline">Sign in</Link>
      </p>
    </div>
  );
}
