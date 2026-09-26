/**
 * Frontend mirror of `core.entities.tags.TAG_WIDGET_REGISTRY` -- the same
 * fixed, closed 9-tag vocabulary maps to the same widget ids here. Kept as a small,
 * duplicated data table (not fetched from the API) because it's a *rendering* contract,
 * not tenant data; a future task could expose it as an endpoint if keeping the two
 * copies in sync ever becomes a real problem, but nothing about that is load-bearing
 * for its own acceptance criteria.
 *
 * Total and domain-blind, same as the backend registry: every core tag maps to
 * something, and this file never references a pack/domain literal.
 */

export const SEMANTIC_TAGS = [
  "resource",
  "attribute",
  "status_set",
  "progression",
  "identity",
  "descriptor",
  "relationship",
  "modifier_source",
  "private",
] as const;

export type SemanticTag = (typeof SEMANTIC_TAGS)[number];

/** "private" maps to no visual widget -- it's a visibility marker other tags compose
 * with (a field already isn't in the API response at all when the viewer can't see
 * it), not a rendered component of its own. */
export const TAG_WIDGET_REGISTRY: Record<SemanticTag, string | null> = {
  resource: "resource_bar",
  attribute: "attribute_block",
  status_set: "status_chip_row",
  progression: "progression_meter",
  identity: "identity_text",
  descriptor: "descriptor_text",
  relationship: "relationship_link",
  modifier_source: "attribute_block",
  private: null,
};

/** First tag (in the field's own declared order) that resolves to a real widget --
 * a field carrying `["attribute", "modifier_source"]` renders once, as one
 * `attribute_block`, not twice. */
export function widgetIdForTags(tags: string[]): string | null {
  for (const tag of tags) {
    const widget = TAG_WIDGET_REGISTRY[tag as SemanticTag];
    if (widget) return widget;
  }
  return null;
}
