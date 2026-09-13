import { useState } from "react";
import { ImportPanel, QuarantinePanel } from "./PortabilityPanels";
import { BackLink } from "@/components/BackLink";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { apiClient } from "@/lib/api-client/client";

type ExportMode = "participant" | "full" | "sanitised";

interface ModeOption {
  mode: ExportMode;
  label: string;
  requires: string;
  consequence: string;
}

/**
 * G4.7 (plan §11.4): the three export modes, with the choice made unmissable.
 *
 * `selected` starts as `null` and the submit button stays disabled until it isn't. That is
 * the whole design: §11.4 says "the UI must make the choice unmissable", and a
 * pre-selected default is exactly how a choice stops being made — the user clicks Export,
 * gets whatever the default was, and never reads the row explaining what they just put in
 * a file. Nothing here is a preference to remember; each export is its own decision about
 * who may read the result.
 *
 * Each option states its consequence in the same words for every mode — who it requires,
 * and what secret content ends up in the bundle. A consequence written only for the
 * dangerous option teaches users that unlabelled options are safe.
 */
const MODES: ModeOption[] = [
  {
    mode: "participant",
    label: "Participant — my session log",
    requires: "any workspace role",
    consequence:
      "Contains the secrets you personally hold, and no others. Everything withheld is listed as a redaction stub.",
  },
  {
    mode: "full",
    label: "Full — backup or migration",
    requires: "the secret:inspect permission (overseer)",
    consequence:
      "Contains every secret's full content. An audit entry naming you is written, exactly as if you had inspected each secret by hand.",
  },
  {
    mode: "sanitised",
    label: "Sanitised — safe to share",
    requires: "any workspace role",
    consequence:
      "Contains no secret content of any kind. Gists and redaction stubs show that secrets exist without revealing them.",
  },
];

const SECTIONS: { key: string; label: string; hint: string }[] = [
  { key: "personas", label: "Personas", hint: "cast + behavior profiles (no credentials)" },
  { key: "knowledge", label: "Knowledge", hint: "rulebooks, lore, entries + versions" },
  { key: "schemas", label: "Schemas", hint: "entity schemas + FSMs" },
  { key: "entities", label: "Entities", hint: "character sheets and records" },
  { key: "process", label: "Flows", hint: "process definitions" },
  { key: "secrets", label: "Secrets", hint: "per the visibility mode above" },
  { key: "vocabulary", label: "Vocabulary", hint: "the display overlay" },
  { key: "sessions", label: "Sessions", hint: "transcripts + resolution records" },
  {
    key: "connections",
    label: "Model connections + credentials",
    hint: "provider API keys travel INSIDE the bundle — forces password encryption",
  },
];

