import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

interface UploadPanelProps {
  sourceId: string;
}

const _TERMINAL_STATUSES = new Set(["done", "failed"]);

/**
 * D1.1's upload/ingestion flow: file drop -> enqueue (A1.2's worker job, never parsed
 * in-process) -> poll job status until it reaches a terminal state -> the draft entries
 * it produced show up in EntryList once the panel's own query invalidation fires.
 */
export function UploadPanel({ sourceId }: UploadPanelProps) {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [scopeKey, setScopeKey] = useState("workspace_public");
  const [uploadClass, setUploadClass] = useState("rules");
  const [dragOver, setDragOver] = useState(false);

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("class", uploadClass);
      formData.append("scope_key", scopeKey);
      const { data, error } = await apiClient.POST("/knowledge/sources/{source_id}/ingest", {
        params: { path: { source_id: sourceId } },
        // openapi-fetch skips JSON serialization for a FormData body and lets the
        // browser set the multipart Content-Type (with boundary) itself.
        body: formData as unknown as { file: string; class: string; scope_key: string },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      if (data) setJobId(data.job_id);
    },
  });

  const jobStatus = useQuery({
    queryKey: ["knowledge-ingest-job", jobId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/knowledge/jobs/{job_id}", {
        params: { path: { job_id: jobId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: jobId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === undefined || _TERMINAL_STATUSES.has(status)) return false;
      return 1000;
    },
  });

  if (jobStatus.data?.status === "done" && jobId !== null) {
    // One-shot invalidation the moment we observe the terminal state -- refetchInterval
    // above already stopped polling by returning false on this same render.
    void queryClient.invalidateQueries({ queryKey: ["knowledge-entries", sourceId] });
  }

  function handleFile(file: File | undefined) {
    if (!file) return;
    upload.mutate(file);
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Class</span>
          <select
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={uploadClass}
            onChange={(e) => setUploadClass(e.target.value)}
          >
            <option value="rules">rules</option>
            <option value="lore">lore</option>
            <option value="misc">misc</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Scope</span>
          <input
            className="rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={scopeKey}
            onChange={(e) => setScopeKey(e.target.value)}
          />
        </label>
      </div>

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          handleFile(e.dataTransfer.files[0]);
        }}
        onClick={() => fileInputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            fileInputRef.current?.click();
          }
        }}
        role="button"
        tabIndex={0}
        className={`flex cursor-pointer flex-col items-center justify-center rounded-md border-2 border-dashed p-8 text-center text-sm text-muted-foreground ${
          dragOver ? "border-primary bg-accent" : "border-border"
        }`}
      >
        Drop a document here, or click to choose a file.
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          onChange={(e) => handleFile(e.target.files?.[0])}
        />
      </div>

      {upload.error !== null && <p className="text-sm text-destructive">Upload failed.</p>}

      {jobId !== null && (
        <div className="rounded-md border border-border p-3 text-sm">
          <div className="text-muted-foreground">Ingestion job {jobId}</div>
          <div className="font-medium">
            {jobStatus.data?.status ?? "starting…"}
            {jobStatus.data?.status === "done" && " — entries ready for review below"}
            {jobStatus.data?.status === "failed" && `: ${jobStatus.data.error ?? "unknown error"}`}
          </div>
        </div>
      )}
    </div>
  );
}
