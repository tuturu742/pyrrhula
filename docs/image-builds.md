# Images: building, importing and running your own

A delegation runs in a container, and the container's image decides which tools the agent
has: Godot, a Rust toolchain, the coding harness. Pyrrhula lets an organization **build** an
image, **import** a published one, and pick it as a repository's runtime — and it checks
every image before anything runs in it.

Pyrrhula never builds images on its own machines. Builds run on a system the operator
chose; Pyrrhula says what to build and where to push it, then trusts none of the answer.

## The life of an image

```
                                   ┌─ import (.pyr or "Add a published image")
Dockerfile ─ Build ─ builder ─ push ┴─ verifying ── smoke_testing ── ready ── runtime
```

1. **Verifying.** Pyrrhula asks the registry for the image's digest itself, with the
   registry's read credential when one is declared. A builder's reported digest must match
   it; a bundle's reference must already be pinned by digest.
2. **Smoke testing.** The image is pulled and run **on the organization's own execution
   engine** — the same engine, pull path and credential a delegation will use. `git` must be
   present. If the image claims a coding harness, its version must print.
3. **Ready.** The image becomes a **build runtime** named after it, pinned to the digest
   (`…/godot-node@sha256:…`), so it can never drift to whatever a tag points at later.
   Earlier ready builds stay in the history; **Make current** rolls back.

A harness proven by the smoke test is not installed again on every delegation — on a
one-shot engine (Kubernetes, ECS) that saves roughly a minute per run. A harness that is only
claimed, or whose install the operator has since changed, is installed as before.

## For an organization (Repos ▸ Images)

- **Add a published image** — a reference pinned by digest. Tags are refused: a tag can be
  moved to different contents after the image was checked.
- **Write a Dockerfile** — start from a template, or **Propose** one from a repository (a
  model reads the file list and manifests and drafts it; nothing is saved until you save).
  **Check**, **Save**, then **Build** on one of the builders your operator made available.
- A `.pyr` bundle can carry images; importing it starts their check, and they appear as
  runtimes once they pass. A refused image (outside the allowlist, unreachable, failing its
  smoke test) is reported with the reason; the rest of the bundle still imports, and the
  bundle's Dockerfile is kept so the image can be rebuilt on your own builder.

### What a Dockerfile here may contain

The build sees the Dockerfile **and nothing else** — never the repository. An image is the
tools; the code is cloned in later.

| Refused | Why |
|---|---|
| `ADD`, `COPY` without `--from` | there is no build context |
| `RUN --mount/--network/--security`, heredocs, `# syntax=` | BuildKit-only; the same file must build on every kind of builder |
| `ONBUILD`, `VOLUME`, `LABEL pyrrhula.*` | behaviour elsewhere, later; platform labels |
| untagged `FROM` | it would build from whatever `latest` is that day |

Warned: a non-root `USER`, `ENTRYPOINT`, `curl … | sh`, anything that looks like a secret.
Every `FROM` is held to the same rules as any runtime image (below). **Never put a secret in
an image** — everyone who can pull it can read every layer.

## For the operator (Admin ▸ Registries, Admin ▸ Builders)

### Registries

A declared registry is where builds are pushed and verified. Each one reserves a
**namespace per organization** — `host/prefix/t<org id>/<name>` (`pyr-t<org id>-<name>` on
Docker Hub, which allows no nesting) — and one organization's reference into another's part
of it is refused. Its **read credential** is used to verify digests and lent to engines to
pull, and **only for the organization's own namespace**: the operator's credential can
usually read the whole registry.

**Publishing an image for everyone** (a sample, a shared toolchain): copy it out of the
organization's namespace first, e.g. `skopeo copy --all --preserve-digests` from
`ghcr.io/acme/pyrrhula/t<org>/godot-node@sha256:…` to `ghcr.io/acme/samples/godot-node`.
Same digest, no rebuild. A reference *inside* an organization's namespace is refused for
every other organization on any deployment that declares that registry and prefix — which
is the rule working, and why a shared image must not live there.

The **runtime-image allowlist** (same page) restricts where *any* runtime image may come from
— repository settings, a repository's own `pyrrhula-build.json`, runtimes, harnesses,
previews, imported bundles, `FROM` lines. Empty means unrestricted.

### Builders

| Kind | Pyrrhula holds | Where RUN steps execute |
|---|---|---|
| **GitHub Actions** | a token that can dispatch one workflow | GitHub's runners (or your self-hosted ones) |
| **Portainer** | an API key of a non-admin user with access to one environment, and the registry push credential | the Docker engine you manage in Portainer |
| **Webhook** | a signing secret (and optional token) | whatever your receiver drives |

**GitHub Actions.** Put [`builders/github-actions.yml`](builders/github-actions.yml) in a
repository you control, set its `PYRRHULA_TARGET_PREFIX` variable (e.g.
`ghcr.io/<owner>/pyrrhula/`), declare the builder with owner/repo/workflow. The workflow pushes
with its own `GITHUB_TOKEN`; Pyrrhula never holds a push credential. Images pushed by a
workflow in a public repository inherit its visibility through the `source` label.

**Portainer.** Builds stream for their whole duration, so they run as one job each; in a busy
deployment run a second worker with `PYRRHULA_WORKER_ROLE=image-builder` and the first with
`general`, so a build never delays a delegation. **Push host** covers a registry the engine
reaches under another name — typically one beside the engine, pushed to over loopback (which
Docker trusts without TLS) and pulled by everything else at its LAN address. Before any
organization can use a Portainer builder, run its **isolation probe** — a throwaway build that
reports what a RUN step there can reach — and acknowledge the result. Changing the engine or
network clears the acknowledgement; host networking is refused.

**Webhook.** The signed contract and a reference receiver: [`builders/webhook.md`](builders/webhook.md).

**Limits** (same page): builds at once and per day per organization, and a timeout.

## Engines and private registries

- **Socket engines** pull with the registry credential above (own namespace only). A plain-HTTP
  registry must be trusted by the engine (`registries.conf` for Podman, `insecure-registries`
  for Docker).
- **Kubernetes** pulls with the engine's `image_pull_secret`; the registry's own
  `k8s_pull_secret` field is not wired yet, so name a secret on the engine that covers it.

## Not in v1

No build secrets; no garbage collection of superseded images on GHCR or Docker Hub (a
`registry:2` with deletion enabled can be cleaned with its own `garbage-collect`); builds are
`linux/amd64`.
