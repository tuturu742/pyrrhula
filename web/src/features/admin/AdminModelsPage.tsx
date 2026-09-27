import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * Which models this deployment embeds and reranks with.
 *
 * Deployment-level rather than per tenant: every tenant's vectors live in one column of
 * one width, so "which embedding model" cannot coherently differ between them. Changes
 * take effect on the next restart — the providers hold a loaded model, and swapping it
 * under a running process would change what a half-finished retrieval means partway
 * through.
 */

/**
 * Whether the models are actually on this box.
 *
 * The installers used to block on a multi-gigabyte download, so "did it work" was
 * answered by the install finishing. They no longer have to, which means something has to
 * be able to say — and an air-gapped deployment needs a way in that is not "reach
 * huggingface.co".
 */
function ModelCacheCard() {
  const queryClient = useQueryClient();
  const [uploading, setUploading] = useState(false);
  const { data } = useQuery({
    queryKey: ["admin-retrieval-cache"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/retrieval-models/cache");
      if (error) throw error;
      return data as {
        cache_path: string;
        offline: boolean;
        total_mb: number;
        models: { model: string; present: boolean; size_mb: number }[];
      };
    },
    // While a download runs there is no event to wait for, so ask periodically.
    refetchInterval: 15000,
  });

  const download = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/admin/retrieval-models/download");
      if (error) throw error;
    },
    onSuccess: () =>
      toast.success("Download queued — this takes a few minutes; the sizes below will grow."),
    onError: () => toast.error("Could not queue the download."),
  });

  async function upload(file: File) {
    setUploading(true);
    try {
      const body = new FormData();
      body.append("file", file);
      const resp = await fetch("/api/admin/retrieval-models/upload", {
        method: "POST",
        body,
        credentials: "include",
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail ?? `upload failed (${resp.status})`);
      }
      toast.success("Cache installed.");
      void queryClient.invalidateQueries({ queryKey: ["admin-retrieval-cache"] });
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setUploading(false);
    }
  }

  const missing = (data?.models ?? []).filter((m) => !m.present);

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <div>
        <h2 className="text-sm font-medium">On this deployment</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Models are cached in <code>{data?.cache_path ?? "…"}</code>, shared by the api and
          the worker. Nothing here is required: whatever is missing is fetched the first
          time something embeds — that first call is just slow.
        </p>
      </div>

      <ul className="flex flex-col gap-1 text-sm">
        {(data?.models ?? []).map((m) => (
          <li key={m.model} className="flex items-center gap-2">
            <span className={m.present ? "text-emerald-600" : "text-amber-600"}>
              {m.present ? "●" : "○"}
            </span>
            <code className="text-xs">{m.model}</code>
            <span className="text-xs text-muted-foreground">
              {m.present ? `${m.size_mb} MB` : "not downloaded"}
            </span>
          </li>
        ))}
      </ul>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={download.isPending || data?.offline}
          onClick={() => download.mutate()}
        >
          {download.isPending ? "Queueing…" : "Download from Hugging Face"}
        </Button>
        <label className="text-sm">
          <span className="cursor-pointer rounded-md border border-input px-3 py-1.5 text-sm">
            {uploading ? "Installing…" : "Upload cache archive"}
          </span>
          <input
            type="file"
            accept=".tar,.gz,.tgz,application/gzip,application/x-tar"
            className="hidden"
            disabled={uploading}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void upload(file);
              e.target.value = "";
            }}
          />
        </label>
      </div>

      {data?.offline ? (
        <p className="text-xs text-muted-foreground">
          This deployment runs offline (<code>HF_HUB_OFFLINE=1</code>): the api and worker
          never reach Hugging Face on their own. <b>Download from Hugging Face</b> above is
          the one exception — it lifts that for its own fetch, on the worker, and puts it
          back. Use the archive upload only where the worker has no route to{" "}
          <code>huggingface.co</code> at all.
        </p>
      ) : null}
      {missing.length > 0 ? (
        <div className="text-xs text-muted-foreground">
          <p>
            Air-gapped? On a machine that can reach Hugging Face, with Python and{" "}
            <code>pip install huggingface_hub</code>:
          </p>
          <pre className="mt-1 overflow-x-auto rounded-md bg-secondary/40 p-2 font-mono">
{`python -c "from huggingface_hub import snapshot_download as d; ${missing
  .map((m) => `d('${m.model.replace(/^local\//, "")}')`)
  .join("; ")}"
tar czf cache.tgz -C ~/.cache/huggingface hub`}
          </pre>
          <p className="mt-1">
            The first line downloads into <code>~/.cache/huggingface/hub</code>; the second
            archives that <code>hub</code> directory (<code>-C</code> changes into its parent,{" "}
            <code>hub</code> is what gets archived). Upload <code>cache.tgz</code> here.
          </p>
        </div>
      ) : null}
    </div>
  );
}

