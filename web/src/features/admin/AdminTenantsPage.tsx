import { useState } from "react";
import { toast } from "sonner";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

const input =
  "rounded-md border border-input bg-transparent px-3 py-1.5 text-sm";
const btn =
  "rounded-md border border-input px-3 py-1.5 text-sm hover:bg-secondary/50 disabled:opacity-50";
const btnPrimary =
  "rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50";

/** Platform-admin: every tenant on the deployment — create, deactivate, pin workflow and
 * overlay, and manage each tenant's user accounts. */
export function AdminTenantsPage() {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [expandedSection, setExpandedSection] = useState<"users" | "mcp" | "egress">("users");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tenants = useQuery({
    queryKey: ["admin", "tenants"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/tenants");
      if (error) throw error;
      return data;
    },
  });
  const workflows = useQuery({
    queryKey: ["admin", "workflows"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/workflows");
      if (error) throw error;
      return data;
    },
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "tenants"] });

  const setWorkflow = useMutation({
    mutationFn: async (vars: { tenantId: string; workflowKey: string | null }) => {
      const { error } = await apiClient.POST("/admin/tenants/{tenant_id}/workflow", {
        params: { path: { tenant_id: vars.tenantId } },
        body: { workflow_key: vars.workflowKey },
      });
      if (error) throw error;
    },
    onSuccess: invalidate,
    onError: (e) => setError(String((e as { detail?: string })?.detail ?? e)),
  });

  const toggleActive = useMutation({
    mutationFn: async (vars: { tenantId: string; deactivated: boolean }) => {
      const path = vars.deactivated
        ? "/admin/tenants/{tenant_id}/reactivate"
        : "/admin/tenants/{tenant_id}/deactivate";
      const { error } = await apiClient.POST(path, {
        params: { path: { tenant_id: vars.tenantId } },
      });
      if (error) throw error;
    },
    onSuccess: invalidate,
    onError: (e) => setError(String((e as { detail?: string })?.detail ?? e)),
  });

  const verifyAudit = useMutation({
    mutationFn: async (tenantId: string) => {
      const { data, error } = await apiClient.GET("/admin/tenants/{tenant_id}/audit/verify", {
        params: { path: { tenant_id: tenantId } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      const d = data as { ok?: boolean; broken_row_ids?: string[] };
      if (d.ok) toast.success("Audit chain intact.");
      else toast.error(`Audit chain BROKEN at: ${(d.broken_row_ids ?? []).join(", ")}`);
    },
    onError: () => toast.error("Verification failed to run."),
  });

  if (tenants.isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (tenants.error)
    return <p className="text-sm text-destructive">Failed to load tenants.</p>;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold tracking-tight">Tenants</h1>
        <button type="button" className={btnPrimary} onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancel" : "New tenant"}
        </button>
      </div>
      <SignupSwitch onError={setError} />
      {error && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}
      {creating && (
        <CreateTenantForm
          onDone={() => {
            setCreating(false);
            invalidate();
          }}
          onError={setError}
        />
      )}

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-secondary/50 text-left text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">Tenant</th>
              <th className="px-3 py-2 font-medium">Members</th>
              <th className="px-3 py-2 font-medium">Agents</th>
              <th className="px-3 py-2 font-medium">Workflow</th>
              <th className="px-3 py-2 font-medium">Overlay</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {(tenants.data ?? []).map((t) => (
              <>
                <tr key={t.id} className="border-t border-border">
                  <td className="px-3 py-2">
                    <div className="font-medium">{t.name}</div>
                    <div className="text-xs text-muted-foreground">{t.slug}</div>
                  </td>
                  <td className="px-3 py-2">{t.member_count}</td>
                  <td className="px-3 py-2">{t.agent_count}</td>
                  <td className="px-3 py-2">
                    <select
                      className={input}
                      value={t.workflow_key ?? ""}
                      onChange={(e) =>
                        setWorkflow.mutate({
                          tenantId: t.id,
                          workflowKey: e.target.value || null,
                        })
                      }
                    >
                      <option value="">—</option>
                      {(workflows.data ?? []).map((w) => (
                        <option key={w.key} value={w.key}>
                          {w.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{t.overlay_key ?? "—"}</td>
                  <td className="px-3 py-2">
                    {t.deactivated ? (
                      <span className="text-destructive">deactivated</span>
                    ) : (
                      <span className="text-muted-foreground">active</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      className={btn}
                      onClick={() => {
                        setExpandedSection("users");
                        setExpanded(expanded === t.id && expandedSection === "users" ? null : t.id);
                      }}
                    >
                      Users
                    </button>{" "}
                    <button
                      type="button"
                      className={btn}
                      onClick={() => {
                        setExpandedSection("mcp");
                        setExpanded(expanded === t.id && expandedSection === "mcp" ? null : t.id);
                      }}
                    >
                      MCP
                    </button>{" "}
                    <button
                      type="button"
                      className={btn}
                      onClick={() => {
                        setExpandedSection("egress");
                        setExpanded(expanded === t.id && expandedSection === "egress" ? null : t.id);
                      }}
                    >
                      Egress
                    </button>{" "}
                    <button
                      type="button"
                      className={btn}
                      onClick={() => verifyAudit.mutate(t.id)}
                      disabled={verifyAudit.isPending}
                    >
                      Verify audit
                    </button>{" "}
                    <button
                      type="button"
                      className={btn}
                      onClick={() =>
                        toggleActive.mutate({ tenantId: t.id, deactivated: t.deactivated })
                      }
                    >
                      {t.deactivated ? "Reactivate" : "Deactivate"}
                    </button>
                  </td>
                </tr>
                {expanded === t.id && (
                  <tr className="border-t border-border bg-secondary/20">
                    <td colSpan={7} className="px-3 py-3">
                      {expandedSection === "users" ? (
                        <TenantUsers tenantId={t.id} onError={setError} />
                      ) : expandedSection === "mcp" ? (
                        <TenantMcp tenantId={t.id} onError={setError} />
                      ) : (
                        <TenantEgress tenantId={t.id} onError={setError} />
                      )}
                    </td>
                  </tr>
                )}
              </>
            ))}
            {tenants.isSuccess && (tenants.data ?? []).length === 0 && (
              <tr>
                <td colSpan={8} className="py-6 text-center text-sm text-muted-foreground">
                  No organizations yet — create the first one above.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CreateTenantForm({
  onDone,
  onError,
}: {
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [ownerEmail, setOwnerEmail] = useState("");
  const [ownerPassword, setOwnerPassword] = useState("");

  const create = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/admin/tenants", {
        body: {
          slug,
          name,
          owner_email: ownerEmail || null,
          owner_password: ownerPassword || null,
          owner_display_name: "Owner",
        },
      });
      if (error) throw error;
    },
    onSuccess: onDone,
    onError: (e) => onError(String((e as { detail?: string })?.detail ?? e)),
  });

  return (
    <form
      className="flex flex-wrap items-end gap-3 rounded-lg border border-border p-4"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <label className="flex flex-col gap-1 text-sm">
        Slug
        <input className={input} value={slug} onChange={(e) => setSlug(e.target.value)} required />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Name
        <input className={input} value={name} onChange={(e) => setName(e.target.value)} required />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Owner email (optional)
        <input
          className={input}
          type="email"
          value={ownerEmail}
          onChange={(e) => setOwnerEmail(e.target.value)}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Owner password
        <input
          className={input}
          type="password"
          value={ownerPassword}
          onChange={(e) => setOwnerPassword(e.target.value)}
        />
      </label>
      <button type="submit" className={btnPrimary} disabled={create.isPending}>
        Create
      </button>
    </form>
  );
}

function TenantUsers({
  tenantId,
  onError,
}: {
  tenantId: string;
  onError: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("participant");

  const users = useQuery({
    queryKey: ["admin", "tenants", tenantId, "users"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/tenants/{tenant_id}/users", {
        params: { path: { tenant_id: tenantId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "tenants", tenantId, "users"] });

  const createUser = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/admin/tenants/{tenant_id}/users", {
        params: { path: { tenant_id: tenantId } },
        body: { email, password, display_name: displayName, role },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setEmail("");
      setPassword("");
      setDisplayName("");
      invalidate();
    },
    onError: (e) => onError(String((e as { detail?: string })?.detail ?? e)),
  });

  const toggleUser = useMutation({
    mutationFn: async (vars: { principalId: string; disabled: boolean }) => {
      const path = vars.disabled
        ? "/admin/tenants/{tenant_id}/users/{principal_id}/reactivate"
        : "/admin/tenants/{tenant_id}/users/{principal_id}/deactivate";
      const { error } = await apiClient.POST(path, {
        params: { path: { tenant_id: tenantId, principal_id: vars.principalId } },
      });
      if (error) throw error;
    },
    onSuccess: invalidate,
    onError: (e) => onError(String((e as { detail?: string })?.detail ?? e)),
  });

  return (
    <div className="flex flex-col gap-3">
      <table className="w-full text-sm">
        <tbody>
          {(users.data ?? []).map((u) => (
            <tr key={u.principal_id} className="border-b border-border/50 last:border-0">
              <td className="py-1.5">{u.display_name}</td>
              <td className="py-1.5 text-muted-foreground">{u.email ?? "—"}</td>
              <td className="py-1.5">{u.role}</td>
              <td className="py-1.5">{u.disabled ? "disabled" : "active"}</td>
              <td className="py-1.5 text-right">
                <button
                  type="button"
                  className={btn}
                  onClick={() =>
                    toggleUser.mutate({ principalId: u.principal_id, disabled: u.disabled })
                  }
                >
                  {u.disabled ? "Reactivate" : "Deactivate"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          createUser.mutate();
        }}
      >
        <input
          className={input}
          type="email"
          placeholder="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <input
          className={input}
          type="password"
          placeholder="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <input
          className={input}
          placeholder="display name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          required
        />
        <select className={input} value={role} onChange={(e) => setRole(e.target.value)}>
          {["owner", "admin", "editor", "participant", "viewer"].map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button type="submit" className={btn} disabled={createUser.isPending}>
          Add user
        </button>
      </form>
    </div>
  );
}

function TenantMcp({
  tenantId,
  onError,
}: {
  tenantId: string;
  onError: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [key, setKey] = useState("");
  const [url, setUrl] = useState("");
  const [enabledTools, setEnabledTools] = useState("");
  const [effectfulTools, setEffectfulTools] = useState("");

  const grants = useQuery({
    queryKey: ["admin", "tenants", tenantId, "mcp"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/tenants/{tenant_id}/mcp-servers", {
        params: { path: { tenant_id: tenantId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "tenants", tenantId, "mcp"] });

  const put = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/admin/tenants/{tenant_id}/mcp-servers", {
        params: { path: { tenant_id: tenantId } },
        body: {
          key,
          url,
          enabled_tools: enabledTools.split(",").map((s) => s.trim()).filter(Boolean),
          effectful_tools: effectfulTools.split(",").map((s) => s.trim()).filter(Boolean),
          require_confirmation: true,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setKey("");
      setUrl("");
      setEnabledTools("");
      setEffectfulTools("");
      invalidate();
    },
    onError: (e) => onError(String((e as { detail?: string })?.detail ?? e)),
  });

  const remove = useMutation({
    mutationFn: async (grantKey: string) => {
      const { error } = await apiClient.DELETE(
        "/admin/tenants/{tenant_id}/mcp-servers/{key}",
        { params: { path: { tenant_id: tenantId, key: grantKey } } },
      );
      if (error) throw error;
    },
    onSuccess: invalidate,
    onError: (e) => onError(String((e as { detail?: string })?.detail ?? e)),
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="text-sm font-medium">
        MCP capabilities{" "}
        <span className="font-normal text-muted-foreground">
          — attached to every workspace of this tenant, on top of its workflow; agents can
          only call the tools listed here.
        </span>
      </div>
      <table className="w-full text-sm">
        <tbody>
          {(grants.data ?? []).map((g) => (
            <tr key={g.key} className="border-b border-border/50 last:border-0">
              <td className="py-1.5 font-medium">{g.key}</td>
              <td className="py-1.5 text-muted-foreground">{g.url}</td>
              <td className="py-1.5">tools: {g.enabled_tools.join(", ") || "none"}</td>
              <td className="py-1.5 text-muted-foreground">
                effectful: {g.effectful_tools.join(", ") || "none"}
              </td>
              <td className="py-1.5 text-right">
                <button type="button" className={btn} onClick={() => remove.mutate(g.key)}>
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          put.mutate();
        }}
      >
        <input
          className={input}
          placeholder="key (e.g. engine)"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          required
        />
        <input
          className={`${input} w-72`}
          placeholder="http://your-mcp-server:8090"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
        />
        <input
          className={`${input} w-56`}
          placeholder="enabled tools, comma-separated"
          value={enabledTools}
          onChange={(e) => setEnabledTools(e.target.value)}
          required
        />
        <input
          className={`${input} w-56`}
          placeholder="effectful tools (subset)"
          value={effectfulTools}
          onChange={(e) => setEffectfulTools(e.target.value)}
        />
        <button type="submit" className={btn} disabled={put.isPending}>
          Attach
        </button>
      </form>
    </div>
  );
}


/** Egress policy: {purpose: ["local"] or ["local","cloud"]}. An absent purpose is
 * permissive (the platform default); an empty list blocks that purpose entirely. */
function TenantEgress({
  tenantId,
  onError,
}: {
  tenantId: string;
  onError: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);

  const policy = useQuery({
    queryKey: ["admin", "tenants", tenantId, "egress"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/tenants/{tenant_id}/egress-policy", {
        params: { path: { tenant_id: tenantId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async () => {
      const parsed = JSON.parse(draft ?? "{}");
      const { error } = await apiClient.PUT("/admin/tenants/{tenant_id}/egress-policy", {
        params: { path: { tenant_id: tenantId } },
        body: { policy: parsed },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setDraft(null);
      queryClient.invalidateQueries({ queryKey: ["admin", "tenants", tenantId, "egress"] });
    },
    onError: (e) => onError(String((e as { detail?: string })?.detail ?? e)),
  });

  const value = draft ?? JSON.stringify((policy.data as { policy?: object })?.policy ?? {}, null, 2);
  return (
    <div className="flex flex-col gap-2">
      <div className="text-sm font-medium">
        Egress policy{" "}
        <span className="font-normal text-muted-foreground">
          — which provider kinds each purpose may reach, e.g.{" "}
          <code>{'{"generation": ["local"]}'}</code>. Absent purpose = allowed everywhere.
        </span>
      </div>
      <textarea
        className="min-h-28 rounded-md border border-input bg-transparent p-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        value={value}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div>
        <button type="button" className={btn} disabled={save.isPending} onClick={() => save.mutate()}>
          Save policy
        </button>
      </div>
    </div>
  );
}

/** Whether strangers may create their own organization (POST /auth/signup). A runtime
 * policy, so it lives here and not in the deployment's environment; the environment
 * only says what a fresh install starts with. */
function SignupSwitch({ onError }: { onError: (message: string) => void }) {
  const queryClient = useQueryClient();
  const signup = useQuery({
    queryKey: ["admin-signup"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/signup");
      if (error) throw error;
      return data;
    },
  });
  const save = useMutation({
    mutationFn: async (allowed: boolean) => {
      const { error } = await apiClient.PUT("/admin/signup", { body: { allowed } });
      if (error) throw error;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["admin-signup"] }),
    onError: (e) => onError(String((e as { detail?: string })?.detail ?? e)),
  });
  if (!signup.data) return null;
  return (
    <label className="flex items-center gap-3 rounded-md border border-border px-3 py-2 text-sm">
      <input
        type="checkbox"
        checked={signup.data.allowed}
        disabled={save.isPending}
        onChange={(e) => save.mutate(e.target.checked)}
      />
      <span>
        <span className="font-medium">Self-serve signup</span>{" "}
        <span className="text-muted-foreground">
          — strangers may create their own organization. Currently{" "}
          {signup.data.allowed ? "on" : "off"}; this deployment started{" "}
          {signup.data.environment_default ? "on" : "off"}.
        </span>
      </span>
    </label>
  );
}