export function ExportDialog() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const [selected, setSelected] = useState<ExportMode | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [sections, setSections] = useState<Set<string>>(
    new Set(SECTIONS.map((s) => s.key).filter((k) => k !== "connections")),
  );
  const [encrypt, setEncrypt] = useState(false);
  const [password, setPassword] = useState("");
  // credentials in the bundle (or full-mode secret plaintext) make plaintext export
  // structurally unavailable -- the server refuses it too; the UI just says so first.
  const sensitive = sections.has("connections") || selected === "full";
  const mustEncrypt = sensitive;
  const effectiveEncrypt = encrypt || mustEncrypt;

  const requestExport = useMutation({
    mutationFn: async (mode: ExportMode) => {
      const { data, error } = await apiClient.POST("/export", {
        body: {
          workspace_id: workspaceId!,
          mode,
          sections: [...sections],
          password: effectiveEncrypt ? password : null,
        },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => setJobId(data?.job_id ?? null),
  });

  if (!workspaceId) return null;

  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <BackLink to={`/workspaces/${workspaceId}`} label="Back to the workspace" />
      <div>
        <h1 className="text-xl font-semibold">Export workspace</h1>
        <p className="text-sm text-muted-foreground">
          Choose what this bundle may contain. There is no default — the three modes put
          different things in the file, and which one is right depends on who will read it.
        </p>
      </div>

      <fieldset className="flex flex-col gap-2">
        <legend className="sr-only">Export mode</legend>
        {MODES.map((option) => (
          <label
            key={option.mode}
            className={`flex cursor-pointer gap-3 rounded-md border p-3 ${
              selected === option.mode ? "border-primary bg-secondary" : "border-border"
            }`}
          >
            <input
              type="radio"
              name="export-mode"
              aria-label={option.label}
              value={option.mode}
              checked={selected === option.mode}
              onChange={() => setSelected(option.mode)}
              className="mt-1"
            />
            <span className="flex flex-col gap-1">
              <span className="text-sm font-medium">{option.label}</span>
              <span className="text-xs text-muted-foreground">Requires: {option.requires}</span>
              <span className="text-xs">{option.consequence}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium">What to include</legend>
        {SECTIONS.map((section) => (
          <label key={section.key} className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={sections.has(section.key)}
              onChange={(e) => {
                const next = new Set(sections);
                if (e.target.checked) next.add(section.key);
                else next.delete(section.key);
                setSections(next);
              }}
            />
            <span>
              {section.label}
              <span className="ml-2 text-xs text-muted-foreground">{section.hint}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <fieldset className="flex flex-col gap-2 rounded-md border border-border p-3">
        <legend className="px-1 text-sm font-medium">Protection</legend>
        <label
          className={`flex items-center gap-2 text-sm ${mustEncrypt ? "opacity-50" : ""}`}
          title={
            mustEncrypt
              ? "Unavailable: this export carries credentials and/or full secret plaintext."
              : undefined
          }
        >
          <input
            type="radio"
            name="protection"
            checked={!effectiveEncrypt}
            disabled={mustEncrypt}
            onChange={() => setEncrypt(false)}
          />
          Plain file
          {mustEncrypt && (
            <span className="text-xs text-destructive">
              — unavailable for credentials / full secret plaintext
            </span>
          )}
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name="protection"
            checked={effectiveEncrypt}
            onChange={() => setEncrypt(true)}
          />
          Password-encrypted (AES-256-GCM)
        </label>
        {effectiveEncrypt && (
          <input
            type="password"
            placeholder="bundle password (min 8 characters)"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-72 rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          />
        )}
      </fieldset>

      <button
        type="button"
        disabled={
          selected === null ||
          requestExport.isPending ||
          sections.size === 0 ||
          (effectiveEncrypt && password.length < 8)
        }
        onClick={() => selected && requestExport.mutate(selected)}
        className="self-start rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
      >
        {selected === null ? "Choose a mode first" : "Start export"}
      </button>

      {requestExport.isError && (
        <p className="text-sm text-destructive">
          The export was refused. A full export needs the secret:inspect permission.
        </p>
      )}

      {jobId && (
        <p className="text-sm">
          Export queued.{" "}
          <a className="underline" href={`/api/export/${jobId}/download`}>
            Download when it finishes
          </a>
          .
        </p>
      )}

      <CardExportLossReport workspaceId={workspaceId} />
      <ImportPanel workspaceId={workspaceId!} />
      <QuarantinePanel />
    </div>
  );
}

interface LossItem {
  kind: string;
  detail: string;
  carried_in_extension: boolean;
}

/**
 * G4.9 (plan §11.3): the loss report, shown *before* download.
 *
 * A card cannot carry a behaviour profile, a secret, or a process definition, and handing
 * someone a card is a moment where a person forms a belief about what they just shared.
 * The report distinguishes "kept in extensions.pyrrhula, restorable by re-importing here"
 * from "gone entirely" — collapsing those into one "lossy!" warning would destroy the
 * distinction the user actually needs to decide.
 */
function CardExportLossReport({ workspaceId }: { workspaceId: string }) {
  const [agentId, setAgentId] = useState("");

  const preview = useQuery({
    queryKey: ["card-export-preview", workspaceId, agentId],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/export/cards/{persona_id}/preview", {
        params: { path: { persona_id: agentId }, query: { workspace_id: workspaceId } },
      });
      if (error) throw error;
      return data;
    },
    enabled: agentId.length > 0,
  });

  const items = (preview.data?.loss_report ?? []) as LossItem[];

  return (
    <section className="flex flex-col gap-2 rounded-md border border-border p-4">
      <h2 className="text-sm font-medium">Export an agent as a character card</h2>
      <p className="text-xs text-muted-foreground">
        Cards are a sharing format from another ecosystem. They can carry a persona and its
        lore; they cannot carry secrets, behaviour profiles, or processes. Check what would
        be lost before you export.
      </p>
      <input
        className="rounded-md border border-input bg-transparent px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
        placeholder="Agent id"
        value={agentId}
        onChange={(e) => setAgentId(e.target.value.trim())}
      />

      {preview.isError && (
        <p className="text-sm text-destructive">No such agent in this workspace.</p>
      )}

      {preview.data && (
        <div className="flex flex-col gap-2">
          <p className="text-sm">
            <strong>{preview.data.name}</strong> — {preview.data.entry_count} lore entr
            {preview.data.entry_count === 1 ? "y" : "ies"} would be written.
          </p>
          {items.length === 0 ? (
            <p className="text-sm">Nothing would be lost.</p>
          ) : (
            <ul className="flex flex-col gap-1 text-xs">
              {items.map((item) => (
                <li key={item.kind} className="flex flex-col">
                  <span className="font-medium">{item.kind}</span>
                  <span className="text-muted-foreground">{item.detail}</span>
                  <span
                    className={
                      item.carried_in_extension ? "text-muted-foreground" : "text-destructive"
                    }
                  >
                    {item.carried_in_extension
                      ? "Kept in extensions.pyrrhula — a Pyrrhula re-import restores it."
                      : "Not representable in a card at all."}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
