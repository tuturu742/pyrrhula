import { useState } from "react";
import { useAssist } from "./assist";

interface DraftWithAssistantProps {
  workspaceId: string | undefined;
  task: "draft_persona" | "draft_knowledge";
  /** What the draft is about (persona name, entry title, …). */
  subject: string;
  onDraft: (text: string) => void;
}

/**
 * "✨ Draft with assistant": a one-line hint input + button that asks the workspace
 * assistant for a grounded draft and hands the text back to the surrounding editor.
 * The human stays the author — the draft lands in the editable field, never saves
 * itself.
 */
export function DraftWithAssistant({
  workspaceId,
  task,
  subject,
  onDraft,
}: DraftWithAssistantProps) {
  const [hint, setHint] = useState("");
  const assist = useAssist(workspaceId);

  if (!workspaceId) return null;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <input
          className="flex-1 rounded-md border border-input bg-transparent px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          placeholder="Tell the assistant what you want (tone, focus, must-haves)…"
        />
        <button
          type="button"
          disabled={assist.isPending}
          onClick={() =>
            assist.mutate(
              { task, subject, instruction: hint || `Draft this for: ${subject}` },
              { onSuccess: (r) => onDraft(r.text) },
            )
          }
          className="shrink-0 rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
          title="Ask the workspace assistant for a draft grounded in this workspace's knowledge"
        >
          {assist.isPending ? "Drafting…" : "✨ Draft with assistant"}
        </button>
      </div>
      {assist.error !== null && (
        <p className="text-xs text-destructive">Assistant call failed — try again.</p>
      )}
      {assist.data && assist.data.context_entry_keys.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Grounded in: {assist.data.context_entry_keys.slice(0, 6).join(", ")}
        </p>
      )}
    </div>
  );
}
