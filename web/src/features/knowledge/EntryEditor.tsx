import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client/client";
import type { components } from "@/lib/api-client/schema";
import { useSoleWorkspaceId } from "@/features/agents/assist";
import { DraftWithAssistant } from "@/features/agents/DraftWithAssistant";
import { EditProposalPanel } from "./EditProposalPanel";

type EntryResponse = components["schemas"]["EntryResponse"];

interface EntryEditorProps {
  sourceId: string;
  /** Present when editing an existing draft entry; absent when creating a new one. */
  existingEntry?: EntryResponse;
  onSaved?: (entry: EntryResponse) => void;
  onCancel?: () => void;
}

/**
 * D1.1: the entry editor — markdown body, title, class, scope, and every activation
 * field from plan §6.4 (keys/secondary_keys/logic/regex/constant/sticky/cooldown/delay/
 * trigger_pct/inclusion_group/position), with inline help text under each. Handles both
 * create (no `existingEntry`) and edit (round-trips every field via EntryResponse, which
 * D1.1 extended server-side to actually carry them — see packages/api/routes/knowledge.py).
 *
 * A fork-on-edit response (`forked_source_id` set) means this write landed in the
 * caller's own copy of a library source, not the library original — `onSaved` receives
 * the full response so the parent page can redirect to the fork.
 */
