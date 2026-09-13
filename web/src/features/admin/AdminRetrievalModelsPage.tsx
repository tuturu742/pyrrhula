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
export function AdminRetrievalModelsPage() {
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
        <h1 className="text-xl font-semibold">Retrieval models</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          The two models this deployment runs itself: one embeds text for search, the
          other reranks what search found. Currently read from{" "}
          <b>{data.source}</b>. Changes apply {data.applies}.
        </p>
      </div>

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