function AssistantConnectionCard() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["admin-assistant-model"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/assistant/model");
      if (error) throw error;
      return data;
    },
  });
  const [provider, setProvider] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [apiBase, setApiBase] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState("");

  const save = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/admin/assistant/model", {
        body: {
          provider: provider ?? data?.provider ?? "",
          model: model ?? data?.model ?? "",
          api_base: apiBase ?? data?.api_base ?? null,
          api_key: apiKey || null,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      setApiKey("");
      toast.success("Assistant model saved.");
      void queryClient.invalidateQueries({ queryKey: ["admin-assistant-model"] });
    },
    onError: () => toast.error("Could not save the assistant model."),
  });

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <div>
        <h2 className="text-sm font-medium">Assistant model</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          A connection on the reserved admin organization — separate from any tenant&apos;s,
          so nobody&apos;s workspace budget pays for console questions.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Input
          className="w-36"
          placeholder="provider"
          value={provider ?? data?.provider ?? ""}
          onChange={(e) => setProvider(e.target.value)}
        />
        <Input
          className="w-56"
          placeholder="model"
          value={model ?? data?.model ?? ""}
          onChange={(e) => setModel(e.target.value)}
        />
        <Input
          className="w-64"
          placeholder="api base (optional)"
          value={apiBase ?? data?.api_base ?? ""}
          onChange={(e) => setApiBase(e.target.value)}
        />
        <Input
          className="w-64"
          type="password"
          placeholder={data?.provider ? "api key (leave blank to keep)" : "api key"}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <Button size="sm" variant="outline" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}


export function AdminModelsPage() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["admin-retrieval-models"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/admin/retrieval-models");
      if (error) throw error;
      return data;
    },
  });

  const [embeddingModel, setEmbeddingModel] = useState("");
  const [dimension, setDimension] = useState("");
  const [rerankerModel, setRerankerModel] = useState("");
  const [rerankerEnabled, setRerankerEnabled] = useState(true);

  useEffect(() => {
    if (!data) return;
    setEmbeddingModel(data.embedding_model ?? "");
    setDimension(String(data.embedding_dimension ?? ""));
    setRerankerModel(data.reranker_model ?? "");
    setRerankerEnabled(Boolean(data.reranker_enabled));
  }, [data]);

  const save = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/admin/retrieval-models", {
        body: {
          embedding_model: embeddingModel.trim(),
          embedding_dimension: Number(dimension),
          reranker_model: rerankerModel.trim(),
          reranker_enabled: rerankerEnabled,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Saved — restart the api and worker to apply.");
      void queryClient.invalidateQueries({ queryKey: ["admin-retrieval-models"] });
    },
    onError: () => toast.error("Could not save — check the model name and dimension."),
  });

  if (isLoading || !data) return <p className="text-sm text-muted-foreground">Loading…</p>;

  const embeddingChanged = embeddingModel.trim() !== (data.embedding_model ?? "");
  const orphans = data.embedded_chunks ?? 0;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Models</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          The models this deployment uses itself — not a tenant's. Two it runs locally
          (one embeds text for search, one reranks what search found), plus the connection
          its own assistant talks to. Retrieval settings are read from{" "}
          <b>{data.source}</b>. Changes apply {data.applies}.
        </p>
      </div>

      <ModelCacheCard />
      <AssistantConnectionCard />

      <div className="flex flex-col gap-3 rounded-md border border-border p-4">
        <label className="flex flex-col gap-1 text-sm" htmlFor="embedding-model">
          <span className="font-medium">Embedding model</span>
          <Input
            id="embedding-model"
            value={embeddingModel}
            onChange={(e) => setEmbeddingModel(e.target.value)}
          />
          <span className="text-xs text-muted-foreground">
            Any sentence-transformers model, prefixed <code>local/</code> to run it in
            process. The default install uses <code>local/BAAI/bge-m3</code> because it is
            multilingual, permissively licensed and runs on CPU — a starting point, not a
            recommendation.
          </span>
        </label>

        <label className="flex flex-col gap-1 text-sm" htmlFor="embedding-dimension">
          <span className="font-medium">Embedding dimension</span>
          <Input
            id="embedding-dimension"
            value={dimension}
            onChange={(e) => setDimension(e.target.value)}
            inputMode="numeric"
          />
          <span className="text-xs text-muted-foreground">
            Must match the model&apos;s output width. It is checked against the loaded
            model at startup, so a mismatch fails on boot rather than returning nothing at
            query time.
          </span>
        </label>

        {embeddingChanged && orphans > 0 && (
          <p className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
            <b>{orphans.toLocaleString()} stored vectors would be orphaned.</b> Embeddings
            from different models are not comparable, so existing knowledge stops being
            findable until it is re-indexed. Nothing is deleted — but search will be
            wrong until the content is embedded again.
          </p>
        )}
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-border p-4">
        <label className="flex items-center gap-2 text-sm font-medium" htmlFor="rerank-enabled">
          <input
            id="rerank-enabled"
            type="checkbox"
            checked={rerankerEnabled}
            onChange={(e) => setRerankerEnabled(e.target.checked)}
          />
          Rerank results
        </label>
        <p className="text-xs text-muted-foreground">
          A second pass that reorders what search found. Safe to change or turn off at any
          time — it stores nothing. Off, ranking falls back to the fused retrieval order.
        </p>
        <label className="flex flex-col gap-1 text-sm" htmlFor="reranker-model">
          <span className="font-medium">Reranker model</span>
          <Input
            id="reranker-model"
            value={rerankerModel}
            onChange={(e) => setRerankerModel(e.target.value)}
            disabled={!rerankerEnabled}
          />
        </label>
      </div>

      <div>
        <Button
          onClick={() => save.mutate()}
          disabled={save.isPending || !embeddingModel.trim() || !dimension}
        >
          {save.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}
