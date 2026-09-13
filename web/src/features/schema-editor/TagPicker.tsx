import { SEMANTIC_TAGS, type SemanticTag } from "@/features/entity-sheets/tagWidgetRegistry";
import type { FieldType, SchemaValidationIssue } from "./types";
import { CelEditor } from "./CelEditor";

interface TagPickerProps {
  fieldPath: string;
  tags: string[];
  tagMetadata: Record<string, unknown>;
  fieldType: FieldType;
  allFieldKeys: string[];
  issues: SchemaValidationIssue[];
  onChange: (tags: string[], tagMetadata: Record<string, unknown>) => void;
}

function issuesFor(issues: SchemaValidationIssue[], path: string): string[] {
  return issues.filter((i) => i.field_path === path || i.field_path.startsWith(`${path}.`)).map((i) => i.message);
}

/**
 * F3.11's tag picker: checkboxes constrained to `core.entities.tags.SEMANTIC_TAGS` --
 * the same closed, fixed nine this repo's own render registry uses, imported rather
 * than re-declared a third time. A checked tag that carries a `tag_metadata` contract
 * (`resource`'s `max_ref`/`low_threshold`, `modifier_source`'s `modifier_formula`,
 * `progression`'s `curve_ref`) grows its own inline metadata form -- the same fields
 * `core.entities.tags._validate_field_tags` requires, so what's missing here is exactly
 * what would fail save-time validation.
 */
export function TagPicker({
  fieldPath,
  tags,
  tagMetadata,
  fieldType,
  allFieldKeys,
  issues,
  onChange,
}: TagPickerProps) {
  function toggleTag(tag: SemanticTag) {
    const next = tags.includes(tag) ? tags.filter((t) => t !== tag) : [...tags, tag];
    onChange(next, tagMetadata);
  }

  function setMetadata(key: string, value: unknown) {
    onChange(tags, { ...tagMetadata, [key]: value });
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {SEMANTIC_TAGS.map((tag) => (
          <label key={tag} className="flex items-center gap-1 text-xs">
            <input type="checkbox" checked={tags.includes(tag)} onChange={() => toggleTag(tag)} />
            {tag}
          </label>
        ))}
      </div>

      {tags.includes("resource") && (
        <div className="flex flex-wrap gap-2 rounded-md border border-border p-2">
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">max_ref</span>
            <select
              className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={(tagMetadata.max_ref as string) ?? ""}
              onChange={(e) => setMetadata("max_ref", e.target.value)}
            >
              <option value="">(choose a field)</option>
              {allFieldKeys.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs">
            <span className="text-muted-foreground">low_threshold</span>
            <input
              type="number"
              className="w-24 rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
              value={(tagMetadata.low_threshold as number) ?? 0}
              onChange={(e) => setMetadata("low_threshold", Number(e.target.value))}
            />
          </label>
        </div>
      )}

      {tags.includes("modifier_source") && (
        <CelEditor
          label="modifier_formula"
          value={(tagMetadata.modifier_formula as string) ?? ""}
          onChange={(v) => setMetadata("modifier_formula", v)}
          errors={issuesFor(issues, `${fieldPath}.tag_metadata.modifier_formula`)}
        />
      )}

      {tags.includes("progression") && (
        <label className="flex flex-col gap-1 text-xs">
          <span className="text-muted-foreground">curve_ref</span>
          <input
            className="rounded-md border border-input bg-transparent px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            value={(tagMetadata.curve_ref as string) ?? ""}
            onChange={(e) => setMetadata("curve_ref", e.target.value)}
          />
        </label>
      )}

      {tags.includes("status_set") && fieldType !== "array" && (
        <p className="text-xs text-destructive">'status_set' requires an array-typed field.</p>
      )}

      {issues.filter((i) => i.field_path === fieldPath).length > 0 && (
        <ul>
          {issues
            .filter((i) => i.field_path === fieldPath)
            .map((issue, i) => (
              <li key={i} className="text-xs text-destructive">
                {issue.message}
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}
