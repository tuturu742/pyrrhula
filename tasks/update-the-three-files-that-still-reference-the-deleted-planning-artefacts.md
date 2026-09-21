# Update the three files that still reference the deleted planning artefacts

- title: Update the three files that still reference the deleted planning artefacts
- description: The planning artefacts were deleted in #10, but three files still point at
them, so the repository now instructs its readers to consult documents that do not
exist. Update all three; change nothing else.

1. CLAUDE.md, "Source-of-truth order" (around line 9). Entries 1, 2 and 4 name
   `pyrrhula-development-plan.md`, `tasks/<phase>/<task>.md` and
   `pyrrhula-research-brief.md`, all of which are gone. This list is what a coding agent
   reads first, so it must not send anyone to a missing file. What remains as authority
   is `docs/agent-guide.md` (conventions and definitions) and the hard rules in CLAUDE.md
   itself; rewrite the section to say that, keeping the surrounding tone.

2. README.md, the repository-map table (around line 188). Remove the two rows for
   `pyrrhula-research-brief.md` and `pyrrhula-development-plan.md`. Leave every other row
   untouched.

3. docs/agent-guide.md, first paragraph (line 3). It opens by saying it "condenses the
   authoritative development plan (`pyrrhula-development-plan.md`, ...)". That plan is no
   longer in the repository, so the sentence needs rewording: this guide IS the
   conventions reference now, not a condensation of something else.

Do not delete any file. Do not add files. Do not touch anything outside these three.

## Brief

The requested documentation updates are still not implemented, and the diff violates the explicit constraints to add no files and touch nothing outside the three named files. Remove `src/update-the-three-files-that-still-reference-the-deleted-planning-artefacts.js` and `tasks/update-the-three-files-that-still-reference-the-deleted-planning-artefacts.md` from the change. Then modify only `CLAUDE.md`, `README.md`, and `docs/agent-guide.md` as specified: rewrite the source-of-truth section to name `docs/agent-guide.md` and CLAUDE.md’s hard rules as the remaining authorities; remove exactly the two obsolete repository-map rows from README.md; and reword the guide’s opening paragraph so the guide is the conventions reference, not a condensation of the deleted plan.

## Status

Scaffolded by the delegated coding agent. TODO: implement.
