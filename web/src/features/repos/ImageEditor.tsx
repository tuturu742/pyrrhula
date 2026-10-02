import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export type EditorState = { name: string; dockerfile: string; harness: string; isNew: boolean };

export const STARTER = `FROM debian:bookworm
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git ca-certificates \\
 && rm -rf /var/lib/apt/lists/*
`;

function describe(e: unknown, fallback: string): string {
  const detail = (e as { detail?: unknown })?.detail;
  return typeof detail === "string" ? detail : fallback;
}

/**
 * Write the toolchain, check it, save it, build it.
 *
 * Nothing builds on save. The validator's answer is shown beside the file; Build is a
 * separate, deliberate click that names which of the operator's builders runs it.
 */
export function DockerfileEditor({
  initial,
  onClose,
  onChanged,
}: {
  initial: EditorState;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [name, setName] = useState(initial.name);
  const [dockerfile, setDockerfile] = useState(initial.dockerfile);
  const [harness, setHarness] = useState(initial.harness);
  const [check, setCheck] = useState<{ errors: string[]; warnings: string[] } | null>(null);
  const [saved, setSaved] = useState(!initial.isNew);
  const [builder, setBuilder] = useState("");
  const [rebuild, setRebuild] = useState(false);

  const builders = useQuery({
    queryKey: ["images", "builders"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/images/builders");
      if (error) throw error;
      return data;
    },
  });
  const harnesses = useQuery({
    queryKey: ["harnesses"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/harnesses");
      if (error) throw error;
      return data;
    },
  });
  const chosenBuilder = builder || builders.data?.[0]?.key || "";

  const templates = useQuery({
    queryKey: ["images", "templates"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/images/templates");
      if (error) throw error;
      return data;
    },
  });
  const repos = useQuery({
    queryKey: ["repos", "for-propose"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/repos");
      if (error) throw error;
      return data;
    },
  });
  const connections = useQuery({
    queryKey: ["model-profiles", "for-propose"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles");
      if (error) throw error;
      return data;
    },
  });
  const [proposeRepo, setProposeRepo] = useState("");
  const [proposeWith, setProposeWith] = useState("");
  const [rationale, setRationale] = useState<string | null>(null);

  const propose = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/images/propose", {
        body: { repo_id: proposeRepo, agent_id: proposeWith, harness_key: harness },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (result) => {
      setDockerfile(result.dockerfile);
      setRationale(result.rationale);
      setCheck({ errors: result.errors, warnings: result.warnings });
      setSaved(false);
    },
    onError: (e) => toast.error(describe(e, "Could not draft a Dockerfile")),
  });

  const validate = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/images/validate", { body: { dockerfile } });
      if (error) throw error;
      return data;
    },
    onSuccess: setCheck,
  });

  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.PUT("/images/{name}", {
        params: { path: { name: name.trim() } },
        body: { dockerfile, harness_key: harness },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (result) => {
      setCheck(result);
      setSaved(true);
      onChanged();
      toast.success("Saved");
    },
    onError: (e) => toast.error(describe(e, "Could not save")),
  });

  const build = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST("/images/{name}/build", {
        params: { path: { name: name.trim() } },
        body: { builder_key: chosenBuilder, rebuild },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (result) => {
      toast.success(
        result.reused
          ? "Already built from this exact recipe — that image is current again"
          : "Sent to the builder",
      );
      onChanged();
      onClose();
    },
    onError: (e) => toast.error(describe(e, "Could not start the build")),
  });

  return (
    <div className="flex flex-col gap-2 rounded-md border border-dashed border-border p-3">
      <div className="grid gap-2 sm:grid-cols-2">
        <label htmlFor="df-name" className="flex flex-col gap-1 text-xs">
          <span className="font-medium">Name (becomes the runtime name)</span>
          <Input
            id="df-name"
            value={name}
            disabled={!initial.isNew}
            onChange={(e) => {
              setName(e.target.value);
              setSaved(false);
            }}
            placeholder="godot-node"
          />
        </label>
        <label htmlFor="df-harness" className="flex flex-col gap-1 text-xs">
          <span className="font-medium">Bake in a harness (installed by the platform)</span>
          <select
            id="df-harness"
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
            value={harness}
            onChange={(e) => {
              setHarness(e.target.value);
              setSaved(false);
            }}
          >
            <option value="">none</option>
            {(harnesses.data ?? []).map((h) => (
              <option key={h.key} value={h.key}>
                {h.key}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted-foreground">Start from:</span>
        <select
          aria-label="Template"
          className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
          value=""
          onChange={(e) => {
            const t = (templates.data ?? []).find((x) => x.key === e.target.value);
            if (!t) return;
            setDockerfile(t.dockerfile);
            setHarness(t.harness_key);
            setRationale(t.description);
            setCheck(null);
            setSaved(false);
          }}
        >
          <option value="">a template…</option>
          {(templates.data ?? []).map((t) => (
            <option key={t.key} value={t.key}>
              {t.label}
            </option>
          ))}
        </select>
        <span className="text-muted-foreground">or propose from</span>
        <select
          aria-label="Repository to propose from"
          className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
          value={proposeRepo}
          onChange={(e) => setProposeRepo(e.target.value)}
        >
          <option value="">a repository…</option>
          {(repos.data ?? []).map((r) => (
            <option key={r.id} value={r.id}>
              {r.name}
            </option>
          ))}
        </select>
        <select
          aria-label="Model to draft with"
          className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
          value={proposeWith}
          onChange={(e) => setProposeWith(e.target.value)}
        >
          <option value="">with model…</option>
          {(connections.data ?? [])
            .filter((c) => c.provider)
            .map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
        </select>
        <Button
          size="sm"
          variant="outline"
          disabled={!proposeRepo || !proposeWith || propose.isPending}
          onClick={() => propose.mutate()}
        >
          {propose.isPending ? "Drafting…" : "Propose"}
        </Button>
      </div>
      {rationale && (
        <p className="rounded bg-muted/50 px-2 py-1 text-xs text-muted-foreground">{rationale}</p>
      )}
      <p className="rounded bg-amber-500/10 px-2 py-1 text-xs text-amber-800 dark:text-amber-300">
        Tools only: the build sees this file and nothing else — not your repository. Never
        put a secret here; anyone who can pull the image can read every layer of it.
      </p>
      <textarea
        aria-label="Dockerfile"
        className="min-h-[220px] rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        value={dockerfile}
        spellCheck={false}
        onChange={(e) => {
          setDockerfile(e.target.value);
          setSaved(false);
          setCheck(null);
        }}
      />
      {check && (
        <ul className="flex flex-col gap-0.5 text-xs">
          {check.errors.map((m) => (
            <li key={`e${m}`} className="text-destructive">
              ✗ {m}
            </li>
          ))}
          {check.warnings.map((m) => (
            <li key={`w${m}`} className="text-amber-700 dark:text-amber-400">
              ! {m}
            </li>
          ))}
          {check.errors.length === 0 && check.warnings.length === 0 && (
            <li className="text-emerald-700 dark:text-emerald-400">✓ looks buildable</li>
          )}
        </ul>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          onClick={() => validate.mutate()}
          disabled={validate.isPending}
        >
          Check
        </Button>
        <Button size="sm" onClick={() => save.mutate()} disabled={!name.trim() || save.isPending}>
          Save
        </Button>
        <span className="mx-1 h-5 w-px bg-border" />
        {(builders.data ?? []).length === 0 ? (
          <span className="text-xs text-muted-foreground">
            No builder is available to this organization — an administrator declares one.
          </span>
        ) : (
          <>
            <select
              aria-label="Builder"
              className="h-8 rounded-md border border-input bg-transparent px-2 text-xs"
              value={chosenBuilder}
              onChange={(e) => setBuilder(e.target.value)}
            >
              {(builders.data ?? []).map((b) => (
                <option key={b.key} value={b.key}>
                  {b.label} → {b.registry_key}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1 text-xs">
              <input
                type="checkbox"
                checked={rebuild}
                onChange={(e) => setRebuild(e.target.checked)}
              />
              rebuild even if unchanged
            </label>
            <Button
              size="sm"
              disabled={
                !saved || !chosenBuilder || build.isPending || (check?.errors.length ?? 0) > 0
              }
              onClick={() => build.mutate()}
            >
              Build
            </Button>
          </>
        )}
        <Button size="sm" variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
    </div>
  );
}
