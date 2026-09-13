import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/auth";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";

/** The import half of portability (§16.6): upload a .pyr bundle (synchronous verdict —
 * it lands or is refused with the broken link named), then clear the quarantine queue
 * one entry at a time. Both endpoints existed with no screen. */
export function ImportPanel({ workspaceId }: { workspaceId: string }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [result, setResult] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const token = useAuthStore((s) => s.token);
  const tenantSlug = useAuthStore((s) => s.tenantSlug);
  const queryClient = useQueryClient();

  const importBundle = useMutation({
    mutationFn: async (file: File) => {
      // multipart upload — the generated client doesn't cover FormData, plain fetch does
      const form = new FormData();
      form.append("file", file);
      if (password) form.append("password", password);
      const response = await fetch(`/api/export/import?workspace_id=${workspaceId}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "X-Pyrrhula-Tenant": tenantSlug ?? "",
        },
        body: form,
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail.slice(0, 300));
      }
      return response.json();
    },
    onSuccess: (data) => {
      setResult(JSON.stringify(data, null, 2));
      toast.success("Bundle imported.");
      void queryClient.invalidateQueries();
    },
    onError: (e) => {
      setResult(String(e));
      toast.error("Import refused — see the details below.");
    },
  });

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <h2 className="text-sm font-medium">Import a bundle</h2>
      <p className="text-xs text-muted-foreground">
        Content in a bundle was authored elsewhere: knowledge entries flagged by the
        scanner land in the quarantine queue below and stay out of retrieval until a
        person approves each one.
      </p>
      <div className="flex items-center gap-2">
        <input
          ref={fileInput}
          type="file"
          accept=".pyr,.tar.gz,.tgz"
          className="text-sm"
        />
        <input
          type="password"
          placeholder="password (if encrypted)"
          autoComplete="off"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-52 rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        />
        <Button
          variant="outline"
          disabled={importBundle.isPending}
          onClick={() => {
            const file = fileInput.current?.files?.[0];
            if (file) importBundle.mutate(file);
          }}
        >
          {importBundle.isPending ? "Importing…" : "Import"}
        </Button>
      </div>
      {result && (
        <pre className="max-h-48 overflow-auto rounded-md bg-muted p-2 text-xs">{result}</pre>
      )}
    </div>
  );
}

export function QuarantinePanel() {
  const queryClient = useQueryClient();
  const entries = useQuery({
    queryKey: ["import-quarantine"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/export/quarantine");
      if (error) throw error;
      return data;
    },
  });

  const approve = useMutation({
    mutationFn: async (entryId: string) => {
      const { error } = await apiClient.POST("/export/quarantine/{entry_id}/approve", {
        params: { path: { entry_id: entryId } },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Approved — the entry is live for retrieval.");
      void queryClient.invalidateQueries({ queryKey: ["import-quarantine"] });
    },
    onError: () => toast.error("Approval failed."),
  });

  const rows = entries.data ?? [];
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <h2 className="text-sm font-medium">Quarantine queue</h2>
      {entries.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {entries.isSuccess && rows.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Nothing waiting for review — imported entries the scanner flags appear here.
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {rows.map((entry) => (
          <li
            key={entry.id}
            className="flex items-center justify-between gap-3 rounded-md bg-muted p-2 text-sm"
          >
            <span>
              <span className="font-medium">{entry.title || entry.entry_key}</span>
              <span className="block text-xs text-muted-foreground">{entry.reason}</span>
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={approve.isPending}
              onClick={() => approve.mutate(entry.id)}
            >
              Approve
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
