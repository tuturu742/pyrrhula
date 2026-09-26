import { useEffect, useState } from "react";
import { getMissingKeysReport } from "./labels";

/**
 * Missing-key detection in dev mode: a badge (dev builds only) showing how
 * many `label_key`s resolved with no entry in the active overlay (or `DEFAULT_LABELS`) since
 * the page loaded — click to log the full report to the console. Renders NOTHING while the
 * report is empty (polled quietly), so a clean surface carries no dev chrome at all.
 */
export function MissingVocabularyKeysBadge() {
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    const timer = setInterval(() => setCount(getMissingKeysReport().length), 3000);
    return () => clearInterval(timer);
  }, []);

  if (!import.meta.env.DEV || count === 0) return null;

  return (
    <button
      type="button"
      onClick={() => console.warn("[vocabulary] missing label_key report:", getMissingKeysReport())}
      title="Click to log the missing vocabulary label_key report to the console"
      className="rounded-full bg-destructive px-2 py-0.5 text-xs text-destructive-foreground"
    >
      vocab: {count} missing
    </button>
  );
}
