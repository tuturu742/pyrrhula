import { useCallback } from "react";
import { useVocabularyStore } from "@/stores/vocabulary";
import { label } from "./labels";

/** React hook form of `label()` -- the one import feature components should use.
 * Subscribes to the active overlay (`useVocabularyStore`), so a live overlay switch
 * (`VocabularySwitcher`) re-renders every consumer automatically. */
export function useLabel() {
  const labels = useVocabularyStore((s) => s.labels);
  return useCallback((labelKey: string) => label(labelKey, labels), [labels]);
}
