import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

const input =
  "rounded-md border border-input bg-transparent px-3 py-1.5 text-sm";
const btn =
  "rounded-md border border-input px-3 py-1.5 text-sm hover:bg-secondary/50 disabled:opacity-50";
const btnPrimary =
  "rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50";

/** Platform-admin: workflow plugin repositories. The declared-servers block is the
 * operator's review surface — adding a repository is also approving its endpoints. */
export function AdminPluginReposPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [uploadName, setUploadName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const repos = useQuery({
    queryKey: ["admin", "plugin-repositories"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/plugin-repositories");
      if (error) throw error;
      return data;
    },
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["admin", "plugin-repositories"] });

  const add = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/admin/plugin-repositories", {
        body: { name, url, ref },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setName("");
      setUrl("");
      setRef("");
      setError(null);
      invalidate();
    },
    onError: (e) => setError(String((e as { detail?: string })?.detail ?? e)),
  });

  const sync = useMutation({
    mutationFn: async (repositoryId: string) => {
      const { error } = await apiClient.POST(
        "/admin/plugin-repositories/{repository_id}/sync",
        { params: { path: { repository_id: repositoryId } } },
      );
      if (error) throw error;
    },
    onSuccess: invalidate,
    onError: (e) => setError(String((e as { detail?: string })?.detail ?? e)),
  });

  // The no-git path: an archive whose root holds plugin.json (a single wrapping
  // directory, as GitHub's "Download ZIP" produces, is unwrapped server-side). Same
  // multipart call the Models page makes for a cache archive; the generated client
  // does not type multipart bodies.
  async function upload(file: File) {
    setUploading(true);
    try {
      const body = new FormData();
      body.append("name", uploadName.trim() || file.name.replace(/\.(zip|tar\.gz|tgz)$/i, ""));
      body.append("file", file);
      const resp = await fetch("/api/admin/plugin-repositories/upload", {
        method: "POST",
        body,
        credentials: "include",
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail ?? `upload failed (${resp.status})`);
      }
      setUploadName("");
      setError(null);
      invalidate();
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setUploading(false);
    }
  }

  const remove = useMutation({
    mutationFn: async (repositoryId: string) => {
      const { error } = await apiClient.DELETE(
        "/admin/plugin-repositories/{repository_id}",
        { params: { path: { repository_id: repositoryId } } },
      );
      if (error) throw error;
    },
    onSuccess: invalidate,
    onError: (e) => setError(String((e as { detail?: string })?.detail ?? e)),
  });

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-xl font-semibold tracking-tight">Plugin repositories</h1>
      {error && (
        <p className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}

      <form
        className="flex flex-wrap items-end gap-3 rounded-lg border border-border p-4"
        onSubmit={(e) => {
          e.preventDefault();
          add.mutate();
        }}
      >
        <label className="flex flex-col gap-1 text-sm">
          Name
          <input className={input} value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Git URL
          <input
            className={`${input} w-80`}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/you/your-workflows"
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Ref (pinned commit)
          <input className={input} value={ref} onChange={(e) => setRef(e.target.value)} required />
        </label>
        <button type="submit" className={btnPrimary} disabled={add.isPending}>
          {add.isPending ? "Syncing…" : "Add repository"}
        </button>
      </form>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-border p-4">
        <label className="flex flex-col gap-1 text-sm">
          Upload a pack (.zip or .tar.gz with plugin.json at its root)
          <input
            className={input}
            value={uploadName}
            onChange={(e) => setUploadName(e.target.value)}
            placeholder="name (defaults to the file name)"
          />
        </label>
        <label className={`${btn} cursor-pointer ${uploading ? "opacity-50" : ""}`}>
          {uploading ? "Uploading…" : "Choose archive"}
          <input
            type="file"
            accept=".zip,.tar.gz,.tgz"
            className="hidden"
            disabled={uploading}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void upload(file);
              e.target.value = "";
            }}
          />
        </label>
        <span className="text-xs text-muted-foreground">
          For a deployment that cannot reach a git host. No credentials, no restart.
        </span>
      </div>

      <div className="flex flex-col gap-4">
        {repos.isLoading && <p className="text-muted-foreground">Loading…</p>}
        {repos.isSuccess && (repos.data ?? []).length === 0 && (
          <p className="text-sm text-muted-foreground">
            No plugin repositories yet — add one above to make its workflows installable.
          </p>
        )}
        {(repos.data ?? []).map((r) => (
          <div key={r.id} className="rounded-lg border border-border p-4">
            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="font-medium">
                  {r.name}{" "}
                  <span className="text-xs text-muted-foreground">({r.source})</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {r.url} @ {r.ref.slice(0, 12)}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span
                  className={`text-sm ${r.status === "synced" ? "text-muted-foreground" : "text-destructive"}`}
                >
                  {r.status}
                </span>
                <button
                  type="button"
                  className={btn}
                  onClick={() => sync.mutate(r.id)}
                  disabled={sync.isPending}
                >
                  Re-sync
                </button>
                {(r.source === "git" || r.source === "upload") && (
                  <button
                    type="button"
                    className={btn}
                    onClick={() => remove.mutate(r.id)}
                    disabled={remove.isPending}
                  >
                    Remove
                  </button>
                )}
              </div>
            </div>
            {r.last_error && (
              <p className="mt-2 text-sm text-destructive">{r.last_error}</p>
            )}
            <div className="mt-2 text-sm text-muted-foreground">
              Workflows: {r.workflow_keys.join(", ") || "—"}
            </div>
            {/* Only a real third party is an approval decision. The platform's own
                pyrrhula:// tooling is listed plainly below it -- showing both under
                "adding this repo approves these endpoints" made a clean install look
                like it had attached external MCP servers when it had not. */}
            {r.declared_servers.some((s) => s.external) && (
              <div className="mt-3 rounded-md bg-secondary/30 p-3 text-sm">
                <div className="mb-1 font-medium">
                  External MCP servers (adding this repo approves these endpoints)
                </div>
                <ul className="flex flex-col gap-1">
                  {r.declared_servers
                    .filter((s) => s.external)
                    .map((s, i) => (
                      <li key={i} className="text-muted-foreground">
                        <span className="text-foreground">{s.workflow_key}</span> · {s.key} →{" "}
                        {s.url} · tools: {s.enabled_tools.join(", ") || "none"}
                      </li>
                    ))}
                </ul>
              </div>
            )}
            {r.declared_servers.some((s) => !s.external) && (
              <div className="mt-2 text-sm text-muted-foreground">
                Built-in tools:{" "}
                {r.declared_servers
                  .filter((s) => !s.external)
                  .map((s) => `${s.key} (${s.enabled_tools.join(", ") || "none"})`)
                  .join(" · ")}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
