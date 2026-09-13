/**
 * The session traffic-light: green (pulsing) = something is happening right now,
 * yellow = paused / waiting, red = the flow reached its terminal phase. `activity` is
 * derived server-side (SessionResponse.activity) so every surface agrees on the rules.
 */
export function SessionStatusDot({ activity }: { activity?: string }) {
  const color =
    activity === "running"
      ? "bg-green-500 animate-pulse"
      : activity === "stopped"
        ? "bg-red-500"
        : "bg-yellow-500";
  const label =
    activity === "running" ? "running" : activity === "stopped" ? "finished" : "waiting";
  return (
    <span className="inline-flex items-center" title={label}>
      <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${color}`} />
    </span>
  );
}