export function EntryEditor({ sourceId, existingEntry, onSaved, onCancel }: EntryEditorProps) {
  const queryClient = useQueryClient();
  const isEditing = existingEntry !== undefined;
  const soleWorkspaceId = useSoleWorkspaceId();

  const [entryKey, setEntryKey] = useState(existingEntry?.entry_key ?? "");
  const [title, setTitle] = useState(existingEntry?.title ?? "");
  const [bodyMd, setBodyMd] = useState(existingEntry?.body_md ?? "");
  const [entryClass, setEntryClass] = useState(existingEntry?.class ?? "rules");
  const [scopeKey, setScopeKey] = useState(existingEntry?.scope_key ?? "workspace_public");
  const [keys, setKeys] = useState(existingEntry?.keys.join(", ") ?? "");
  const [secondaryKeys, setSecondaryKeys] = useState(
    existingEntry?.secondary_keys.join(", ") ?? "",
  );
  const [logic, setLogic] = useState(existingEntry?.logic ?? "AND");
  const [useRegex, setUseRegex] = useState(existingEntry?.use_regex ?? false);
  const [constant, setConstant] = useState(existingEntry?.constant ?? false);
  const [sticky, setSticky] = useState(existingEntry?.sticky?.toString() ?? "");
  const [cooldown, setCooldown] = useState(existingEntry?.cooldown?.toString() ?? "");
  const [delay, setDelay] = useState(existingEntry?.delay?.toString() ?? "");
  const [triggerPct, setTriggerPct] = useState(existingEntry?.trigger_pct?.toString() ?? "");
  const [inclusionGroup, setInclusionGroup] = useState(existingEntry?.inclusion_group ?? "");
  const [position, setPosition] = useState(existingEntry?.position ?? "before_char");

  const save = useMutation({
    mutationFn: async () => {
      const { data, error } = await apiClient.PUT(
        "/knowledge/sources/{source_id}/entries/{entry_key}",
        {
          params: { path: { source_id: sourceId, entry_key: entryKey } },
          body: {
            title,
            body_md: bodyMd,
            class: entryClass,
            scope_key: scopeKey,
            keys: keys
              .split(",")
              .map((k) => k.trim())
              .filter(Boolean),
            secondary_keys: secondaryKeys
              .split(",")
              .map((k) => k.trim())
              .filter(Boolean),
            logic,
            use_regex: useRegex,
            constant,
            sticky: sticky === "" ? null : Number(sticky),
            cooldown: cooldown === "" ? null : Number(cooldown),
            delay: delay === "" ? null : Number(delay),
            trigger_pct: triggerPct === "" ? null : Number(triggerPct),
            inclusion_group: inclusionGroup === "" ? null : inclusionGroup,
            position,
            insertion_order: existingEntry?.insertion_order ?? 0,
          },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (entry) => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge-entries", sourceId] });
      if (entry) onSaved?.(entry);
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!entryKey.trim() || !title.trim()) return;
    save.mutate();
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-4 rounded-md border border-border p-4"
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Entry key (stable identifier)" hint="Unique within this source's draft.">
          <input
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={entryKey}
            onChange={(e) => setEntryKey(e.target.value)}
            disabled={isEditing}
            required
          />
        </Field>
        <Field label="Title">
          <input
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
          />
        </Field>
      </div>

      <Field label="Body (markdown)">
        <textarea
          className="min-h-32 w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          value={bodyMd}
          onChange={(e) => setBodyMd(e.target.value)}
        />
        <DraftWithAssistant
          workspaceId={soleWorkspaceId}
          task="draft_knowledge"
          subject={title || entryKey}
          onDraft={setBodyMd}
        />
      </Field>

      <div className="grid grid-cols-2 gap-3">
        <Field label="Class">
          <select
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={entryClass}
            onChange={(e) => setEntryClass(e.target.value)}
          >
            <option value="rules">rules</option>
            <option value="lore">lore</option>
            <option value="misc">misc</option>
          </select>
        </Field>
        <Field label="Scope" hint="Which visibility compartment can read this entry.">
          <input
            className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={scopeKey}
            onChange={(e) => setScopeKey(e.target.value)}
            required
          />
        </Field>
      </div>

      <fieldset className="flex flex-col gap-3 rounded-md border border-dashed border-border p-3">
        <legend className="px-1 text-sm font-medium text-muted-foreground">
          Activation (plan §6.4)
        </legend>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Keys" hint="Comma-separated. Any/all (per Logic) must appear in the scan text.">
            <input
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={keys}
              onChange={(e) => setKeys(e.target.value)}
            />
          </Field>
          <Field label="Secondary keys" hint="Comma-separated, combined with Logic.">
            <input
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={secondaryKeys}
              onChange={(e) => setSecondaryKeys(e.target.value)}
            />
          </Field>
        </div>

        <div className="grid grid-cols-3 gap-3">
          <Field label="Logic">
            <select
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={logic}
              onChange={(e) => setLogic(e.target.value)}
            >
              <option value="AND">AND</option>
              <option value="OR">OR</option>
              <option value="NOT">NOT</option>
            </select>
          </Field>
          <Checkbox
            label="Use regex"
            hint="Keys are regex patterns, not literal text."
            checked={useRegex}
            onChange={setUseRegex}
          />
          <Checkbox
            label="Constant"
            hint="Always active regardless of keys — consumes budget first."
            checked={constant}
            onChange={setConstant}
          />
        </div>

        <div className="grid grid-cols-4 gap-3">
          <Field label="Sticky" hint="Turns to stay active once fired.">
            <input
              type="number"
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={sticky}
              onChange={(e) => setSticky(e.target.value)}
            />
          </Field>
          <Field label="Cooldown" hint="Turns before it can refire.">
            <input
              type="number"
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={cooldown}
              onChange={(e) => setCooldown(e.target.value)}
            />
          </Field>
          <Field label="Delay" hint="Won't fire before this turn number.">
            <input
              type="number"
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={delay}
              onChange={(e) => setDelay(e.target.value)}
            />
          </Field>
          <Field label="Trigger %" hint="Probabilistic activation, 0-100.">
            <input
              type="number"
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={triggerPct}
              onChange={(e) => setTriggerPct(e.target.value)}
            />
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Inclusion group" hint="Only one entry per group activates.">
            <input
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={inclusionGroup}
              onChange={(e) => setInclusionGroup(e.target.value)}
            />
          </Field>
          <Field label="Position" hint="Where this entry lands relative to other content.">
            <select
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={position}
              onChange={(e) => setPosition(e.target.value)}
            >
              <option value="before_char">before_char</option>
              <option value="after_char">after_char</option>
              <option value="at_depth_0">at_depth_0</option>
            </select>
          </Field>
        </div>
      </fieldset>

      {save.error !== null && <p className="text-sm text-destructive">Failed to save entry.</p>}

      {isEditing && (
        <EditProposalPanel
          sourceId={sourceId}
          entryKey={entryKey}
          onApplied={(newBodyMd) => setBodyMd(newBodyMd)}
        />
      )}

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={save.isPending}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:bg-primary/90"
        >
          {isEditing ? "Save changes" : "Create entry"}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-border px-4 py-2 text-sm hover:bg-accent disabled:opacity-50"
          >
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </label>
  );
}

function Checkbox({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="flex items-center gap-2 text-muted-foreground">
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        {label}
      </span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </label>
  );
}
