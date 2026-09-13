# Roadmap

Direction, not promises — single-maintainer honesty applies. Ordered roughly by intent;
issues welcome on any of it, and "I want to build this" beats "+1".

## Near (0.1.x)

- **Helm chart** — the k8s path as a first-class chart instead of kustomize overlays.
- **More rule systems and samples** — the pack format is stable; the Basic Fantasy
  conversion is the template. A horror one-shot and a second mystery are sketched.
- **Sharper Director's View** — the per-turn disclosure timeline is there; filtering,
  diffing a persona's context between turns, and "why was this concealed" drill-down.
- **First-run polish** — the distance from `install.sh` to a running sample measured in
  minutes and kept there.

## Mid (0.2)

- **Workflow-authored UI extensions** — packs contributing presentation, not just
  content: the flagship is a **visual-novel mode** for tabletop sessions (character
  sprites, speaker staging) shipped as pack data the core renders, with the same
  discipline as everything else — no user code, declarative only.
- **Hosted read-only demo** — a public instance serving finished sessions (transcript +
  Director's View) so the claim is inspectable without installing.
- **Play-by-post hardening** — awaits/timeouts/reminders exist; the mid-goal is a table
  that runs for weeks across timezones without a facilitator babysitting it.
- **OIDC/SAML** — the identity port is there; enterprise login lands when a real
  deployment asks for it.

## Far / open questions

- **Dogfooding closed loop** — Pyrrhula's own backlog worked by a team of agents managed
  by Pyrrhula (the swdev workflow is the substrate; the loop needs more autonomy than we
  currently trust).
- **Federated/portable identities for personas** — a character you take from one world
  to another, provenance intact (`.pyr` is the seed of this).
- **Marketplace-shaped pack discovery** — only if a community exists to want it.

## Explicitly not planned

- User-authored code execution in packs (schemas + FSMs + CEL only — this is a security
  stance, not a missing feature).
- A hosted multi-tenant SaaS run by us. The design supports it; running one is a
  different business than building an engine.
