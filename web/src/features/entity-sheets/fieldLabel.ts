import { useCallback } from "react";
import { useVocabularyStore } from "@/stores/vocabulary";
import { DEFAULT_LABELS, label } from "@/lib/vocabulary/labels";

/** `max_hit_points` -> "Max hit points": the last segment of a field label key,
 * readable, for the overwhelmingly common case of a user-authored schema whose field
 * keys no overlay has ever heard of. */
export function humanizeFieldKey(labelKey: string): string {
  const leaf = labelKey.slice(labelKey.lastIndexOf(".") + 1).replace(/[_-]+/g, " ").trim();
  return leaf ? leaf.charAt(0).toUpperCase() + leaf.slice(1) : labelKey;
}

/**
 * `useLabel()` for a sheet field. Core label keys (`group.attributes`, `tab.main`) are
 * overlay vocabulary and must resolve or show as odd; a field key (`sheet.strength`) is
 * schema content -- the overlay may label it, but when it doesn't, the reader should
 * see "Strength", not the key.
 */
export function useFieldLabel() {
  const labels = useVocabularyStore((s) => s.labels);
  return useCallback(
    (labelKey: string) =>
      labels[labelKey] !== undefined || DEFAULT_LABELS[labelKey] !== undefined
        ? label(labelKey, labels)
        : humanizeFieldKey(labelKey),
    [labels],
  );
}
