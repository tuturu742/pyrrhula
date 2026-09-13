import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { useAuthStore } from "@/stores/auth";
import { Button } from "@/components/ui/button";

/** G4.11's report pipeline, on screen at last: pick a template, generate, review
 * (which unlocks artifacts for templates that require it), render, download. The
 * whole reports router previously had zero UI. */
export function ReportPanel({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const token = useAuthStore((s) => s.token);
  const tenantSlug = useAuthStore((s) => s.tenantSlug);
  const [reportId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const templates = useQuery({
    queryKey: ["report-templates"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/reports/templates");
      if (error) throw error;
      return data;
    },
    enabled: open,
  });

  // POST /reports returns a job id; the report row appears when the worker lands it.
  const reports = useQuery({
    queryKey: ["session-reports", sessionId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/reports", {
        params: { query: { session_id: sessionId } },
      });
      if (error) throw error;
      return data;
    },
    enabled: open,
    refetchInterval: 5000,
  });
  const current = (reports.data ?? []).find((r) => r.id === reportId) ?? reports.data?.[0];

  const generate = useMutation({
    mutationFn: async (templateKey: string) => {
      const { data, error } = await apiClient.POST("/reports", {
        body: { session_id: sessionId, template_key: templateKey },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      toast.success("Report generating — it appears here when ready.");
      void queryClient.invalidateQueries({ queryKey: ["session-reports", sessionId] });
    },
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Could not start the report.")),
  });

  const review = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.POST("/reports/{report_id}/review", {
        params: { path: { report_id: current!.id } },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Marked reviewed — artifacts unlocked.");
      void queryClient.invalidateQueries({ queryKey: ["session-reports", sessionId] });
    },
    onError: () => toast.error("Review failed."),
  });

  const render = useMutation({
    mutationFn: async (format: string) => {
      const { error } = await apiClient.POST("/reports/{report_id}/render", {
        params: { path: { report_id: current!.id } },
        body: { output_format: format },
      });
      if (error) throw error;
      return format;
    },
    onSuccess: (format) => {
      toast.success(`Rendering ${format} — download below shortly.`);
      void queryClient.invalidateQueries({ queryKey: ["session-reports", sessionId] });
    },
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Render failed.")),
  });

  async function download(format: string) {
    const response = await fetch(`/api/reports/${current!.id}/artifacts/${format}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        "X-Pyrrhula-Tenant": tenantSlug ?? "",
      },
    });
    if (!response.ok) {
      toast.error("Artifact not ready yet.");
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `report-${current!.id}.${format === "markdown" ? "md" : format}`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium">Report</h2>
        <Button variant="ghost" size="sm" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Generate a report"}
        </Button>
      </div>
      {open && !current && (
        <div className="flex flex-wrap gap-2">
          {(templates.data ?? []).map((template) => (
            <Button
              key={template.key}
              variant="outline"
              size="sm"
              disabled={generate.isPending}
              onClick={() => generate.mutate(template.key)}
            >
              {template.key}
            </Button>
          ))}
          {templates.isSuccess && (templates.data ?? []).length === 0 && (
            <p className="text-sm text-muted-foreground">
              No report templates in this workflow.
            </p>
          )}
        </div>
      )}
      {open && current && (
        <div className="flex flex-col gap-2 text-sm">
          <p className="text-xs text-muted-foreground">
            {current.template_key}
            {current.reviewed_by ? " · reviewed" : " · awaiting review"}
          </p>
          {current.content_md && (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">
              {current.content_md}
            </pre>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => review.mutate()}>
              Mark reviewed
            </Button>
            <Button variant="outline" size="sm" onClick={() => render.mutate("markdown")}>
              Render markdown
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void download("markdown")}>
              Download
            </Button>
            
          </div>
        </div>
      )}
    </div>
  );
}
