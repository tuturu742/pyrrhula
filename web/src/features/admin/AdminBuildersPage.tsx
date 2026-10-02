import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import type { components } from "@/lib/api-client/schema";

type Builder = components["schemas"]["BuilderOut"];

const input = "rounded-md border border-input bg-transparent px-3 py-1.5 text-sm";
const btn =
  "rounded-md border border-input px-3 py-1.5 text-sm hover:bg-secondary/50 disabled:opacity-50";
const btnPrimary =
  "rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50";

function detailOf(e: unknown): string {
  const detail = (e as { detail?: unknown })?.detail;
  return typeof detail === "string" ? detail : String(detail ?? e);
}

/**
 * Platform-admin: the external systems that build organizations' images.
 *
 * Pyrrhula has no builder of its own. An organization's Dockerfile -- and every RUN step
 * in it -- executes on a system declared here: a CI pipeline, a webhook in front of a
 * build farm. Each one pushes to one declared registry, inside the organization's own
 * namespace there, and the platform verifies and smoke-tests what comes back.
 */
export function AdminBuildersPage() {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const builders = useQuery({
    queryKey: ["admin", "image-builders"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/image-builders");
      if (error) throw error;
      return data;
    },
  });
  const registries = useQuery({
    queryKey: ["admin", "image-registries"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/image-registries");
      if (error) throw error;
      return data;
    },
  });
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "image-builders"] });

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Image builders</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Where organizations' Dockerfiles are built. Every RUN step runs on the system
          you declare here, never on Pyrrhula's own machines; what it pushes is verified
          against the registry and smoke-tested before anything uses it.
        </p>
      </header>

      {error && (
        <p className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}

      {(registries.data ?? []).length === 0 && (
        <p className="text-sm text-muted-foreground">
          Declare a registry first: a builder pushes to one.
        </p>
      )}

      <section className="flex flex-col gap-3">
        {builders.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">No builders declared.</p>
        )}
        {builders.data?.map((b) => (
          <BuilderCard key={b.key} builder={b} onChange={invalidate} onError={setError} />
        ))}
      </section>

      {(registries.data ?? []).length > 0 && (
        <AddBuilderForm
          registries={(registries.data ?? []).map((r) => r.key)}
          onAdded={invalidate}
          onError={setError}
        />
      )}
      <LimitsCard onError={setError} />
    </div>
  );
}

