import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import type { components } from "@/lib/api-client/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmButton } from "@/components/ConfirmButton";

/** Workspace MCP servers: what your agents can reach beyond the platform. The
 * owner-permissioned PUT/DELETE existed with no UI — registration was admin-console
 * or curl only. `credential_ref` names an ENV VAR on the server holding the bearer
 * token; keys are never stored in this table. */
type McpServer = components["schemas"]["McpServerResponse"];

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
  // Transport-specific knobs, as JSON -- a SearXNG engine list, say. Each transport
  // reads only its own keys (docs/mcp.md).
  const [options, setOptions] = useState("");
  // The row being edited, if any. PUT replaces a key's whole configuration, so a save
  // carries the fields this form does not show (effectful tools, confirmation, the
  // credential ref, the result cap) from the original row rather than resetting them.
  const [editing, setEditing] = useState<McpServer | null>(null);

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
      let parsedOptions: Record<string, unknown> = {};
      if (options.trim()) {
        try {
          parsedOptions = JSON.parse(options) as Record<string, unknown>;
        } catch {
          throw { detail: "options must be a JSON object, e.g. {\"engines\": \"bing news\"}" };
        }
      }
      const { error } = await apiClient.PUT("/mcp-servers", {
        body: {
          workspace_id: workspaceId,
          key: key.trim(),
          url: url.trim(),
          enabled_tools: tools
            .split(",")
            .map((t) => t.trim())
            .filter(Boolean),
          effectful_tools: editing?.effectful_tools ?? [],
          require_confirmation: editing?.require_confirmation ?? true,
          credential_ref: editing?.credential_ref ?? null,
          max_result_chars: editing?.max_result_chars ?? null,
          max_calls_per_session: cap.trim() ? Number(cap.trim()) : null,
          timeout_seconds: timeout.trim() ? Number(timeout.trim()) : null,
          options: parsedOptions,
        },
      });
      if (error) throw error;
    },
    onSuccess: () => {
      toast.success(
        editing
          ? "Server updated — agents see the new settings on their next turn."
          : "Server registered — its allowed tools join agent turns here.",
      );
      clearForm();
      void queryClient.invalidateQueries({ queryKey: ["mcp-servers", workspaceId] });
    },
    onError: (e) =>
      toast.error(String((e as { detail?: string })?.detail ?? "Registration failed.")),
  });

  function clearForm() {
    setEditing(null);
    setKey("");
    setUrl("");
    setTools("");
    setCap("");
    setTimeout_("");
    setOptions("");
  }

  function startEditing(server: McpServer) {
    setEditing(server);
    setKey(server.key);
    setUrl(server.url);
    setTools(server.enabled_tools.join(", "));
    setCap(server.max_calls_per_session != null ? String(server.max_calls_per_session) : "");
    setTimeout_(server.timeout_seconds != null ? String(server.timeout_seconds) : "");
    setOptions(
      Object.keys(server.options ?? {}).length > 0 ? JSON.stringify(server.options) : "",
    );
  }

  // Ask the server what it offers -- the discovery a turn runs, reported to the person
  // instead of swallowed. A turn treats an unreachable server as "no tools this turn"
  // and carries on; the only symptom was a persona explaining why its tool had gone.
  const [testResults, setTestResults] = useState<
    Record<string, { ok: boolean; detail: string; tools: string[] }>
  >({});
  const test = useMutation({
    mutationFn: async (serverKey: string) => {
      const { data, error } = await apiClient.POST("/mcp-servers/{key}/test", {
        params: { path: { key: serverKey }, query: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return { serverKey, result: data };
    },
    onSuccess: ({ serverKey, result }) =>
      setTestResults((prev) => ({ ...prev, [serverKey]: result })),
    onError: () => toast.error("Could not reach the test endpoint."),
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
        tools that aren&apos;t listed. Registering a key again replaces its settings.
        Press <b>Test</b> after registering: the address must route from where the api
        runs, which a server on your own machine may not.
      </p>
      {servers.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      <ul className="flex flex-col gap-1.5">
        {rows.map((server) => (
          <li
            key={server.key}
            className="flex items-center justify-between gap-2 text-sm"
          >
            <span className="min-w-0">
              <span className="font-medium">{server.key}</span>
              <span className="ml-2 text-xs text-muted-foreground">{server.url}</span>
              <span className="block text-xs text-muted-foreground">
                tools: {server.enabled_tools.join(", ") || "none"}
                {server.max_calls_per_session != null
                  ? ` · ${server.max_calls_per_session} calls per session`
                  : ""}
                {server.timeout_seconds != null ? ` · ${server.timeout_seconds}s timeout` : ""}
                {Object.keys(server.options ?? {}).length > 0
                  ? ` · options ${JSON.stringify(server.options)}`
                  : ""}
              </span>
              {testResults[server.key] && (
                <span
                  className={`block text-xs ${
                    testResults[server.key].ok ? "text-green-600" : "text-destructive"
                  }`}
                >
                  {testResults[server.key].ok ? "✓ " : "✗ "}
                  {testResults[server.key].detail}
                  {testResults[server.key].tools.length > 0
                    ? ` — offers: ${testResults[server.key].tools.join(", ")}`
                    : ""}
                </span>
              )}
            </span>
            <span className="flex shrink-0 items-center gap-1">
              <Button
                variant="outline"
                size="sm"
                title="Load this server into the form below; saving replaces its settings"
                onClick={() => startEditing(server)}
              >
                Edit
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={test.isPending && test.variables === server.key}
                title="Ask the server which tools it offers, from where the api runs"
                onClick={() => test.mutate(server.key)}
              >
                {test.isPending && test.variables === server.key ? "Testing…" : "Test"}
              </Button>
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
            </span>
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
        {editing && (
          <span className="w-full text-xs text-muted-foreground">
            Editing <b>{editing.key}</b> — Save replaces its settings.
          </span>
        )}
        <Input
          placeholder="key (e.g. engine)"
          className="w-36"
          value={key}
          // The key is the identity PUT replaces by; changing it here would register a
          // second server and leave the first as it was.
          readOnly={editing !== null}
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
        <Input
          placeholder='options JSON, e.g. {"engines": "bing news"}'
          className="w-72 font-mono text-xs"
          value={options}
          onChange={(e) => setOptions(e.target.value)}
        />
        <Button
          type="submit"
          variant="outline"
          disabled={!key.trim() || !url.trim() || register.isPending}
        >
          {editing ? "Save" : "Register"}
        </Button>
        {editing && (
          <Button type="button" variant="ghost" size="sm" onClick={clearForm}>
            Cancel
          </Button>
        )}
      </form>
    </div>
  );
}
