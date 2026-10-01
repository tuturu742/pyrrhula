import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import type { components } from "@/lib/api-client/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DockerfileEditor, STARTER, type EditorState } from "./ImageEditor";

type Image = components["schemas"]["ImageOut"];
type Build = components["schemas"]["BuildOut"];

const ACTIVE = new Set(["queued", "submitted", "building", "verifying", "smoke_testing"]);

const STATUS_LABEL: Record<string, string> = {
  queued: "queued",
  submitted: "submitted",
  building: "building",
  verifying: "checking the registry",
  smoke_testing: "smoke test",
  ready: "ready",
  failed: "failed",
  cancelled: "cancelled",
  superseded: "superseded",
  deleted: "deleted",
};

function describe(e: unknown, fallback: string): string {
  const detail = (e as { detail?: unknown })?.detail;
  return typeof detail === "string" ? detail : fallback;
}

/**
 * The organization's own images: each is checked before any repository can run in it.
 *
 * An image arrives as an exact, digest-pinned reference -- typed here, or carried by a
 * `.pyr` bundle. The platform then asks the registry for the digest itself, runs a smoke
 * test on this organization's execution engine (git present; a claimed harness actually
 * there), and only then offers the name in the runtime dropdown, pinned to that digest.
 */
export function ImagesCard() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [image, setImage] = useState("");
  const [harness, setHarness] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [editing, setEditing] = useState<EditorState | null>(null);

  const images = useQuery({
    queryKey: ["images"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/images");
      if (error) throw error;
      return data;
    },
    // Follow a check while one runs; stop asking once nothing is in flight.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((i) => i.latest && ACTIVE.has(i.latest.status))
        ? 4000
        : false,
  });
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["images"] });
    void queryClient.invalidateQueries({ queryKey: ["repo-runtimes"] });
  };

  const importImage = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/images/import", {
        body: {
          name: name.trim(),
          image: image.trim(),
          dockerfile: "",
          harness_claim: harness.trim() ? { key: harness.trim(), version: "" } : null,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success(`Checking "${name.trim()}"`);
      setAdding(false);
      setName("");
      setImage("");
      setHarness("");
      invalidate();
    },
    onError: (e) => toast.error(describe(e, "Could not add the image")),
  });

  const act = useMutation({
    mutationFn: async ({ kind, imageName }: { kind: "recheck" | "cancel" | "remove"; imageName: string }) => {
      const path = { params: { path: { name: imageName } } };
      const { error } =
        kind === "recheck"
          ? await apiClient.POST("/images/{name}/recheck", path)
          : kind === "cancel"
            ? await apiClient.POST("/images/{name}/cancel", path)
            : await apiClient.DELETE("/images/{name}", path);
      if (error) throw error;
    },
    onSuccess: invalidate,
    onError: (e) => toast.error(describe(e, "That did not work")),
  });

  const list = images.data ?? [];

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-medium">Images</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Exact images this organization runs delegations in. Each one is checked —
            digest from the registry, then a smoke test on your execution engine — before
            it appears as a build runtime, pinned so it can never change underneath you.
          </p>
        </div>
        {!adding && !editing && (
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              setEditing({ name: "", dockerfile: STARTER, harness: "", isNew: true })
            }
          >
            Write a Dockerfile
          </Button>
        )}
        {!adding && !editing && (
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            Add a published image
          </Button>
        )}
      </div>

      {adding && (
        <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
          <div className="grid gap-2 sm:grid-cols-2">
            <label htmlFor="image-name" className="flex flex-col gap-1 text-xs">
              <span className="font-medium">Name (becomes the runtime name)</span>
              <Input
                id="image-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="godot-node"
              />
            </label>
            <label htmlFor="image-harness" className="flex flex-col gap-1 text-xs">
              <span className="font-medium">Harness inside (optional)</span>
              <Input
                id="image-harness"
                value={harness}
                onChange={(e) => setHarness(e.target.value)}
                placeholder="opencode"
              />
            </label>
          </div>
          <label htmlFor="image-ref" className="flex flex-col gap-1 text-xs">
            <span className="font-medium">Image, pinned by digest</span>
            <Input
              id="image-ref"
              className="font-mono text-xs"
              value={image}
              onChange={(e) => setImage(e.target.value)}
              placeholder="ghcr.io/org/godot-node@sha256:…"
            />
          </label>
          <p className="text-xs text-muted-foreground">
            A tag is not accepted: it can be pointed at different contents after the
            image was checked. A harness is only skipped on each run if the smoke test
            finds the version this deployment expects.
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              disabled={!name.trim() || !image.trim() || importImage.isPending}
              onClick={() => importImage.mutate()}
            >
              Check and add
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {editing && (
        <DockerfileEditor
          key={editing.name || "new"}
          initial={editing}
          onClose={() => setEditing(null)}
          onChanged={invalidate}
        />
      )}

      {images.isLoading ? (
        <p className="text-xs text-muted-foreground">Loading…</p>
      ) : list.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No images yet. Importing a bundle that carries one adds it here too.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-border">
          {list.map((i) => (
            <ImageRow
              key={i.name}
              image={i}
              open={open === i.name}
              onToggle={() => setOpen(open === i.name ? null : i.name)}
              onAction={(kind) => act.mutate({ kind, imageName: i.name })}
              busy={act.isPending}
              onPromoted={invalidate}
              onEdit={() =>
                setEditing({
                  name: i.name,
                  dockerfile: i.dockerfile,
                  harness: i.harness_key,
                  isNew: false,
                })
              }
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function StatusBadge({ build }: { build: Build }) {
  const tone =
    build.status === "ready"
      ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
      : build.status === "failed"
        ? "bg-destructive/15 text-destructive"
        : ACTIVE.has(build.status)
          ? "bg-sky-500/15 text-sky-700 dark:text-sky-400"
          : "bg-muted text-muted-foreground";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${tone}`}>
      {STATUS_LABEL[build.status] ?? build.status}
    </span>
  );
}

function ImageRow({
  image,
  open,
  onToggle,
  onAction,
  busy,
  onPromoted,
  onEdit,
}: {
  image: Image;
  open: boolean;
  onToggle: () => void;
  onAction: (kind: "recheck" | "cancel" | "remove") => void;
  busy: boolean;
  onPromoted: () => void;
  onEdit: () => void;
}) {
  const latest = image.latest;
  const current = image.current;
  const active = latest ? ACTIVE.has(latest.status) : false;
  return (
    <li className="flex flex-col gap-2 py-2">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <span className="text-sm font-medium">{image.name}</span>
          <span className="ml-2 text-[11px] text-muted-foreground">{image.origin}</span>
          {latest && (
            <span className="ml-2">
              <StatusBadge build={latest} />
            </span>
          )}
          <div className="truncate font-mono text-xs text-muted-foreground">
            {current?.pinned_ref || latest?.target_ref || ""}
          </div>
          {latest?.error && (
            <div className="text-xs text-destructive">{latest.error}</div>
          )}
        </div>
        <div className="flex shrink-0 gap-1">
          <Button size="sm" variant="ghost" onClick={onToggle}>
            {open ? "Hide" : "History"}
          </Button>
          {image.origin === "built" && !active && (
            <Button size="sm" variant="ghost" onClick={onEdit}>
              Edit / build
            </Button>
          )}
          {latest?.external_url && (
            <a
              className="self-center px-2 text-xs underline"
              href={latest.external_url}
              target="_blank"
              rel="noreferrer"
            >
              builder log
            </a>
          )}
          {active ? (
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAction("cancel")}>
              Cancel
            </Button>
          ) : (
            image.origin === "imported" && (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAction("recheck")}>
                Check again
              </Button>
            )
          )}
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              if (confirm(`Remove "${image.name}"? Repositories using it will stop building until they pick another runtime.`))
                onAction("remove");
            }}
          >
            Remove
          </Button>
        </div>
      </div>
      {open && <History name={image.name} onPromoted={onPromoted} dockerfile={image.dockerfile} />}
    </li>
  );
}

function History({
  name,
  dockerfile,
  onPromoted,
}: {
  name: string;
  dockerfile: string;
  onPromoted: () => void;
}) {
  const queryClient = useQueryClient();
  const builds = useQuery({
    queryKey: ["images", name, "builds"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/images/{name}/builds", {
        params: { path: { name } },
      });
      if (error) throw error;
      return data;
    },
  });
  const promote = useMutation({
    mutationFn: async (buildId: string) => {
      const { error } = await apiClient.POST("/images/{name}/builds/{build_id}/promote", {
        params: { path: { name, build_id: buildId } },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["images", name, "builds"] });
      onPromoted();
    },
    onError: (e) => toast.error(describe(e, "Could not make that build current")),
  });

  return (
    <div className="flex flex-col gap-2 rounded-md bg-muted/40 p-3 text-xs">
      {(builds.data ?? []).map((b) => (
        <div key={b.id} className="flex flex-col gap-1 border-b border-border pb-2 last:border-0">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <StatusBadge build={b} />
              {b.current && <span className="font-medium">current</span>}
              <span className="text-muted-foreground">
                {new Date(b.created_at).toLocaleString()}
              </span>
            </div>
            {b.status === "ready" && !b.current && (
              <Button
                size="sm"
                variant="outline"
                disabled={promote.isPending}
                onClick={() => promote.mutate(b.id)}
              >
                Make current
              </Button>
            )}
          </div>
          <div className="break-all font-mono text-muted-foreground">
            {b.pinned_ref || b.target_ref}
          </div>
          {b.smoke && (
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded bg-background p-2 font-mono text-[11px]">
              {b.smoke}
            </pre>
          )}
          {b.error && <div className="text-destructive">{b.error}</div>}
          {b.log_tail && (
            <details>
              <summary className="cursor-pointer text-muted-foreground">Builder output</summary>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-background p-2 font-mono text-[11px]">
                {b.log_tail}
              </pre>
            </details>
          )}
        </div>
      ))}
      {dockerfile && (
        <details>
          <summary className="cursor-pointer text-muted-foreground">How it was made (Dockerfile)</summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-background p-2 font-mono text-[11px]">
            {dockerfile}
          </pre>
        </details>
      )}
    </div>
  );
}
