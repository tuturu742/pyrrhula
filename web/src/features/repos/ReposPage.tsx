import { useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmButton } from "@/components/ConfirmButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import { usePreviewActions, usePreviews } from "@/features/previews/usePreviews";

/**
 * Tenant repo registry: repos live in the hosted server-side git store; registering one may
 * import from a source URL (a token for private remotes is write-only — sealed server-side,
 * never shown again) and carries the exec-environment config (runtime / setup / test command)
 * delegated coding agents build and run tests in.
 */

interface RepoRowData {
  id: string;
  key: string;
  name: string;
  description?: string;
  source_url?: string | null;
  provider?: string | null;
  runtime_image?: string | null;
  has_registry_credential?: boolean;
  has_credential?: boolean;
  runtime?: string;
  setup_cmds?: string[];
  test_cmd?: string | null;
  artifact_name?: string | null;
}

/**
 * Preview deployments: a built artifact actually running in a container, behind a link
 * that needs no login.
 *
 * The share URL is never stored — it is a signed token minted on demand — so "Copy link"
 * always hands over a fresh one and pushes the preview's deadline out. That is the
 * answer to a link that stopped working: ask for another, rather than redeploy and lose
 * the running container.
 */
function PreviewsCard({ repos }: { repos: RepoRowData[] }) {
  const [notice, setNotice] = useState<string | null>(null);
  const { data: previews } = usePreviews();
  const { deploy, share, stop } = usePreviewActions(setNotice);

  const deployable = repos.filter((r) => r.artifact_name);
  if (deployable.length === 0 && (previews?.length ?? 0) === 0) return null;

  const byRepo = new Map((previews ?? []).map((p) => [p.repo_id ?? "", p]));

  return (
    <section className="rounded-md border border-border p-4">
      <h2 className="font-medium">Preview deployments</h2>
      <p className="mt-1 text-xs text-muted-foreground">
        Runs a repo&apos;s latest build in its own container and gives you a link anyone
        can open — no Pyrrhula account needed — so a person can try what the agents built.
        Previews expire on their own.
      </p>
      {notice && (
        <p className="mt-2 break-all rounded bg-secondary px-2 py-1 text-xs">{notice}</p>
      )}
      <div className="mt-3 flex flex-col gap-2">
        {deployable.map((repo) => {
          const preview = byRepo.get(repo.id);
          const running = preview?.status === "running";
          return (
            <div
              key={repo.id}
              className="flex flex-wrap items-center gap-2 rounded border border-border px-3 py-2 text-sm"
            >
              <span className="font-medium">{repo.name}</span>
              {preview && (
                <span
                  className={
                    running
                      ? "rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs text-emerald-600 dark:text-emerald-400"
                      : "rounded bg-secondary px-1.5 py-0.5 text-xs text-muted-foreground"
                  }
                  title={preview.last_error || undefined}
                >
                  {preview.status}
                </span>
              )}
              {preview?.expires_at && running && (
                <span className="text-xs text-muted-foreground">
                  until {new Date(preview.expires_at).toLocaleTimeString()}
                </span>
              )}
              <span className="grow" />
              <button
                type="button"
                onClick={() => deploy.mutate({ repoId: repo.id })}
                disabled={deploy.isPending}
                className="rounded-md border border-border px-2 py-1 text-xs"
              >
                {preview ? "Redeploy" : "Deploy"}
              </button>
              {running && preview && (
                <>
                  <button
                    type="button"
                    onClick={() => share.mutate(preview.id)}
                    disabled={share.isPending}
                    className="rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground"
                  >
                    Copy link
                  </button>
                  <ConfirmButton
                    title="Stop this preview?"
                    description="The container is torn down and the shared link stops working. You can deploy it again from the latest build."
                    confirmLabel="Stop"
                    destructive
                    onConfirm={() => stop.mutate(preview.id)}
                  >
                    <button
                      type="button"
                      className="rounded-md border border-border px-2 py-1 text-xs"
                    >
                      Stop
                    </button>
                  </ConfirmButton>
                </>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}


function ExecEngineCard() {
  const queryClient = useQueryClient();
  const { data: engines } = useQuery({
    queryKey: ["exec-engines"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos/exec-engines");
      if (error) throw error;
      return data;
    },
  });
  const setEngine = useMutation({
    mutationFn: async (engine: string) => {
      const { error } = await apiClient.PUT("/repos/exec-engines/current", {
        body: { engine },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["exec-engines"] }),
  });
  if (!engines || engines.length < 2) return null; // one engine = nothing to choose

  return (
    <section className="flex items-center justify-between rounded-md border border-border p-4">
      <div>
        <h2 className="font-medium">Agent environments run on</h2>
        <p className="text-xs text-muted-foreground">
          Where delegated coding agents build and test — engines offered by this deployment.
        </p>
      </div>
      <select
        className="rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        value={engines.find((e) => e.current)?.key ?? engines[0].key}
        onChange={(e) => setEngine.mutate(e.target.value)}
        disabled={setEngine.isPending}
      >
        {engines.map((e) => (
          <option key={e.key} value={e.key}>
            {e.label} ({e.kind})
          </option>
        ))}
      </select>
    </section>
  );
}

const ENV_STATUS_STYLE: Record<string, string> = {
  running: "bg-green-500/15 text-green-600 dark:text-green-400",
  idle: "bg-yellow-500/15 text-yellow-700 dark:text-yellow-400",
  kill_requested: "bg-orange-500/15 text-orange-600 dark:text-orange-400",
  completed: "bg-muted text-muted-foreground",
  failed: "bg-destructive/15 text-destructive",
  killed: "bg-muted text-muted-foreground",
  removed: "bg-muted text-muted-foreground",
};

function envAge(iso: string): string {
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function ExecEnvironmentsCard() {
  const queryClient = useQueryClient();
  const [showFinished, setShowFinished] = useState(false);
  const { data: envs } = useQuery({
    queryKey: ["exec-environments", showFinished],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos/exec-environments", {
        params: { query: { include_finished: showFinished } },
      });
      if (error) throw error;
      return data;
    },
    // Environments change while delegations run; keep the view live.
    refetchInterval: 8000,
  });
  const kill = useMutation({
    mutationFn: async (environmentId: string) => {
      const { error } = await apiClient.POST(
        "/repos/exec-environments/{environment_id}/kill",
        { params: { path: { environment_id: environmentId } } },
      );
      if (error) throw error;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["exec-environments"] }),
  });

  const active = envs?.filter((e) =>
    ["running", "idle", "kill_requested"].includes(e.status),
  );
  if (!envs || (envs.length === 0 && !showFinished)) return null;

  return (
    <section className="rounded-md border border-border p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="font-medium">
          Active environments{" "}
          <span className="text-sm font-normal text-muted-foreground">
            ({active?.length ?? 0} active)
          </span>
        </h2>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showFinished}
            onChange={(e) => setShowFinished(e.target.checked)}
          />
          show finished
        </label>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        Containers/tasks where delegated coding agents build and test, and who spawned
        them. Kill removes a stuck or orphaned one through its engine.
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="py-1 pr-3 font-normal">Environment</th>
              <th className="py-1 pr-3 font-normal">Spawned by</th>
              <th className="py-1 pr-3 font-normal">Session</th>
              <th className="py-1 pr-3 font-normal">Image</th>
              <th className="py-1 pr-3 font-normal">Status</th>
              <th className="py-1 pr-3 font-normal">Last activity</th>
              <th className="py-1 font-normal" />
            </tr>
          </thead>
          <tbody>
            {envs.map((e) => (
              <tr key={e.id} className="border-b border-border/50">
                <td className="py-1.5 pr-3 font-mono text-xs">{e.name}</td>
                <td className="py-1.5 pr-3">{e.spawned_by_label || "—"}</td>
                <td className="py-1.5 pr-3 text-muted-foreground">
                  {e.session_name || (e.session_id ? e.session_id.slice(0, 8) : "—")}
                </td>
                <td className="py-1.5 pr-3 font-mono text-xs text-muted-foreground">
                  {e.image}
                </td>
                <td className="py-1.5 pr-3">
                  <span
                    className={`rounded px-1.5 py-0.5 text-xs ${ENV_STATUS_STYLE[e.status] ?? "bg-muted"}`}
                  >
                    {e.status}
                    {e.last_exit_code != null && e.status !== "running"
                      ? ` (exit ${e.last_exit_code})`
                      : ""}
                  </span>
                </td>
                <td className="py-1.5 pr-3 text-xs text-muted-foreground">
                  {envAge(e.updated_at)} ago
                </td>
                <td className="py-1.5 text-right">
                  {["running", "idle"].includes(e.status) && (
                    <button
                      type="button"
                      disabled={kill.isPending}
                      onClick={() => kill.mutate(e.id)}
                      className="rounded-md border border-destructive/50 px-2 py-0.5 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-50"
                    >
                      Kill
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function ReposPage() {
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<RepoRowData | null>(null);

  const { data: repos, isLoading: pageLoading, isError: pageError } = useQuery({
    queryKey: ["repos"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos");
      if (error) throw error;
      return data;
    },
  });

  const archive = useMutation({
    mutationFn: async (repoId: string) => {
      const { error } = await apiClient.DELETE("/repos/{repo_id}", {
        params: { path: { repo_id: repoId } },
      });
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["repos"] }),
  });

  if (pageLoading) {
    return (
      <div className="flex flex-col gap-3 py-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }
  if (pageError) {
    return <p className="py-8 text-sm text-destructive">This page could not load — please refresh or try again.</p>;
  }

  return (
    <div className="flex flex-col gap-6">
      <ExecEngineCard />
      <PreviewsCard repos={repos ?? []} />
      <ExecEnvironmentsCard />
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Repos</h1>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
        >
          {showForm ? "Close" : "Register repo"}
        </button>
      </div>

      {(showForm || editing) && (
        <RepoForm
          existing={editing}
          onDone={() => {
            setShowForm(false);
            setEditing(null);
            queryClient.invalidateQueries({ queryKey: ["repos"] });
          }}
        />
      )}

      <section className="flex flex-col gap-2">
        {repos?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No repos yet. Register one, then select it when starting a session.
          </p>
        )}
        {repos?.map((r) => (
          <div
            key={r.id}
            className="flex items-center justify-between rounded-md border border-border px-4 py-3"
          >
            <div className="flex flex-col">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{r.name}</span>
                <span className="font-mono text-xs text-muted-foreground">{r.key}</span>
                <span className="rounded bg-secondary px-1.5 py-0.5 text-xs">{r.runtime}</span>
                {r.source_url?.startsWith("http") && (
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-xs" title={r.source_url}>
                    remote
                  </span>
                )}
                {r.has_credential && (
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-xs">token set</span>
                )}
              </div>
              {(r.description || r.test_cmd) && (
                <span className="text-xs text-muted-foreground">
                  {r.description}
                  {r.description && r.test_cmd ? " · " : ""}
                  {r.test_cmd && <code>{r.test_cmd}</code>}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
            {r.artifact_name && (
              <a
                href={`/api/repos/${r.id}/artifacts/play`}
                target="_blank"
                rel="noreferrer"
                className="rounded-md border border-border px-2.5 py-1 text-xs"
                title="Open the latest green build in the browser"
              >
                ▶ Play
              </a>
            )}
            <button
              type="button"
              onClick={() => {
                setShowForm(false);
                setEditing(r);
              }}
              className="rounded-md border border-border px-2.5 py-1 text-xs"
            >
              Edit
            </button>
            <ConfirmButton
                title="Archive"
                description={`Archive repo "${r.name}"? Hosted content is kept.`}
                confirmLabel="Archive"
                destructive
                onConfirm={() => archive.mutate(r.id)}
              >
                <button
              type="button"
              disabled={archive.isPending}
              className="rounded-md border border-destructive/50 px-2.5 py-1 text-xs text-destructive disabled:opacity-50"
            >
              Archive
            </button>
              </ConfirmButton>
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}

function RepoForm({
  existing,
  onDone,
}: {
  existing: RepoRowData | null;
  onDone: () => void;
}) {
  const [name, setName] = useState(existing?.name ?? "");
  const [key, setKey] = useState(existing?.key ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [sourceUrl, setSourceUrl] = useState(existing?.source_url ?? "");
  const [token, setToken] = useState("");
  const [provider, setProvider] = useState(existing?.provider ?? "auto");
  const [runtime, setRuntime] = useState(existing?.runtime ?? "debian");
  const [runtimeImage, setRuntimeImage] = useState(existing?.runtime_image ?? "");
  const [registryUser, setRegistryUser] = useState("");
  const [registryToken, setRegistryToken] = useState("");
  const [setupCmds, setSetupCmds] = useState((existing?.setup_cmds ?? []).join("\n"));
  const [testCmd, setTestCmd] = useState(existing?.test_cmd ?? "");
  const [importStatus, setImportStatus] = useState<string | null>(null);

  const { data: runtimes } = useQuery({
    queryKey: ["repo-runtimes"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos/runtimes");
      if (error) throw error;
      return data;
    },
  });

  const create = useMutation({
    mutationFn: async () => {
      const shared = {
        name,
        description,
        source_url: sourceUrl.trim() === "" ? null : sourceUrl.trim(),
        access_token: token.trim() === "" ? null : token,
        provider: provider === "auto" && !existing ? null : provider,
        runtime,
        runtime_image: runtime === "custom" ? runtimeImage.trim() : null,
        registry_username: registryUser.trim() === "" ? null : registryUser.trim(),
        registry_token: registryToken.trim() === "" ? null : registryToken,
        setup_cmds: setupCmds
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        test_cmd: testCmd.trim() === "" ? null : testCmd.trim(),
      };
      if (existing) {
        const { data, error } = await apiClient.PATCH("/repos/{repo_id}", {
          params: { path: { repo_id: existing.id } },
          body: { ...shared, clear_build: false,
          clear_test_cmd: testCmd.trim() === "" },
        });
        if (error) throw error;
        return data;
      }
      const { data, error } = await apiClient.POST("/repos", { body: { key, ...shared } });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      const status =
        data && typeof data === "object" && "import_status" in data
          ? (data as { import_status?: string | null }).import_status
          : null;
      if (status && status !== "imported") {
        setImportStatus(status);
      } else {
        onDone();
      }
    },
  });

  return (
    <section className="flex flex-col gap-4 rounded-md border border-border p-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Name</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (!key)
                setKey(
                  e.target.value
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, "-")
                    .replace(/^-+|-+$/g, ""),
                );
            }}
            placeholder="My Project"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Key</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={key}
            onChange={(e) => setKey(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "-"))}
            placeholder="my-project"
            disabled={!!existing}
          />
        </label>
      </div>

      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Description</span>
        <input
          className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Import from (optional)</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={sourceUrl}
            onChange={(e) => setSourceUrl(e.target.value)}
            placeholder="https://github.com/org/repo.git"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Access token (private repos)</span>
          <input
            type="password"
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={existing?.has_credential ? "leave empty to keep the stored token" : "stored encrypted; never shown again"}
            autoComplete="new-password"
          />
        </label>
      </div>

      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Git provider</span>
        <select
          className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          <option value="auto">Auto-detect (github.com / gitlab.com / codeberg.org)</option>
          <option value="github">GitHub</option>
          <option value="gitlab">GitLab (incl. self-hosted)</option>
          <option value="gitea">Gitea / Forgejo</option>
          <option value="generic">Generic (push only, no PRs)</option>
        </select>
        <span className="text-xs text-muted-foreground">
          Self-hosted GitLab/Gitea can't be auto-detected — pick the provider so PRs and
          review comments land on your server.
        </span>
      </label>

      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Runtime</span>
          <select
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={runtime}
            onChange={(e) => setRuntime(e.target.value)}
          >
            {(runtimes ?? [{ key: "debian", image: "" }]).map((r) => (
              <option key={r.key} value={r.key}>
                {r.key === "custom" ? "Custom image…" : `${r.key} (${r.image})`}
              </option>
            ))}
          </select>
          <span className="text-xs text-muted-foreground">
            The environment agents build and run tests in.
          </span>
          {runtime === "custom" && (
            <>
              <input
                className="mt-1 rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                value={runtimeImage}
                onChange={(e) => setRuntimeImage(e.target.value)}
                placeholder="ghcr.io/acme/build-env:1.4  (image must contain git)"
              />
              <div className="mt-1 grid grid-cols-2 gap-2">
                <input
                  className="rounded-md border border-input bg-transparent px-3 py-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={registryUser}
                  onChange={(e) => setRegistryUser(e.target.value)}
                  placeholder="registry username (private images)"
                  autoComplete="off"
                />
                <input
                  type="password"
                  className="rounded-md border border-input bg-transparent px-3 py-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                  value={registryToken}
                  onChange={(e) => setRegistryToken(e.target.value)}
                  placeholder={existing?.has_registry_credential ? "keep stored credentials" : "registry token/password"}
                  autoComplete="new-password"
                />
              </div>
            </>
          )}
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Test command (optional)</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={testCmd}
            onChange={(e) => setTestCmd(e.target.value)}
            placeholder="npm test"
          />
          <span className="text-xs text-muted-foreground">
            Run after each delegated change; its pass/fail becomes the PR's CI status.
          </span>
        </label>
      </div>

      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Setup commands (optional, one per line)</span>
        <textarea
          className="min-h-16 rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={setupCmds}
          onChange={(e) => setSetupCmds(e.target.value)}
          placeholder="corepack enable"
        />
      </label>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={!name.trim() || key.trim().length < 2 || create.isPending}
          onClick={() => create.mutate()}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {create.isPending ? "Saving…" : existing ? "Save" : "Register"}
        </button>
        {create.isError && (
          <span className="text-xs text-destructive">
            {(create.error as Error)?.message ?? "could not register"}
          </span>
        )}
        {importStatus && (
          <span className="text-xs text-amber-500">
            Registered, but: {importStatus}{" "}
            <button type="button" className="underline" onClick={onDone}>
              dismiss
            </button>
          </span>
        )}
      </div>
    </section>
  );
}
