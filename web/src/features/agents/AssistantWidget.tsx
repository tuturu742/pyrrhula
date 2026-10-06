import { useEffect, useRef, useState } from "react";
import { useMatch } from "react-router-dom";
import { useAuthStore } from "@/stores/auth";
import { Markdown } from "@/components/Markdown";
import { useSoleWorkspaceId } from "./assist";
import { applyAssistantAction } from "./assistant-actions";

interface Proposal {
  id: string;
  action: string;
  args: Record<string, unknown>;
  summary: string;
  status: "pending" | "applied" | "failed" | "dismissed";
  detail?: string;
}

interface ChatItem {
  role: "user" | "assistant" | "system";
  content: string;
  proposals?: Proposal[];
}

/**
 * The support-style assistant chat: a floating launcher on every page. History lives in
 * sessionStorage (per workspace) and travels with each request; the reply streams in as
 * NDJSON. Edit proposals arrive as cards — Apply runs the ordinary API call from THIS
 * browser session, so the assistant can do exactly what the user could, nothing more.
 */
/** Secret plaintext a proposal carries is for the Apply call, not for the screen. */
const HIDDEN_ARGS = new Set(["content", "hint_text", "behavioral_directive"]);
function maskSecretArgs(args: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(args).map(([k, v]) => [k, HIDDEN_ARGS.has(k) && v ? "(hidden)" : v]),
  );
}

