/** "3m ago" for list rows; empty string when unknown. */
export function relativeTime(iso?: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0) return "just now";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Display name: explicit name, else the agenda's first words, else the short id. */
export function sessionDisplayName(s: {
  id: string;
  name?: string | null;
  agenda_md?: string | null;
}): string {
  if (s.name) return s.name;
  const agenda = (s.agenda_md ?? "").trim().split("\n")[0];
  if (agenda) return agenda.length > 60 ? `${agenda.slice(0, 57)}…` : agenda;
  return `Session ${s.id.slice(0, 8)}`;
}
