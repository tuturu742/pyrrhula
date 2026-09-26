import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * The images this organization can build in.
 *
 * The catalog used to be four entries compiled into the platform, so a Rust project, a Go
 * project or anything on an internal registry had no name here and each repository
 * carried its own fully-qualified image instead. Registering one puts the name in the
 * dropdown for every repository at once.
 *
 * Reusing a built-in's name overrides it, which is as much the point as adding new ones:
 * a deployment behind a private registry points `debian` at its own mirror once rather
 * than every repository repeating the address.
 */
export function RuntimesCard() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [key, setKey] = useState("");
  const [image, setImage] = useState("");
  const [setup, setSetup] = useState("");

  const runtimes = useQuery({
    queryKey: ["repo-runtimes"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos/runtimes");
      if (error) throw error;
      return data;
    },
  });

  const reset = () => {
    setKey("");
    setImage("");
    setSetup("");
    setAdding(false);
  };

  const save = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/repos/runtimes/{key}", {
        params: { path: { key: key.trim() } },
        body: {
          image: image.trim(),
          setup: setup
            .split("\n")
            .map((l) => l.trim())
            .filter(Boolean),
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success(`Runtime "${key.trim()}" registered`);
      reset();
      void queryClient.invalidateQueries({ queryKey: ["repo-runtimes"] });
    },
    onError: (e: unknown) => toast.error(describe(e)),
  });

  const remove = useMutation({
    mutationFn: async (k: string) => {
      const { error } = await apiClient.DELETE("/repos/runtimes/{key}", {
        params: { path: { key: k } },
      });
      if (error) throw error;
      return k;
    },
    onSuccess: (k) => {
      toast.success(`Runtime "${k}" removed`);
      void queryClient.invalidateQueries({ queryKey: ["repo-runtimes"] });
    },
    onError: (e: unknown) => toast.error(describe(e)),
  });

  // `custom` is the bring-your-own-image sentinel, not a registered runtime — listing it
  // here would invite someone to try to delete it.
  const entries = (runtimes.data ?? []).filter((r) => r.key !== "custom");
  const owned = entries.filter((r) => r.tenant_owned);

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-medium">Build runtimes</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            The images agents build and test in. Register your own to give them a name
            every repository can pick; reuse a built-in name to point it somewhere else.
          </p>
        </div>
        {!adding && (
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            Register image
          </Button>
        )}
      </div>

      {adding && (
        <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
          <div className="grid gap-2 sm:grid-cols-2">
            <label htmlFor="runtime-name" className="flex flex-col gap-1 text-xs">
              <span className="font-medium">Name</span>
              <Input
                id="runtime-name"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="rust"
                autoComplete="off"
              />
            </label>
            <label htmlFor="runtime-image" className="flex flex-col gap-1 text-xs">
              <span className="font-medium">Image</span>
              <Input
                id="runtime-image"
                className="font-mono text-xs"
                value={image}
                onChange={(e) => setImage(e.target.value)}
                placeholder="docker.io/library/rust:1.97-bookworm"
                autoComplete="off"
              />
            </label>
          </div>
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Setup commands (one per line, optional)</span>
            <textarea
              className="min-h-[60px] rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={setup}
              onChange={(e) => setSetup(e.target.value)}
              placeholder={"apt-get update && apt-get install -y --no-install-recommends git"}
            />
            <span className="text-muted-foreground">
              Run before every build in this runtime. The image needs git for agents to
              work in it.
            </span>
          </label>
          {entries.some((r) => r.key === key.trim() && !r.tenant_owned) && (
            <p className="text-xs text-amber-600 dark:text-amber-500">
              This overrides the built-in <span className="font-mono">{key.trim()}</span>.
              Every repository using that name will build in your image instead.
            </p>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              disabled={!key.trim() || !image.trim() || save.isPending}
              onClick={() => save.mutate()}
            >
              {save.isPending ? "Saving…" : "Save"}
            </Button>
            <Button size="sm" variant="ghost" onClick={reset}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {runtimes.isLoading ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {entries.map((r) => (
            <li key={r.key} className="flex items-center justify-between gap-3 py-2">
              <div className="min-w-0">
                <span className="text-sm font-medium">{r.key}</span>
                {r.tenant_owned && (
                  <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                    yours
                  </span>
                )}
                <div className="truncate font-mono text-xs text-muted-foreground">
                  {r.image}
                </div>
                {(r.setup ?? []).length > 0 && (
                  <div className="truncate text-[11px] text-muted-foreground">
                    {(r.setup ?? []).length} setup command
                    {(r.setup ?? []).length === 1 ? "" : "s"}
                  </div>
                )}
              </div>
              {r.tenant_owned && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(r.key)}
                >
                  Remove
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}

      {owned.length === 0 && !adding && (
        <p className="text-xs text-muted-foreground">
          Only the built-in runtimes so far. A repository can still name any image
          directly with “Custom image…”.
        </p>
      )}
    </div>
  );
}

function describe(e: unknown): string {
  const detail = (e as { detail?: unknown })?.detail;
  return typeof detail === "string" ? detail : "Could not save the runtime";
}