export function AssistantWidget() {
  const routeWorkspace = useMatch("/workspaces/:workspaceId/*")?.params.workspaceId;
  const soleWorkspace = useSoleWorkspaceId();
  const workspaceId = routeWorkspace ?? soleWorkspace;

  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const storageKey = `assistant-chat-${workspaceId ?? "none"}`;

  useEffect(() => {
    if (!workspaceId) return;
    try {
      const raw = sessionStorage.getItem(storageKey);
      setItems(raw ? (JSON.parse(raw) as ChatItem[]) : []);
    } catch {
      setItems([]);
    }
  }, [storageKey, workspaceId]);

  useEffect(() => {
    if (workspaceId) sessionStorage.setItem(storageKey, JSON.stringify(items));
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [items, storageKey, workspaceId]);

  async function send() {
    const question = input.trim();
    if (!question || busy || !workspaceId) return;
    setInput("");
    setBusy(true);
    const history = [...items, { role: "user" as const, content: question }];
    setItems([...history, { role: "assistant", content: "" }]);

    try {
      const token = useAuthStore.getState().token;
      const resp = await fetch(`/api/workspaces/${workspaceId}/assistant-chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          messages: history.map(({ role, content }) => ({ role, content })),
        }),
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.trim()) continue;
          const event = JSON.parse(line) as Record<string, unknown>;
          setItems((prev) => {
            const next = [...prev];
            const last = { ...next[next.length - 1] } as ChatItem;
            if (event.type === "text") {
              last.content += String(event.delta ?? "");
            } else if (event.type === "proposal") {
              last.proposals = [
                ...(last.proposals ?? []),
                {
                  id: `${Date.now()}-${(last.proposals?.length ?? 0) + 1}`,
                  action: String(event.action),
                  args: (event.args ?? {}) as Record<string, unknown>,
                  summary: String(event.summary ?? ""),
                  status: "pending",
                },
              ];
            } else if (event.type === "error") {
              last.content =
                (last.content ? `${last.content}\n\n` : "") +
                `⚠️ ${String(event.detail ?? "assistant error")}`;
            }
            next[next.length - 1] = last;
            return next;
          });
        }
      }
    } catch (exc) {
      setItems((prev) => {
        const next = [...prev];
        const last = { ...next[next.length - 1] } as ChatItem;
        last.content =
          (last.content ? `${last.content}\n\n` : "") +
          `⚠️ request failed (${exc instanceof Error ? exc.message : "network error"})`;
        next[next.length - 1] = last;
        return next;
      });
    } finally {
      setBusy(false);
    }
  }

  async function applyProposal(itemIndex: number, proposal: Proposal) {
    if (!workspaceId) return;
    const res = await applyAssistantAction(proposal.action, proposal.args, workspaceId);
    setItems((prev) => {
      const next = [...prev];
      const item = { ...next[itemIndex] } as ChatItem;
      item.proposals = (item.proposals ?? []).map((p) =>
        p.id === proposal.id
          ? { ...p, status: res.ok ? "applied" : "failed", detail: res.detail }
          : p,
      );
      next[itemIndex] = item;
      // The model should know the outcome on the next turn.
      return [
        ...next,
        {
          role: "system",
          // Worded so the model cannot read it as the proposal notice again: the user
          // has acted, and this is the outcome.
          content: res.ok
            ? `[The user clicked Apply on ${proposal.action}; it was applied: ${res.detail}]`
            : `[The user clicked Apply on ${proposal.action}; it failed: ${res.detail}]`,
        },
      ];
    });
  }

  function dismissProposal(itemIndex: number, proposal: Proposal) {
    setItems((prev) => {
      const next = [...prev];
      const item = { ...next[itemIndex] } as ChatItem;
      item.proposals = (item.proposals ?? []).map((p) =>
        p.id === proposal.id ? { ...p, status: "dismissed" } : p,
      );
      next[itemIndex] = item;
      return next;
    });
  }

  if (!workspaceId) return null;

  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col items-end gap-3">
      {open && (
        <div className="flex h-[32rem] max-h-[80vh] w-96 max-w-[90vw] flex-col overflow-hidden rounded-lg border border-border bg-background shadow-xl">
          <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
            <div>
              <div className="text-sm font-semibold">Assistant</div>
              <div className="text-xs text-muted-foreground">
                Ask about this workspace, or ask it to change things — edits need your
                confirmation.
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button
                type="button"
                title="Clear conversation"
                className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-secondary"
                onClick={() => setItems([])}
              >
                Clear
              </button>
              <button
                type="button"
                className="rounded px-2 py-1 text-sm text-muted-foreground hover:bg-secondary"
                onClick={() => setOpen(false)}
              >
                ✕
              </button>
            </div>
          </div>

          <div ref={scrollRef} className="flex-1 overflow-y-auto p-3">
            {items.length === 0 && (
              <p className="p-2 text-sm text-muted-foreground">
                e.g. “What do our repos collectively do?”, “Rename the latest session to
                Kickoff”, “Make Dev1's persona more formal”.
              </p>
            )}
            <div className="flex flex-col gap-2">
              {items.map((item, i) =>
                item.role === "system" ? (
                  <div key={i} className="text-center text-xs text-muted-foreground">
                    {item.content}
                  </div>
                ) : (
                  <div
                    key={i}
                    className={`max-w-[90%] rounded-lg px-3 py-2 text-sm ${
                      item.role === "user"
                        ? "self-end bg-primary text-primary-foreground"
                        : "self-start bg-secondary"
                    }`}
                  >
                    {item.role === "user" ? (
                      <p className="whitespace-pre-wrap">{item.content}</p>
                    ) : item.content ? (
                      <Markdown text={item.content} />
                    ) : busy && i === items.length - 1 ? (
                      <p>…</p>
                    ) : null}
                    {(item.proposals ?? []).map((p) => (
                      <div
                        key={p.id}
                        className="mt-2 rounded-md border border-border bg-background p-2"
                      >
                        <div className="text-xs font-medium">
                          Proposed: {p.action.replaceAll("_", " ")}
                        </div>
                        <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-all text-[11px] text-muted-foreground">
                          {JSON.stringify(maskSecretArgs(p.args), null, 1)}
                        </pre>
                        {p.status === "pending" ? (
                          <div className="mt-1.5 flex gap-2">
                            <button
                              type="button"
                              onClick={() => void applyProposal(i, p)}
                              className="rounded bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground"
                            >
                              Apply
                            </button>
                            <button
                              type="button"
                              onClick={() => dismissProposal(i, p)}
                              className="rounded border border-border px-2.5 py-1 text-xs"
                            >
                              Dismiss
                            </button>
                          </div>
                        ) : (
                          <div
                            className={`mt-1 text-xs ${
                              p.status === "applied"
                                ? "text-green-600"
                                : p.status === "failed"
                                  ? "text-destructive"
                                  : "text-muted-foreground"
                            }`}
                          >
                            {p.status}
                            {p.detail ? ` — ${p.detail}` : ""}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                ),
              )}
            </div>
          </div>

          <form
            className="flex items-end gap-2 border-t border-border p-2.5"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <textarea
              className="max-h-24 min-h-9 flex-1 resize-none rounded-md border border-input bg-transparent px-2.5 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={input}
              rows={1}
              placeholder="Ask the assistant…"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <button
              type="submit"
              disabled={busy || !input.trim()}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
            >
              {busy ? "…" : "Send"}
            </button>
          </form>
        </div>
      )}

      <button
        type="button"
        title="Workspace assistant"
        onClick={() => setOpen((v) => !v)}
        className="flex h-12 w-12 items-center justify-center rounded-full bg-primary text-xl text-primary-foreground shadow-lg hover:opacity-90"
      >
        {open ? "✕" : "💬"}
      </button>
    </div>
  );
}
