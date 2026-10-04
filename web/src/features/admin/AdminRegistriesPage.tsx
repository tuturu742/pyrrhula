import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import type { components } from "@/lib/api-client/schema";

type Registry = components["schemas"]["RegistryOut"];

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
 * Platform-admin: the container registries this deployment declares, and the allowlist of
 * where runtime images may come from.
 *
 * A registry is infrastructure the operator runs or subscribes to — organizations never
 * declare one, they use what is declared here. Each one also reserves a namespace under
 * which images built for an organization live, and the platform refuses one
 * organization's reference to another's part of it.
 */
export function AdminRegistriesPage() {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const registries = useQuery({
    queryKey: ["admin", "image-registries"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/image-registries");
      if (error) throw error;
      return data;
    },
  });
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "image-registries"] });

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Container registries</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Where images built for organizations are pushed and verified, and where
          execution engines pull them from. A credential set here is read access — enough
          to confirm an image exists and to pull it — and is never shown again.
        </p>
      </header>

      {error && (
        <p className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}

      <section className="flex flex-col gap-3">
        {registries.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
        {registries.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">No registries declared yet.</p>
        )}
        {registries.data?.map((r) => (
          <RegistryCard key={r.key} registry={r} onChange={invalidate} onError={setError} />
        ))}
      </section>

      <AddRegistryForm onAdded={invalidate} onError={setError} />
      <AllowlistCard onError={setError} />
    </div>
  );
}

function RegistryCard({
  registry,
  onChange,
  onError,
}: {
  registry: Registry;
  onChange: () => void;
  onError: (message: string | null) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [probe, setProbe] = useState<string | null>(null);

  const setCredential = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/admin/image-registries/{key}/credential", {
        params: { path: { key: registry.key } },
        body: { username, password },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setPassword("");
      onError(null);
      onChange();
    },
    onError: (e) => onError(detailOf(e)),
  });

  const clearCredential = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.DELETE("/admin/image-registries/{key}/credential", {
        params: { path: { key: registry.key } },
      });
      if (error) throw error;
    },
    onSuccess: onChange,
    onError: (e) => onError(detailOf(e)),
  });

  const test = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/admin/image-registries/{key}/test", {
        params: { path: { key: registry.key } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (result) =>
      setProbe(`${result.reachable ? "✓" : "✗"} ${result.detail}`),
    onError: (e) => setProbe(`✗ ${detailOf(e)}`),
  });

  const toggle = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PATCH("/admin/image-registries/{key}", {
        params: { path: { key: registry.key } },
        body: { enabled: !registry.enabled },
      });
      if (error) throw error;
    },
    onSuccess: onChange,
    onError: (e) => onError(detailOf(e)),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.DELETE("/admin/image-registries/{key}", {
        params: { path: { key: registry.key } },
      });
      if (error) throw error;
    },
    onSuccess: onChange,
    onError: (e) => onError(detailOf(e)),
  });

  const namespace =
    registry.path_style === "flat"
      ? `${registry.pull_host}/${registry.path_prefix}/pyr-t<org>-<name>`
      : `${registry.pull_host}/${registry.path_prefix}/t<org>/<name>`;

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="font-medium">{registry.label || registry.key}</span>{" "}
          <code className="text-xs text-muted-foreground">{registry.key}</code>
          {!registry.enabled && (
            <span className="ml-2 rounded-full bg-secondary px-2 py-0.5 text-xs">disabled</span>
          )}
          {registry.insecure && (
            <span className="ml-2 rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-400">
              plain HTTP
            </span>
          )}
        </div>
        <div className="flex gap-2">
          <button type="button" className={btn} onClick={() => test.mutate()} disabled={test.isPending}>
            {test.isPending ? "Testing…" : "Test"}
          </button>
          <button type="button" className={btn} onClick={() => toggle.mutate()}>
            {registry.enabled ? "Disable" : "Enable"}
          </button>
          <button
            type="button"
            className={btn}
            onClick={() => {
              if (confirm(`Remove registry "${registry.key}"? Disable it instead to keep its namespace reserved.`))
                remove.mutate();
            }}
          >
            Remove
          </button>
        </div>
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Host</dt>
        <dd>
          <code>{registry.pull_host}</code>
          {registry.aliases.length > 0 && (
            <span className="text-muted-foreground"> (also {registry.aliases.join(", ")})</span>
          )}
        </dd>
        <dt className="text-muted-foreground">Organization images</dt>
        <dd>
          <code className="text-xs">{namespace}</code>
        </dd>
        <dt className="text-muted-foreground">Deleting old images</dt>
        <dd>{registry.supports_delete ? "supported" : "not supported — images accumulate"}</dd>
        {registry.k8s_pull_secret && (
          <>
            <dt className="text-muted-foreground">Kubernetes pull secret</dt>
            <dd>
              <code>{registry.k8s_pull_secret}</code>
            </dd>
          </>
        )}
      </dl>
      {probe && <p className="text-sm">{probe}</p>}
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3 text-sm">
        <span className="text-muted-foreground">Read credential:</span>
        {registry.has_credential ? (
          <>
            <span>set for <code>{registry.credential_username || "(no username)"}</code></span>
            <button type="button" className={btn} onClick={() => clearCredential.mutate()}>
              Clear
            </button>
          </>
        ) : (
          <span className="text-muted-foreground">none (anonymous)</span>
        )}
        <input
          className={input}
          placeholder="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="off"
        />
        <input
          className={input}
          type="password"
          placeholder={registry.has_credential ? "replace token" : "token or password"}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
        />
        <button
          type="button"
          className={btnPrimary}
          disabled={!password || setCredential.isPending}
          onClick={() => setCredential.mutate()}
        >
          Save credential
        </button>
      </div>
    </div>
  );
}

