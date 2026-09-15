import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { apiClient } from "@/lib/api-client/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * An assistant for the platform-admin console.
 *
 * The admin console has no workspaces and so no personas, which left the one screen where
 * a mistake is deployment-wide as the one screen with no help on it.
 *
 * It proposes and never applies. A proposal arrives as a card with an Apply button, and
 * that click calls the ordinary admin endpoint from this browser session — so the
 * assistant holds no privilege of its own and everything it does lands in the same audit
 * trail as if the operator had clicked it themselves.
 */

type Proposal = { action: string; args: Record<string, unknown>; summary: string };
type Turn = { role: "user" | "assistant"; content: string; proposals?: Proposal[] };

async function applyProposal(p: Proposal): Promise<void> {
  if (p.action === "set_retrieval_models") {
    const { error } = await apiClient.PUT("/admin/retrieval-models", {
      body: p.args as never,
    });
    if (error) throw error;
    return;
  }
  if (p.action === "download_retrieval_models") {
    const { error } = await apiClient.POST("/admin/retrieval-models/download");
    if (error) throw error;
    return;
  }
  if (p.action === "add_plugin_repository") {
    const { error } = await apiClient.POST("/admin/plugin-repositories", {
      body: p.args as never,
    });
    if (error) throw error;
    return;
  }
  throw new Error(`no apply path for ${p.action}`);
}

export function AdminAssistantPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  async function send() {
    const question = draft.trim();
    if (!question || busy) return;
    setDraft("");
    const history = [...turns, { role: "user" as const, content: question }];
    setTurns(history);
    setBusy(true);

    let reply = "";
    let proposals: Proposal[] = [];
    try {
      const resp = await fetch("/api/admin/assistant/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          messages: history.map((t) => ({ role: t.role, content: t.content })),
        }),
      });
      if (!resp.ok || !resp.body) {
        const detail = await resp.json().catch(() => null);
        throw new Error(detail?.detail ?? `chat failed (${resp.status})`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      setTurns([...history, { role: "assistant", content: "" }]);
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (event.type === "text") reply += event.delta;
          if (event.type === "tool") reply += reply ? "" : "";
          if (event.type === "done") proposals = event.proposals ?? [];
          if (event.type === "error") throw new Error(event.detail);
          setTurns([...history, { role: "assistant", content: reply, proposals }]);
        }
      }
      setTurns([...history, { role: "assistant", content: reply, proposals }]);
    } catch (e) {
      toast.error(String((e as Error).message));
      setTurns([...history, { role: "assistant", content: `(failed: ${(e as Error).message})` }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Assistant</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Ask about this deployment, or ask for a change. Changes are{" "}
          <b>proposed, never applied</b> — you click Apply, and it runs as you. Its model
          is configured under <Link className="underline" to="/admin/models">Models</Link>.
        </p>
      </div>

      <div className="flex flex-col gap-3 rounded-md border border-border p-4">
        {turns.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Try: “are the retrieval models downloaded?”, “what plugin repositories are
            registered?”, “switch the embedding model to BAAI/bge-small-en-v1.5”.
          </p>
        ) : null}
        {turns.map((turn, i) => (
          <div key={i} className="flex flex-col gap-2">
            <div className="text-xs font-medium text-muted-foreground">
              {turn.role === "user" ? "You" : "Assistant"}
            </div>
            <div className="whitespace-pre-wrap text-sm">{turn.content}</div>
            {(turn.proposals ?? []).map((p, j) => (
              <ProposalCard key={j} proposal={p} />
            ))}
          </div>
        ))}
        <div className="flex gap-2 border-t border-border pt-3">
          <Input
            placeholder="Ask about this deployment…"
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <Button size="sm" disabled={busy || !draft.trim()} onClick={() => void send()}>
            {busy ? "…" : "Send"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function ProposalCard({ proposal }: { proposal: Proposal }) {
  const [state, setState] = useState<"pending" | "applied" | "failed">("pending");
  return (
    <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50/40 p-3 dark:bg-amber-950/20">
      <div className="text-xs font-medium">Proposed change</div>
      <code className="text-xs">{proposal.summary}</code>
      {state === "pending" ? (
        <div>
          <Button
            size="sm"
            variant="outline"
            onClick={async () => {
              try {
                await applyProposal(proposal);
                setState("applied");
                toast.success("Applied.");
              } catch (e) {
                setState("failed");
                toast.error(String((e as { detail?: string })?.detail ?? "Apply failed."));
              }
            }}
          >
            Apply
          </Button>
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">
          {state === "applied" ? "Applied." : "Failed — nothing changed."}
        </div>
      )}
    </div>
  );
}
