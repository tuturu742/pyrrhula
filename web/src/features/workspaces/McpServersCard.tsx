import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmButton } from "@/components/ConfirmButton";

/** Workspace MCP servers: what your agents can reach beyond the platform. The
 * owner-permissioned PUT/DELETE existed with no UI — registration was admin-console
 * or curl only. `credential_ref` names an ENV VAR on the server holding the bearer
 * token; keys are never stored in this table. */
export function McpServersCard({ workspaceId }: { workspaceId: string }) {
  const queryClient = useQueryClient();
  const [key, setKey] = useState("");
  const [url, setUrl] = useState("");
  const [tools, setTools] = useState("");
  // Per-session call ceiling; blank = unlimited. An external server cannot enforce one
  // itself -- it is never told which session is calling -- so the platform holds it.
  const [cap, setCap] = useState("");
  // Seconds to wait on this server, blank = platform default. A lookup tool should fail
  // fast; a build tool should not be cut off at a lookup tool's patience.
  const [timeout, setTimeout_] = useState("");

  const servers = useQuery({
    queryKey: ["mcp-servers", workspaceId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/mcp-servers", {
        params: { query: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
  });

  const register = useMutation({
    mutationFn: async () => {
      const { error } = await apiClient.PUT("/mcp-servers", {
        body: {
          workspace_id: workspaceId,
          key: key.trim(),
          url: url.trim(),
          enabled_tools: tools
            .split(",")
            .map((t) => t.trim())
            .filter(Boolean),
          effectful_tools: [],
          require_confirmation: true,
          max_calls_per_session: cap.trim() ? Number(cap.trim()) : null,
          timeout_seconds: timeout.trim() ? Number(timeout.trim()) : null,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success("Server registered — its allowed tools join agent turns here.");
      setKey("");
      setUrl("");
      setTools("");
      setCap("");
      setTimeout_("");
      void queryClient.invalidateQueries({ queryKey: ["mcp-servers", workspaceId] });
    },
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Registration failed.")),
  });

  const remove = useMutation({
    mutationFn: async (serverKey: string) => {
      const { error } = await apiClient.DELETE("/mcp-servers/{key}", {
        params: { path: { key: serverKey }, query: { workspace_id: workspaceId } },
      });
      if (error) throw error;
    },
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["mcp-servers", workspaceId] }),
    onError: () => toast.error("Could not remove the server."),
  });

  const rows = servers.data ?? [];
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-4">
      <h2 className="text-sm font-medium">MCP servers</h2>
      <p className="text-xs text-muted-foreground">
        Tools your agents may call during sessions here. Register any
        streamable-HTTP MCP endpoint and allowlist its tools — agents can never reach
        tools that aren&apos;t listed.
      </p>
      {servers.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      <ul className="flex flex-col gap-1.5">
        {rows.map((server) => (
          <li
            key={server.key}
            className="flex items-center justify-between gap-2 text-sm"
          >
            <span>
              <span className="font-medium">{server.key}</span>
              <span className="ml-2 text-xs text-muted-foreground">{server.url}</span>
              <span className="block text-xs text-muted-foreground">
                tools: {server.enabled_tools.join(", ") || "none"}
                {server.max_calls_per_session != null
                  ? ` · ${server.max_calls_per_session} calls per session`
                  : ""}
                {server.timeout_seconds != null ? ` · ${server.timeout_seconds}s timeout` : ""}
              </span>
            </span>
            <ConfirmButton
              title="Remove MCP server"
              description={`Remove "${server.key}"? Agents lose its tools on their next turn. Re-register any time.`}
              confirmLabel="Remove"
              destructive
              onConfirm={() => remove.mutate(server.key)}
            >
              <Button variant="ghost" size="sm" className="text-destructive">
                Remove
              </Button>
            </ConfirmButton>
          </li>
        ))}
      </ul>
      <form
        className="flex flex-wrap items-center gap-2 border-t border-border pt-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (key.trim() && url.trim()) register.mutate();
        }}
      >
        <Input
          placeholder="key (e.g. engine)"
          className="w-36"
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
        <Input
          placeholder="https://host:port/mcp"
          className="w-64"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <Input
          placeholder="allowed tools, comma-separated"
          className="w-64"
          value={tools}
          onChange={(e) => setTools(e.target.value)}
        />
        <Input
          placeholder="calls/session (blank = ∞)"
          className="w-40"
          inputMode="numeric"
          value={cap}
          onChange={(e) => setCap(e.target.value.replace(/[^0-9]/g, ""))}
        />
        <Input
          placeholder="timeout s (blank = 120)"
          className="w-40"
          inputMode="numeric"
          value={timeout}
          onChange={(e) => setTimeout_(e.target.value.replace(/[^0-9]/g, ""))}
        />
        <Button
          type="submit"
          variant="outline"
          disabled={!key.trim() || !url.trim() || register.isPending}
        >
          Register
        </Button>
      </form>
    </div>
  );
}