function AddRegistryForm({
  onAdded,
  onError,
}: {
  onAdded: () => void;
  onError: (message: string | null) => void;
}) {
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [host, setHost] = useState("");
  const [prefix, setPrefix] = useState("pyrrhula");
  const [flat, setFlat] = useState(false);
  const [insecure, setInsecure] = useState(false);
  const [supportsDelete, setSupportsDelete] = useState(false);
  const [publicAck, setPublicAck] = useState(false);
  const [pullSecret, setPullSecret] = useState("");
  const isHub = ["docker.io", "index.docker.io"].includes(host.trim().toLowerCase());

  const add = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/admin/image-registries", {
        body: {
          key,
          label,
          pull_host: host,
          path_prefix: prefix,
          path_style: flat || isHub ? "flat" : "nested",
          insecure,
          supports_delete: supportsDelete,
          public_by_default_ack: publicAck,
          k8s_pull_secret: pullSecret,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setKey("");
      setLabel("");
      setHost("");
      onError(null);
      onAdded();
    },
    onError: (e) => onError(detailOf(e)),
  });

  return (
    <section className="flex flex-col gap-3 rounded-md border border-dashed border-border p-4">
      <h2 className="font-medium">Declare a registry</h2>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          Key
          <input className={input} value={key} onChange={(e) => setKey(e.target.value)} placeholder="local" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Label
          <input className={input} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Build registry" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Host
          <input className={input} value={host} onChange={(e) => setHost(e.target.value)} placeholder="registry.example.com or localhost:5000" />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          {isHub ? "Docker Hub organization" : "Path prefix"}
          <input className={input} value={prefix} onChange={(e) => setPrefix(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Kubernetes pull secret (optional)
          <input className={input} value={pullSecret} onChange={(e) => setPullSecret(e.target.value)} placeholder="regcred" />
        </label>
      </div>
      <div className="flex flex-col gap-1 text-sm">
        {!isHub && (
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={flat} onChange={(e) => setFlat(e.target.checked)} />
            Flat paths (the registry allows no nested repository names)
          </label>
        )}
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={insecure} onChange={(e) => setInsecure(e.target.checked)} />
          Plain HTTP — the engines must also be configured to trust it
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={supportsDelete} onChange={(e) => setSupportsDelete(e.target.checked)} />
          Supports deleting images (registry:2 with deletion enabled)
        </label>
        {isHub && (
          <label className="flex items-center gap-2 text-amber-700 dark:text-amber-400">
            <input type="checkbox" checked={publicAck} onChange={(e) => setPublicAck(e.target.checked)} />
            I understand Docker Hub creates new repositories as public by default
          </label>
        )}
      </div>
      <div>
        <button type="button" className={btnPrimary} disabled={!key || !host || add.isPending} onClick={() => add.mutate()}>
          Declare registry
        </button>
      </div>
    </section>
  );
}

function AllowlistCard({ onError }: { onError: (message: string | null) => void }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);

  const allowlist = useQuery({
    queryKey: ["admin", "runtime-image-allowlist"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/runtime-image-allowlist");
      if (error) throw error;
      return data;
    },
  });

  const save = useMutation({
    mutationFn: async (prefixes: string[]) => {
      const { error } = await apiClient.PUT("/admin/runtime-image-allowlist", {
        body: { prefixes },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setDraft(null);
      onError(null);
      queryClient.invalidateQueries({ queryKey: ["admin", "runtime-image-allowlist"] });
    },
    onError: (e) => onError(detailOf(e)),
  });

  const current = allowlist.data?.prefixes ?? [];
  const text = draft ?? current.join("\n");

  return (
    <section className="flex flex-col gap-3 rounded-md border border-border p-4">
      <h2 className="font-medium">Where runtime images may come from</h2>
      <p className="text-sm text-muted-foreground">
        One prefix per line, each starting with a registry host — for example{" "}
        <code>docker.io/library/</code> or <code>ghcr.io/acme/</code>. Empty means
        unrestricted. Applies everywhere an image enters: repository settings, the
        repository's own build file, runtimes, harnesses, previews and imported bundles.
        Images built for an organization in a registry declared above are always allowed.
      </p>
      <textarea
        className={`${input} min-h-24 font-mono`}
        value={text}
        onChange={(e) => setDraft(e.target.value)}
        placeholder="(unrestricted)"
      />
      <div>
        <button
          type="button"
          className={btnPrimary}
          disabled={draft === null || save.isPending}
          onClick={() =>
            save.mutate(
              text
                .split("\n")
                .map((line) => line.trim())
                .filter(Boolean),
            )
          }
        >
          Save allowlist
        </button>
      </div>
    </section>
  );
}