function BuilderCard({
  builder,
  onChange,
  onError,
}: {
  builder: Builder;
  onChange: () => void;
  onError: (message: string | null) => void;
}) {
  const [secret, setSecret] = useState("");
  const [token, setToken] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [pushUser, setPushUser] = useState("");
  const [pushPassword, setPushPassword] = useState("");
  const [probe, setProbe] = useState<string | null>(null);
  const [isolation, setIsolation] = useState<string[] | null>(null);
  const needsAck = builder.kind === "portainer";
  const [allowed, setAllowed] = useState(
    builder.allowed_tenants === null ? "" : builder.allowed_tenants.join("\n"),
  );

  const path = { params: { path: { key: builder.key } } };

  const saveCredential = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/admin/image-builders/{key}/credential", {
        ...path,
        body: {
          signing_secret: secret,
          token,
          api_key: apiKey,
          push_username: pushUser,
          push_password: pushPassword,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setSecret("");
      setToken("");
      setApiKey("");
      setPushPassword("");
      onError(null);
      onChange();
    },
    onError: (e) => onError(detailOf(e)),
  });

  const patch = useMutation({
    mutationFn: async (body: components["schemas"]["BuilderFields"]) => {
      const { error } = await apiClient.PATCH("/admin/image-builders/{key}", { ...path, body });
      if (error) throw error;
    },
    onSuccess: () => {
      onError(null);
      onChange();
    },
    onError: (e) => onError(detailOf(e)),
  });

  const test = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/admin/image-builders/{key}/test", path);
      if (error) throw error;
      return data;
    },
    onSuccess: (r) => setProbe(`${r.ok ? "✓" : "✗"} ${r.detail}`),
    onError: (e) => setProbe(`✗ ${detailOf(e)}`),
  });

  const runIsolationProbe = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/admin/image-builders/{key}/isolation-probe", {
        ...path,
        body: { extra_targets: [] },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (r) => setIsolation(r.lines),
    onError: (e) => onError(detailOf(e)),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.DELETE("/admin/image-builders/{key}", path);
      if (error) throw error;
    },
    onSuccess: onChange,
    onError: (e) => onError(detailOf(e)),
  });

  const config = builder.config as Record<string, unknown>;

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="font-medium">{builder.label || builder.key}</span>{" "}
          <code className="text-xs text-muted-foreground">{builder.key}</code>
          <span className="ml-2 rounded-full bg-secondary px-2 py-0.5 text-xs">{builder.kind}</span>
          {!builder.enabled && (
            <span className="ml-2 rounded-full bg-secondary px-2 py-0.5 text-xs">disabled</span>
          )}
          {needsAck && !builder.isolation_ack && (
            <span className="ml-2 rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-400">
              isolation not acknowledged — unavailable
            </span>
          )}
          {!builder.has_credential && (
            <span className="ml-2 rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-400">
              no credential — unavailable
            </span>
          )}
        </div>
        <div className="flex gap-2">
          <button type="button" className={btn} onClick={() => test.mutate()} disabled={test.isPending}>
            {test.isPending ? "Testing…" : "Test"}
          </button>
          <button type="button" className={btn} onClick={() => patch.mutate({ enabled: !builder.enabled })}>
            {builder.enabled ? "Disable" : "Enable"}
          </button>
          <button
            type="button"
            className={btn}
            onClick={() => {
              if (confirm(`Remove builder "${builder.key}"?`)) remove.mutate();
            }}
          >
            Remove
          </button>
        </div>
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Pushes to</dt>
        <dd>
          <code>{builder.registry_key}</code>
        </dd>
        {Object.entries(config)
          .filter(([, v]) => typeof v === "string" && v)
          .map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{k.replace(/_/g, " ")}</dt>
              <dd className="break-all">
                <code className="text-xs">{String(v)}</code>
              </dd>
            </div>
          ))}
      </dl>
      {probe && <p className="text-sm">{probe}</p>}

      {needsAck && (
        <div className="flex flex-col gap-2 border-t border-border pt-3 text-sm">
          <span className="text-muted-foreground">
            Organizations' RUN steps execute on this engine. Run the probe to see what a
            RUN step there can reach, then decide whether that is acceptable.
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className={btn}
              disabled={runIsolationProbe.isPending}
              onClick={() => runIsolationProbe.mutate()}
            >
              {runIsolationProbe.isPending ? "Probing… (builds a throwaway image)" : "Run isolation probe"}
            </button>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={builder.isolation_ack}
                disabled={!builder.isolation_ack && isolation === null}
                onChange={(e) => patch.mutate({ isolation_ack: e.target.checked })}
              />
              I have seen what a build here can reach, and organizations may build here
            </label>
          </div>
          {isolation && (
            <pre className="max-h-48 overflow-auto rounded bg-muted/40 p-2 font-mono text-xs">
              {isolation.join("\n")}
            </pre>
          )}
        </div>
      )}

      <div className="flex flex-col gap-2 border-t border-border pt-3 text-sm">
        <span className="text-muted-foreground">
          Which organizations may build here — one organization id per line; empty means
          every organization.
        </span>
        <textarea
          className={`${input} min-h-16 font-mono text-xs`}
          value={allowed}
          onChange={(e) => setAllowed(e.target.value)}
          placeholder="(every organization)"
        />
        <div>
          <button
            type="button"
            className={btn}
            onClick={() => {
              const ids = allowed
                .split("\n")
                .map((l) => l.trim())
                .filter(Boolean);
              patch.mutate({ allowed_tenants: ids.length ? ids : null });
            }}
          >
            Save who may use it
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3 text-sm">
        <span className="text-muted-foreground">
          Credential {builder.has_credential ? "(set — replace)" : "(required)"}:
        </span>
        {builder.kind === "webhook" && (
          <input
            className={input}
            type="password"
            placeholder="signing secret (16+ chars)"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            autoComplete="new-password"
          />
        )}
        {builder.kind === "portainer" ? (
          <>
            <input
              className={input}
              type="password"
              placeholder="Portainer API key (non-admin user)"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="new-password"
            />
            <input
              className={input}
              placeholder="registry push user"
              value={pushUser}
              onChange={(e) => setPushUser(e.target.value)}
              autoComplete="off"
            />
            <input
              className={input}
              type="password"
              placeholder="registry push password"
              value={pushPassword}
              onChange={(e) => setPushPassword(e.target.value)}
              autoComplete="new-password"
            />
          </>
        ) : (
          <input
            className={input}
            type="password"
            placeholder={
              builder.kind === "github_actions"
                ? "fine-grained token (Actions: read & write)"
                : "bearer token (optional)"
            }
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="new-password"
          />
        )}
        <button
          type="button"
          className={btnPrimary}
          disabled={
            (builder.kind === "webhook"
              ? secret.length < 16
              : builder.kind === "portainer"
                ? apiKey.length < 20 || !pushPassword
                : token.length < 20) || saveCredential.isPending
          }
          onClick={() => saveCredential.mutate()}
        >
          Save credential
        </button>
      </div>
    </div>
  );
}

