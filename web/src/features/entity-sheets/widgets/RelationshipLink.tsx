import { useFieldLabel } from "../fieldLabel";

export interface RelationshipLinkProps {
  labelKey: string;
  value: string;
}

/** the `relationship` tag -- a project's owner, a work item's assignee, a skill's
 * governing attribute: a reference to something else, rendered as a link-styled chip
 * (not a real router link -- the referenced id's own entity type isn't known here,
 * only that this field points at one). */
export function RelationshipLink({ labelKey, value }: RelationshipLinkProps) {
  const t = useFieldLabel();
  return (
    <div className="flex flex-col gap-0.5" data-widget="relationship_link">
      <div className="text-xs text-muted-foreground">{t(labelKey)}</div>
      <div className="text-sm text-primary underline-offset-2 hover:underline">{value}</div>
    </div>
  );
}
