import { useTheme } from "@/components/ThemeProvider";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { apiClient } from "@/lib/api-client/client";

interface GraphNodeData {
  id: string;
  label: string;
  kind: string;
  summary: string;
}
interface GraphEdgeData {
  source: string;
  target: string;
  label: string;
}

// Accent hues stay fixed; fills are mixed INTO the theme background so nodes read
// correctly in both light and dark (hardcoded pastels floated on the dark canvas).
const KIND_STYLE: Record<string, { background: string; border: string }> = {
  repo: {
    background: "color-mix(in oklch, #3b82f6 18%, var(--background))",
    border: "#3b82f6",
  },
  concept: {
    background: "color-mix(in oklch, #eab308 18%, var(--background))",
    border: "#eab308",
  },
  module: {
    background: "color-mix(in oklch, #22c55e 18%, var(--background))",
    border: "#22c55e",
  },
  service: {
    background: "color-mix(in oklch, #d946ef 18%, var(--background))",
    border: "#d946ef",
  },
};

/** Circular layout: no layout engine dependency, stable for the small graphs the
 * analysis produces (a handful of repos + at most five concepts). */
function layout(nodes: GraphNodeData[]): Node[] {
  const radius = Math.max(180, nodes.length * 55);
  return nodes.map((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(nodes.length, 1);
    const style = KIND_STYLE[n.kind] ?? {
      background: "var(--muted)",
      border: "var(--border)",
    };
    return {
      id: n.id,
      position: {
        x: radius + radius * Math.cos(angle),
        y: radius + radius * Math.sin(angle),
      },
      data: { label: n.label },
      style: {
        background: style.background,
        border: `2px solid ${style.border}`,
        borderRadius: 8,
        padding: 8,
        fontSize: 12,
        color: "var(--foreground)",
        maxWidth: 180,
      },
    };
  });
}

/**
 * The repo knowledge graph: what the workspace's analyzed repos collectively achieve.
 * Rendered from published knowledge entries (the same rows agents retrieve from) — this
 * page is a viewer, the analysis worker is the author.
 */
export function RepoGraphPage() {
  const { resolved } = useTheme();
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const queryClient = useQueryClient();
  const [analysisRequested, setAnalysisRequested] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const { data: graph, isLoading } = useQuery({
    queryKey: ["repo-graph", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET(
        "/workspaces/{workspace_id}/repo-graph",
        {
          params: { path: { workspace_id: workspaceId! } },
        },
      );
      if (error) throw error;
      return data;
    },
    enabled: !!workspaceId,
    // While an analysis is running there is nothing to subscribe to — poll gently.
    // Keyed on what the SERVER reports as well as on this component's memory, so a
    // page refresh mid-analysis keeps watching instead of going quiet.
    refetchInterval: (query) => {
      const status = (
        query.state.data as { analysis?: { status?: string } } | undefined
      )?.analysis?.status;
      const live = status === "pending" || status === "running";
      return live || analysisRequested ? 5000 : false;
    },
  });

  const analyze = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST(
        "/workspaces/{workspace_id}/repo-analysis",
        {
          params: { path: { workspace_id: workspaceId! } },
          body: { repo_ids: [] },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      setAnalysisRequested(true);
      void queryClient.invalidateQueries({
        queryKey: ["repo-graph", workspaceId],
      });
    },
  });

  const rawNodes = useMemo(
    () => ((graph?.graph?.nodes ?? []) as GraphNodeData[]).filter((n) => n.id),
    [graph],
  );
  const nodes = useMemo(() => layout(rawNodes), [rawNodes]);
  const edges: Edge[] = useMemo(
    () =>
      ((graph?.graph?.edges ?? []) as GraphEdgeData[])
        .filter((e) => e.source && e.target)
        .map((e, i) => ({
          id: `e${i}`,
          source: e.source,
          target: e.target,
          label: e.label || undefined,
          style: { strokeWidth: 1.5 },
        })),
    [graph],
  );
  const selectedNode = rawNodes.find((n) => n.id === selected);

  if (!workspaceId) return null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Repo knowledge graph</h1>
          <p className="text-sm text-muted-foreground">
            What this workspace's repos collectively achieve — written into
            workspace knowledge, so agents see it too.{" "}
            <Link to={`/workspaces/${workspaceId}`} className="underline">
              Back to workspace
            </Link>
          </p>
        </div>
        <button
          type="button"
          disabled={analyze.isPending}
          onClick={() => analyze.mutate()}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
        >
          {analyze.isPending
            ? "Queuing…"
            : graph?.available
              ? "Re-analyze repos"
              : "Analyze repos"}
        </button>
      </div>
      {analyze.error !== null && (
        <p className="text-sm text-destructive">
          Could not start the analysis — are any repos registered?
        </p>
      )}
      {/* The job's own outcome, not just whether queuing worked. A job that was queued
          and then failed used to look exactly like one nobody had asked for: the page
          said nothing, forever, and a refresh lost even the "queued" notice because it
          lived in component state. Observed live — an analysis died on a binary file in
          one of three repositories and left no trace anywhere a user could see. */}
      {graph?.analysis?.status === "failed" && (
        <p className="text-sm text-destructive">
          The last analysis failed:{" "}
          {graph.analysis.error ?? "no reason recorded"}. Fix the cause and run
          it again.
        </p>
      )}
      {(graph?.analysis?.status === "pending" ||
        graph?.analysis?.status === "running") && (
        <p className="text-sm text-muted-foreground">
          Analysis in progress — this page refreshes itself as results land (a
          few minutes on local models).
        </p>
      )}
      {analysisRequested && !analyze.isPending && !graph?.analysis && (
        <p className="text-sm text-muted-foreground">
          Analysis queued — this page refreshes itself as results land (a few
          minutes on local models).
        </p>
      )}

      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {graph && !graph.available && (
        <p className="text-muted-foreground">
          No analysis yet. Register repos on the Repos page, then analyze.
        </p>
      )}

      {nodes.length > 0 && (
        <div className="h-[480px] rounded-md border border-border">
          <ReactFlow
            colorMode={resolved}
            nodes={nodes}
            edges={edges}
            fitView
            nodesDraggable
            onNodeClick={(_e, node) => setSelected(node.id)}
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
          </ReactFlow>
        </div>
      )}
      {selectedNode && selectedNode.summary && (
        <div className="rounded-md border border-border bg-secondary/40 p-3">
          <div className="text-sm font-medium">{selectedNode.label}</div>
          <p className="whitespace-pre-wrap text-sm text-muted-foreground">
            {selectedNode.summary}
          </p>
        </div>
      )}

      {graph?.available && graph.overview_md && (
        <section className="flex flex-col gap-2 rounded-md border border-border p-4">
          <h2 className="font-medium">Overview</h2>
          <p className="whitespace-pre-wrap text-sm">{graph.overview_md}</p>
        </section>
      )}
      {graph?.available &&
        Object.entries(graph.repo_summaries ?? {}).map(([key, summary]) => (
          <section
            key={key}
            className="flex flex-col gap-2 rounded-md border border-border p-4"
          >
            <h2 className="font-medium">
              Repo: <span className="font-mono">{key}</span>
            </h2>
            <p className="whitespace-pre-wrap text-sm">{summary as string}</p>
          </section>
        ))}
    </div>
  );
}