function AddBuilderForm({
  registries,
  onAdded,
  onError,
}: {
  registries: string[];
  onAdded: () => void;
  onError: (message: string | null) => void;
}) {
  const [kind, setKind] = useState<"webhook" | "github_actions" | "portainer">("github_actions");
  const [baseUrl, setBaseUrl] = useState("");
  const [endpointId, setEndpointId] = useState("");
  const [pushHost, setPushHost] = useState("");
  const [networkMode, setNetworkMode] = useState("");
  const [tlsVerify, setTlsVerify] = useState(true);
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [registry, setRegistry] = useState(registries[0] ?? "");
  const [submitUrl, setSubmitUrl] = useState("");
  const [statusUrl, setStatusUrl] = useState("");
  const [cancelUrl, setCancelUrl] = useState("");
  const [insecure, setInsecure] = useState(false);
  const [owner, setOwner] = useState("");
  const [repo, setRepo] = useState("");
  const [workflow, setWorkflow] = useState("pyrrhula-image-build.yml");
  const [ref, setRef] = useState("main");
  const [apiBase, setApiBase] = useState("https://api.github.com");

  const ready =
    kind === "webhook"
      ? !!submitUrl && !!statusUrl
      : kind === "portainer"
        ? !!baseUrl && !!endpointId
        : !!owner && !!repo && !!workflow;

  const add = useMutation({
    mutationFn: async () => {
      const config =
        kind === "webhook"
          ? { submit_url: submitUrl, status_url: statusUrl, cancel_url: cancelUrl, insecure }
          : kind === "portainer"
            ? {
                base_url: baseUrl,
                endpoint_id: Number(endpointId),
                push_host: pushHost,
                network_mode: networkMode,
                tls_verify: tlsVerify,
              }
            : { owner, repo, workflow, ref, api_base: apiBase };
      const { error } = await apiClient.POST("/admin/image-builders", {
        body: { key, kind, label, registry_key: registry, config },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setKey("");
      setLabel("");
      onError(null);
      onAdded();
    },
    onError: (e) => onError(detailOf(e)),
  });

  return (
    <section className="flex flex-col gap-3 rounded-md border border-dashed border-border p-4">
      <h2 className="font-medium">Declare a builder</h2>
      <div className="flex gap-4 text-sm">
        {(["github_actions", "portainer", "webhook"] as const).map((k) => (
          <label key={k} className="flex items-center gap-2">
            <input type="radio" checked={kind === k} onChange={() => setKind(k)} />
            {k === "github_actions" ? "GitHub Actions" : k === "portainer" ? "Portainer" : "Webhook"}
          </label>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {kind === "github_actions"
          ? "Pyrrhula dispatches a workflow in a repository you control and follows the run. Start from docs/builders/github-actions.yml; the push credential stays in your CI."
          : kind === "portainer"
            ? "Builds run on a Docker environment you manage in Portainer, with the Dockerfile as the only build input. Use an API key of a non-admin user with access to that environment only. Organizations can use it once you have run the isolation probe and acknowledged the result."
            : "Your receiver gets a signed JSON request with the Dockerfile base64-encoded and the exact image reference to push; Pyrrhula polls its status URL. See docs/builders/webhook.md."}
      </p>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          Key
          <input className={input} value={key} onChange={(e) => setKey(e.target.value)} placeholder="ci" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Label
          <input className={input} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Build farm" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Pushes to registry
          <select className={input} value={registry} onChange={(e) => setRegistry(e.target.value)}>
            {registries.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        {kind === "portainer" ? (
          <>
            <label className="flex flex-col gap-1 text-sm">
              Portainer URL
              <input className={input} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://portainer.example:9443" />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Environment id
              <input className={input} value={endpointId} onChange={(e) => setEndpointId(e.target.value)} placeholder="3" />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Push host (optional — how the engine reaches the registry)
              <input className={input} value={pushHost} onChange={(e) => setPushHost(e.target.value)} placeholder="127.0.0.1:5002" />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Build network (optional)
              <input className={input} value={networkMode} onChange={(e) => setNetworkMode(e.target.value)} placeholder="default bridge" />
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={tlsVerify} onChange={(e) => setTlsVerify(e.target.checked)} />
              Verify Portainer's TLS certificate
            </label>
          </>
        ) : kind === "webhook" ? (
          <>
            <label className="flex flex-col gap-1 text-sm">
              Submit URL
              <input className={input} value={submitUrl} onChange={(e) => setSubmitUrl(e.target.value)} placeholder="https://builds.example/submit" />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Status URL
              <input className={input} value={statusUrl} onChange={(e) => setStatusUrl(e.target.value)} placeholder="https://builds.example/status" />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Cancel URL (optional)
              <input className={input} value={cancelUrl} onChange={(e) => setCancelUrl(e.target.value)} />
            </label>
          </>
        ) : (
          <>
            <label className="flex flex-col gap-1 text-sm">
              Owner
              <input className={input} value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="acme" />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Repository
              <input className={input} value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="pyrrhula-builds" />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Workflow file
              <input className={input} value={workflow} onChange={(e) => setWorkflow(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Branch
              <input className={input} value={ref} onChange={(e) => setRef(e.target.value)} />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              API base (GitHub Enterprise Server)
              <input className={input} value={apiBase} onChange={(e) => setApiBase(e.target.value)} />
            </label>
          </>
        )}
      </div>
      {kind === "webhook" && (
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={insecure} onChange={(e) => setInsecure(e.target.checked)} />
          Allow plain HTTP (a receiver on a trusted private network only)
        </label>
      )}
      <div>
        <button
          type="button"
          className={btnPrimary}
          disabled={!key || !registry || !ready || add.isPending}
          onClick={() => add.mutate()}
        >
          Declare builder
        </button>
      </div>
    </section>
  );
}

function LimitsCard({ onError }: { onError: (message: string | null) => void }) {
  const queryClient = useQueryClient();
  const limits = useQuery({
    queryKey: ["admin", "image-build-limits"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/image-build-limits");
      if (error) throw error;
      return data;
    },
  });
  const [draft, setDraft] = useState<components["schemas"]["BuildLimits"] | null>(null);
  const value = draft ?? limits.data;

  const save = useMutation({
    mutationFn: async (body: components["schemas"]["BuildLimits"]) => {
      const { error } = await apiClient.PUT("/admin/image-build-limits", { body });
      if (error) throw error;
    },
    onSuccess: () => {
      setDraft(null);
      onError(null);
      void queryClient.invalidateQueries({ queryKey: ["admin", "image-build-limits"] });
    },
    onError: (e) => onError(detailOf(e)),
  });

  if (!value) return null;
  const field = (name: keyof typeof value, label: string) => (
    <label className="flex flex-col gap-1 text-sm">
      {label}
      <input
        className={input}
        type="number"
        value={value[name]}
        onChange={(e) => setDraft({ ...value, [name]: Number(e.target.value) })}
      />
    </label>
  );
  return (
    <section className="flex flex-col gap-3 rounded-md border border-border p-4">
      <h2 className="font-medium">Build limits per organization</h2>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {field("max_concurrent_per_tenant", "Builds at once")}
        {field("max_per_day_per_tenant", "Builds per 24 hours")}
        {field("timeout_seconds", "Timeout (seconds)")}
      </div>
      <div>
        <button
          type="button"
          className={btnPrimary}
          disabled={draft === null || save.isPending}
          onClick={() => draft && save.mutate(draft)}
        >
          Save limits
        </button>
      </div>
    </section>
  );
}
