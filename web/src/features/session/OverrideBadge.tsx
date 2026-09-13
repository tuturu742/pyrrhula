/**
 * G4.4 (req 10): the badge that makes a human-in-place-of-agent turn visible.
 *
 * There is no "hide me" prop and no conditional on the message's content: if a turn was a
 * human override, this renders. Whether *participants* see it at all is decided upstream
 * by the workspace's `show_override_badge_to_participants` setting (default on) before the
 * event ever reaches this component -- the overseer's view never consults that setting.
 * Keeping the policy out of the component means no future edit can accidentally make the
 * badge conditional on something it shouldn't be.
 */
export function OverrideBadge({ rewriteApplied }: { rewriteApplied: boolean }) {
  return (
    <span
      className="ml-2 inline-flex items-center rounded-full border border-amber-500/50 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-600"
      title={
        rewriteApplied
          ? "A human wrote this turn in the agent's place; an AI rewrite conformed it to the agent's voice, and the human approved it before posting."
          : "A human wrote this turn in the agent's place, verbatim."
      }
    >
      human override{rewriteApplied ? " · rewritten" : ""}
    </span>
  );
}
