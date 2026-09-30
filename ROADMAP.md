# Roadmap

Direction, not promises — a single-maintainer project, so this is ordered by intent and
kept honest. Issues are welcome on any of it, and "I want to build this" beats "+1".

Three tracks, because Pyrrhula is one engine serving three kinds of table: tabletop
role-playing first, structured enterprise work second, software development third.

## Tabletop RPG

- **Visual-novel mode** — a pack contributing presentation, not just content: character
  sprites, speaker staging, a scene layout, shipped as declarative pack data the core
  renders. Same discipline as everything else: no user code, ever.
- **Richer rule-system bindings** — the Basic Fantasy conversion is the template; the
  goal is a binding that covers a whole system's checks, tables and modifiers without
  a line of Python, so a ruleset is content a table author can ship.
- **More ruleset packs and samples** — a horror one-shot and a second mystery are
  sketched; a system with initiative and conditions would exercise the entity state
  machines properly.
- **Sharper Director's View** — the per-turn disclosure timeline is there; filtering,
  diffing a persona's context between turns, and "why was this concealed" drill-down.
- **Play-by-post hardening** — awaits, timeouts and reminders exist; the goal is a table
  that runs for weeks across time zones without a facilitator babysitting it.

## Software development

- **Proper coding harnesses** — today a delegated agent emits file contents and the
  environment runs the tests. The next step is a real agent loop inside the container:
  read, edit, run, iterate, with the test output in front of it rather than the last
  page of it. Writing that loop well is most of a product on its own, so the likelier
  route is to run somebody else's — Claude Code, opencode, Gemini CLI, Mistral's Vibe
  CLI — inside the container, behind a port like every other cross-cutting dependency.
  That trades the loop away for three problems it does not solve, and they are the
  interesting part: a harness brings its own model access, so its calls never pass
  through `ModelProvider` and are invisible both to `usage_record` and to the egress
  check that lives inside that port; it wants a provider key *in* the container, where
  `agent.credential_ref` exists precisely so one never lands; and each harness has its
  own invocation, output shape and release cadence to pin. Worth proving against one
  harness end to end — metering and credential handling settled — before a second.
- **An image builder, and a registry to push to** — a delegated environment needs the
  repo's toolchain and, now, a coding harness on top of it, and today both are installed
  on every run. A warm socket container pays that once per session; k8s and ECS spawn a
  fresh Job per run and pay it every time, rework rounds included. The repo's own setup is
  usually the larger part of it, so this is not a harness feature — it is what makes
  one-shot engines practical at all. It is also the only route for a deployment that
  cannot reach the public internet, and it removes the question of whether we may
  redistribute a given harness: the deployment builds into its own registry and we ship
  nothing. The pull half exists already (`registry_credential_ref`, `registry_auth`,
  `imagePullSecrets`); the missing half is a builder behind a port, with a
  content-addressed tag so an unchanged recipe never rebuilds, and tenant-namespaced tags
  so one tenant's image is never another's to pull.

  Whether a registry is needed at all depends on the engine, and the answer is not
  uniform. A socket engine needs **none**: the build lands in the same local image store
  the engine pulls from, which is the common self-host case. k8s and cloud engines need
  one their nodes can reach. `PYRRHULA_K8S_REGISTRY` is *not* it — that names a registry
  the operator already runs, pushes with `--tls-verify=false`, and carries install images
  that are identical for every tenant. A built delegation image carries **tenant source**,
  so its registry has to be authenticated and namespaced per tenant; sharing the
  install-time one would make repository contents readable across tenants. One property
  helps: the container never pulls — the kubelet or the engine does — so the registry
  belongs off the `pyrrhula-envs` network entirely, out of reach of the agent-chosen
  commands running inside.
- **Agents free to act inside their container** — choosing their own commands (`rm`, a
  migration, a one-off script), which is ordinary work a human contributor does without
  asking. The container is already the boundary; what is missing is what a safe version
  needs: egress policy for the container's network (arbitrary commands plus network is
  an exfiltration path for repository contents and the job token), CPU and memory
  limits per engine (a wall-clock timeout exists), and a tighter scope and lifetime on
  the token the container carries.
- **Better review and rework loops** — the reviewer refuses a red build today; next is a
  reviewer that reads the diff against the task, asks for the missing test by name, and
  a rework cycle that carries the review comments into the next attempt verbatim.
- **Dogfooding closed loop** — Pyrrhula's own backlog worked by a team of agents managed
  by Pyrrhula. The software-development workflow is the substrate; the loop needs more
  autonomy than is trusted yet.

## General

- **More model providers** — anything LiteLLM reaches works today; first-class support
  means tested defaults, structured-output detection for the disclosure gate, and a
  connection form that knows the provider's parameters.
- **Background and asynchronous sessions** — sessions that advance on a schedule or on
  an external event (a webhook, a commit, a calendar), so a table can run without a
  browser tab open on it.
- **Cloud deployments and cloud engine runners** — verified install paths beyond compose
  and Kubernetes, and container engines that run delegated work on a cloud service. An
  ECS engine exists as an experimental, unverified adapter; it returns to the supported
  set only with a tested install path behind it.
- **Execution engines as a runtime registry** — several engines per deployment, declared
  and edited by the platform admin in the console rather than in a worker environment
  variable that needs a redeploy to change, and selected per tenant as today. The
  tenant-side half exists; the admin-side half is a deployment-level registry with the
  same shape as the retrieval-model override, a page to edit it, and one honest limit
  stated in the UI: a socket engine is only usable where the socket is mounted, which
  no setting can create.
- **Helm chart** — the Kubernetes path as a first-class chart instead of kustomize
  overlays.
- **Hosted read-only demo** — a public instance serving finished sessions (transcript
  and Director's View) so the claim is inspectable without installing.
- **OIDC/SAML** — the identity port is there; enterprise login lands when a real
  deployment asks for it.
- **Federated, portable identities for personas** — a character you take from one world
  to another, provenance intact; `.pyr` is the seed of this.
- **Marketplace-shaped pack discovery** — only if a community exists to want it.

## Explicitly not planned

- **User-authored code execution in packs.** Schemas, state machines and CEL only; this
  is a security stance, not a missing feature. The boundary: a pack is tenant-authored
  content evaluated inside the platform's own process, and nothing there will ever run
  arbitrary code. A delegated agent's execution environment is the opposite case by
  design — an ephemeral container whose whole purpose is building and testing code — and
  widening what an agent may do *there* is on the list above, not a contradiction of
  this one.
- **A hosted multi-tenant service run by us.** The design supports it; running one is a
  different business than building an engine.
