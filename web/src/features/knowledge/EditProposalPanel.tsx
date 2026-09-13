import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";

interface EditProposalPanelProps {
  sourceId: string;
  entryKey: string;
  onApplied?: (newBodyMd: string) => void;
}

/**
 * F3.12: chat-based editing for one knowledge entry -- draft-and-approve, the same
 * discipline E2.2's secret `DraftAssist` established (propose, never autopilot).
 * Declining is simply never calling apply -- the version DAG stays untouched, so there's
 * no "cancel" request to make, just local state to clear.
 */
export function EditProposalPanel({ sourceId, entryKey, onApplied }: EditProposalPanelProps) {
  const queryClient = useQueryClient();
  const [modelProfileId, setModelProfileId] = useState("");
  const [instruction, setInstruction] = useState("");
  const [proposal, setProposal] = useState<{
    proposedBodyMd: string;
    textDiff: string;
    valid: boolean;
    issues: string[];
  } | null>(null);

  const { data: modelProfiles } = useQuery({
    queryKey: ["model-profiles"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/model-profiles");
      if (error) throw error;
      return data;
    },
  });

  const propose = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.POST(
        "/knowledge/sources/{source_id}/entries/{entry_key}/propose-edit",
        {
          params: { path: { source_id: sourceId, entry_key: entryKey } },
          body: { agent_id: modelProfileId, instruction },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (result) => {
      if (!result) return;
      setProposal({
        proposedBodyMd: result.proposed_body_md,
        textDiff: result.text_diff,
        valid: result.valid,
        issues: result.issues,
      });
    },
  });

  const apply = useMutation({
    mutationFn: async () => {
      if (!proposal) throw new Error("no proposal to apply");
      const { data, error } = await apiClient.POST(
        "/knowledge/sources/{source_id}/entries/{entry_key}/apply-edit",
        {
          params: { path: { source_id: sourceId, entry_key: entryKey } },
          body: { proposed_body_md: proposal.proposedBodyMd },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      const newBodyMd = proposal?.proposedBodyMd;
      setProposal(null);
      setInstruction("");
      void queryClient.invalidateQueries({ queryKey: ["knowledge-versions", sourceId] });
      void queryClient.invalidateQueries({ queryKey: ["knowledge-entries", sourceId] });
      if (newBodyMd !== undefined) onApplied?.(newBodyMd);
    },
  });

  return (
    <div className="flex flex-col gap-3 rounded-md border border-dashed border-border p-4">
      <div className="text-sm font-medium">Propose an AI edit</div>
      <div className="flex gap-2">
        <select
          className="rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={modelProfileId}
          onChange={(e) => setModelProfileId(e.target.value)}
        >
          <option value="">Model profile…</option>
          {modelProfiles?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <input
          className="flex-1 rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="e.g. shorten this to two sentences"
        />
        <button
          type="button"
          disabled={!modelProfileId || !instruction.trim() || propose.isPending}
          onClick={() => propose.mutate()}
          className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50"
        >
          {propose.isPending ? "Drafting…" : "Propose edit"}
        </button>
      </div>

      {propose.error !== null && (
        <p className="text-sm text-destructive">Failed to draft a proposal.</p>
      )}

      {proposal && (
        <div className="flex flex-col gap-2 rounded-md bg-secondary p-3">
          {!proposal.valid && (
            <ul className="text-sm text-destructive">
              {proposal.issues.map((issue, i) => (
                <li key={i}>{issue}</li>
              ))}
            </ul>
          )}
          <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-xs">
            {proposal.textDiff || "(no textual difference)"}
          </pre>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={!proposal.valid || apply.isPending}
              onClick={() => apply.mutate()}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              {apply.isPending ? "Publishing…" : "Accept -- publish new version"}
            </button>
            <button
              type="button"
              onClick={() => setProposal(null)}
              className="rounded-md border border-border px-3 py-1.5 text-sm"
            >
              Discard
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
