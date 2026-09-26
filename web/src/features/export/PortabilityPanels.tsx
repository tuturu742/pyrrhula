import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/auth";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";

/** The import half of portability: upload a .pyr bundle (synchronous verdict —
 * it lands or is refused with the broken link named), then clear the quarantine queue
 * one entry at a time. Both endpoints existed with no screen. */
type BundleItem = { key: string; name: string; collides: boolean };
type Inspection = {
  tenant_ref: string;
  workflow_key: string;
  app_version: string;
  exported_at: string;
  encrypted: boolean;
  collisions: number;
  compatibility: string;
  compatibility_note: string;
  sections: Record<string, BundleItem[]>;
};

/** The order the importer lands them in, which is also the order they read in. */
const SECTION_ORDER = [
  "knowledge",
  "entities",
  "sessions",
  "personas",
  "scopes",
  "flows",
  "rules",
  "vocabulary",
  "secrets",
] as const;

const SECTION_LABEL: Record<string, string> = {
  knowledge: "Knowledge sources",
  entities: "Entities and schemas",
  sessions: "Sessions and history",
  personas: "Personas",
  scopes: "Scope bands",
  flows: "Flows",
  rules: "Rule systems and their tools",
  vocabulary: "Vocabulary overlays",
  secrets: "Secrets",
};

export function ImportPanel({ workspaceId }: { workspaceId: string }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [result, setResult] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const token = useAuthStore((s) => s.token);
  const tenantSlug = useAuthStore((s) => s.tenantSlug);
  const queryClient = useQueryClient();

  const post = async (path: string, extra?: (form: FormData) => void) => {
    const file = fileInput.current?.files?.[0];
    if (!file) throw new Error("Choose a .pyr file first.");
    // multipart upload — the generated client doesn't cover FormData, plain fetch does
    const form = new FormData();
    form.append("file", file);
    if (password) form.append("password", password);
    extra?.(form);
    const response = await fetch(path, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "X-Pyrrhula-Tenant": tenantSlug ?? "" },
      body: form,
    });
    if (!response.ok) throw new Error((await response.text()).slice(0, 300));
    return response.json();
  };

  const inspect = useMutation({
    mutationFn: async () => (await post("/api/export/import/inspect")) as Inspection,
    onSuccess: (data) => {
      setInspection(data);
      setResult(null);
      // Everything on by default EXCEPT what is already here. Import forks a colliding
      // key rather than overwriting it, so re-importing an updated bundle with the
      // defaults doubles every unchanged section -- which is the thing this preview
      // exists to stop happening silently.
      setChosen(
        new Set(
          SECTION_ORDER.filter((name) => {
            const items = data.sections[name] ?? [];
            return items.length > 0 && !items.every((i) => i.collides);
          }),
        ),
      );
    },
    onError: (e) => {
      setInspection(null);
      setResult(String(e));
      toast.error("Could not read that bundle.");
    },
  });

  const importBundle = useMutation({
    mutationFn: async () =>
      post("/api/export/import?workspace_id=" + workspaceId, (form) => {
        if (inspection) form.append("sections", [...chosen].join(","));
      }),
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

  const toggle = (name: string) => {
    const next = new Set(chosen);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    setChosen(next);
  };

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
          onChange={() => {
            setInspection(null);
            setResult(null);
          }}
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
          disabled={inspect.isPending}
          onClick={() => inspect.mutate()}
        >
          {inspect.isPending ? "Reading…" : "Inspect"}
        </Button>
        <Button
          variant="outline"
          // Nothing ticked must never reach the server: an empty `sections` field arrives
          // as absent, and absent means "import everything" -- so the one click that
          // clearly means "none of it" would otherwise do the most.
          disabled={importBundle.isPending || (inspection !== null && chosen.size === 0)}
          onClick={() => importBundle.mutate()}
        >
          {importBundle.isPending
            ? "Importing…"
            : inspection
              ? chosen.size === 0
                ? "Nothing selected"
                : `Import ${chosen.size} of ${SECTION_ORDER.filter((n) => (inspection.sections[n] ?? []).length).length}`
              : "Import everything"}
        </Button>
      </div>

      {inspection && (
        <div className="flex flex-col gap-2 rounded-md border border-border p-3">
          {inspection.compatibility_note && (
            <p
              className={
                inspection.compatibility === "newer"
                  ? "text-xs text-amber-700 dark:text-amber-400"
                  : "text-xs text-muted-foreground"
              }
              role={inspection.compatibility === "newer" ? "alert" : undefined}
            >
              {inspection.compatibility_note}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            From <code>{inspection.tenant_ref.slice(0, 8)}</code>
            {inspection.workflow_key ? ` · ${inspection.workflow_key}` : ""} · exported{" "}
            {inspection.exported_at.slice(0, 10)}
            {inspection.collisions > 0 && (
              <>
                {" · "}
                <span className="text-amber-600 dark:text-amber-400">
                  {inspection.collisions} already here
                </span>
              </>
            )}
          </p>
          {SECTION_ORDER.map((name) => {
            const items = inspection.sections[name] ?? [];
            if (items.length === 0) return null;
            const already = items.filter((i) => i.collides).length;
            return (
              <label key={name} className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={chosen.has(name)}
                  onChange={() => toggle(name)}
                />
                <span>
                  <span className="font-medium">{SECTION_LABEL[name] ?? name}</span>{" "}
                  <span className="text-xs text-muted-foreground">({items.length})</span>
                  {already > 0 && (
                    <span className="ml-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-700 dark:text-amber-400">
                      {already} already here — importing forks a copy
                    </span>
                  )}
                  <span className="block text-xs text-muted-foreground">
                    {items.map((i) => i.name || i.key).join(", ")}
                  </span>
                </span>
              </label>
            );
          })}
          {chosen.has("secrets") && !chosen.has("personas") && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              Secrets are held by personas. Without personas they are skipped rather than
              imported unreachable.
            </p>
          )}
        </div>
      )}

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
