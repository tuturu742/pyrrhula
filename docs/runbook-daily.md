# Daily rebuild runbook

Both deployments are torn down and rebuilt from nothing every day, then repopulated from
the two content repositories. The point is not tidiness: a deployment that has been
upgraded in place for a week is not the deployment a new user installs, and every defect
that only appears on a *fresh* install — a missing migration, a pack that never shipped, a
credential the installer no longer writes — is invisible until you do this.

Everything here uses the documented installer. There is no second, private install path.

## What you need

| | |
|---|---|
| Secrets directory | files named `anthropic`, `deepseek`, `gh_tuturu`, `gh_pyrrhula-bot`, each containing one key |
| `pyrrhula-workflows` checkout | the packs; fetched by the build, pinned in `deploy/plugins.json` |
| `pyrrhula-samples` checkout | the `.pyr` bundles and their READMEs |
| Encryption key | `~/.config/pyrrhula/encryption.key` — **outside** both deployments, survives every purge |

The encryption key is the one thing a purge must not take. Sealed credentials in the
database are unreadable without it, so losing it turns every stored API key into bytes
nobody can decrypt, on a deployment that otherwise looks fine.

## Kubernetes — hagnaryd-mystery, mice-invaders, loxia

```bash
cd ~/code/Pyrrhula
export KUBECONFIG=$HOME/.kube/config
export PYRRHULA_K8S_REGISTRY=127.0.0.1:5000
export PYRRHULA_PLUGINS_TOKEN="$(tr -d '\n' < ~/code/lets_finish_it/gh_tuturu)"

./install.sh k8s --multi-tenant --purge
```

`--purge` deletes both namespaces and the volumes they own before installing. Without it
you are testing an upgrade, which is a different thing and not this.

`PYRRHULA_PLUGINS_TOKEN` matters more than it looks: `pyrrhula-workflows` is private, and
without a token the fetch fails, the build falls back to whatever pack happens to be
cached on disk, and the install reports success. The build now prints a WARNING naming
both refs when that happens — set `PYRRHULA_PLUGINS_STRICT=1` to make it a build failure
instead.

## Compose — karsh-vale, pyrrhula, coffee-campaign

```bash
cd ~/code/Pyrrhula
export PYRRHULA_PLUGINS_TOKEN="$(tr -d '\n' < ~/code/lets_finish_it/gh_tuturu)"

./install.sh compose --multi-tenant --purge
```

On a host whose DNS runs through `systemd-resolved`, set a resolver for the containers:

```bash
export PYRRHULA_COMPOSE_DNS=1.1.1.1
```

The host's only nameserver is then `127.0.0.53`, a loopback address that means nothing
inside a container's namespace, so every outbound name lookup fails. Nothing says "DNS":
the model download reports `couldn't connect to huggingface.co`, which reads as an outage
at Hugging Face. The installer now notices the loopback resolver and says so, but it
cannot fix the host for you -- either set this variable or put `dns_servers = ["1.1.1.1"]`
under `[containers]` in `~/.config/containers/containers.conf` once.

`--purge` here removes the named volumes (postgres, blobs, the retrieval model) and the
generated `.env`. The `.env` has to go with them: it holds the password of the database
being deleted, and an install that writes fresh credentials into a file still carrying
the old ones produces a deployment that looks installed and cannot reach its own
database.

## Repopulating

`scripts/seed_samples.py` performs, per tenant, exactly the steps each sample's README
walks a person through — create the model connections, import the `.pyr`, bind every
persona to the connection its role calls for. The READMEs remain the path a human
follows; this is the path a daily loop follows, because a rebuild that takes forty clicks
does not happen daily.

```bash
# Kubernetes (run inside the api pod, which already has the database and the key):
kubectl -n pyrrhula cp scripts/seed_samples.py "$POD":/tmp/seed_samples.py
kubectl -n pyrrhula cp ~/code/pyrrhula-samples "$POD":/tmp/samples
kubectl -n pyrrhula cp ~/code/lets_finish_it "$POD":/tmp/secrets
kubectl -n pyrrhula exec "$POD" -- python /tmp/seed_samples.py \
  --secrets-dir /tmp/secrets --samples-dir /tmp/samples \
  --samples hagnaryd-mystery,mice-invaders
```

Each sample name becomes a tenant of the same slug; `slug=sample` names it differently
(`loxia=pyrrhula` seeds a tenant called `loxia` from the `pyrrhula` bundle's cast).

### The embedding model

A purge takes the model cache with it, and the installer deliberately does not download
models -- which ones a deployment wants is the operator's choice. So a freshly purged
deployment has no embedder, and **every session fails at context assembly** with a message
about the admin console. `seed_samples.py` queues the download before it seeds anything;
it is gigabytes, so the first sessions after a rebuild will fail until the worker
finishes. Check before starting one:

```bash
psql -tAc "select status, result->>'outcome' from job
            where kind='download_retrieval_models' order by created_at desc limit 1"
```

`done` with outcome `downloaded` is the only green. A job can be `done` with outcome
`failed`: the outcome lives in the result payload for the admin console to render, so the
job table alone will tell you it succeeded.

### Which model each persona gets

Declared once in the script, by role rather than by name, so a sample that gains a seventh
persona still gets a model:

| Table | Seat | Connection |
|---|---|---|
| software | lead, architect, senior, staff | `claude-opus-5-5` |
| software | junior, middle, QA | `claude-sonnet-5` |
| tabletop | referee (supervisor) | `deepseek-chat` |
| tabletop | players | `qwen3.8:27b` (local Ollama) |
| enterprise | everyone | `deepseek-chat` |
| any | assistant (informational) | `deepseek-chat` |

## Verifying a rebuild actually worked

A green installer is not the check. These are:

```bash
# the purge was real -- namespaces minutes old, no sample tenants left before seeding
kubectl get ns | grep pyrrhula
psql -tAc "select coalesce(string_agg(slug,', '),'(none)') from tenant"

# the packs in the image are the pinned ones, not a cached copy
kubectl -n pyrrhula exec "$POD" -- grep -c abandoned /app/packs/swdev/schemas/work_item.json

# PDF export is present (it is an optional extra; without it report export raises)
kubectl -n pyrrhula exec "$POD" -- python -c "import weasyprint"

# a seeded tenant has its content -- personas AND the secrets that carry private briefs
psql -tAc "select (select count(*) from persona where tenant_id=t.id),
                  (select count(*) from secret  where tenant_id=t.id)
             from tenant t where t.slug='hagnaryd-mystery'"
```

The secrets count is the one that has actually caught something. An import with no
authoring principal skips every secret and otherwise succeeds, so a six-agent mystery
imports looking complete while the private briefs that are the entire point of it are
absent. `seed_samples.py` now fails loudly rather than reporting a clean import.

## When something is wrong

Fix it in the repository that owns it, never in the running deployment — the next purge
deletes the deployment, and a fix that lived only there is gone with it.

| Wrong thing | Where it is fixed |
|---|---|
| a flow, schema, or overlay | `pyrrhula-workflows`, then bump `deploy/plugins.json` and rebuild |
| a sample's content or setup steps | `pyrrhula-samples` — the bundle and its README |
| the platform | this repository |

A pack change is not shipped when it is committed. It is shipped when the pin names it
*and* the build fetched it: check for the stale-pack warning before believing otherwise.
