# MCP servers

An MCP server gives your agents tools the platform does not ship — a search index, a
game engine, an internal API. This page covers what they are here, how to attach one,
and why a clean install lists none.

## Built-in tools are not MCP servers

Two different things wear the same label in the codebase, and telling them apart is the
whole point of this section.

**Built-in tools** are addressed with a `pyrrhula://` URL — `pyrrhula://resolution`,
`pyrrhula://git`. They are the platform's own code, called in-process. Nothing leaves
the deployment, no third party is involved, and there is nothing to install or approve.
Workflow packs declare them so a workflow arrives with its dice roller or its delegation
tool already wired.

**External MCP servers** are anything else — `http://`, `https://`. These are real
endpoints owned by someone, and attaching one is a decision with consequences.

A clean install has **zero external MCP servers**. If the admin console's plugin
repository list shows entries under *Built-in tools*, that is the bundled workflow pack
declaring its own tooling, not an external endpoint that got attached.

## Attaching one: two independent halves

Attaching an MCP server means doing two separate things. Keeping them separate is
deliberate — the first is plumbing, the second is the security boundary.

1. **Make it reachable** from the api and worker containers. Networking only.
2. **Grant it** to a workspace, naming the exact tools that workspace may call.

A reachable server grants nothing on its own. Nothing can call it until step 2, and
even then only the tools you listed.

## 1. Make it reachable

**It already has a URL.** Nothing to do. A server on another host, in your VPC, or a
hosted service is reachable as-is. Skip to granting. This is the common case and needs
no deployment changes at all.

**compose, server running in a container.** Add it to `docker/compose.selfhost.yml`
alongside the others and it is reachable at `http://<service-name>:<port>`. Put it on
the default network, not `envs` — that one is deliberately restricted to exec
environments.

**compose, server running on the host.** Reach the host from a container at
`http://host.containers.internal:<port>` (podman) or `http://host.docker.internal:<port>`
(Docker Desktop). On Docker for Linux, add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the `api` and `worker` services.

**Kubernetes, server running on the host.** A pod cannot reach the node's localhost by
name, so give it a Service with a hand-written EndpointSlice. Copy the template:

```bash
cp deploy/k8s/host-mcp.example.yaml my-mcp.yaml
# edit the name and port, then
kubectl apply -f my-mcp.yaml
```

The template carries the label `pyrrhula.io/host-endpoint: "true"`. `dev-up.sh` rewrites
every slice with that label to the address of the machine it runs on, so the manifest
stays portable — re-run it, or patch the address yourself:

```bash
kubectl -n pyrrhula patch endpointslice my-mcp-1 --type=json \
  -p '[{"op":"replace","path":"/endpoints/0/addresses/0","value":"<host ip>"}]'
```

Verify a pod can actually reach it before moving on:

```bash
kubectl -n pyrrhula exec deploy/pyrrhula-api -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://my-mcp:9000', timeout=5).status)"
```

## 2. Grant it

**One workspace** — *Workspace → MCP servers → Register*. Or over the API:

```bash
curl -X PUT http://localhost:8000/mcp-servers \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"workspace_id":"<uuid>","key":"my-mcp","url":"http://my-mcp:9000",
       "enabled_tools":["search","fetch"],"effectful_tools":["fetch"],
       "require_confirmation":true}'
```

`PUT` is idempotent: re-running a setup script updates the grant instead of creating a
duplicate. It requires `workflow:manage` (owner/admin) plus membership of the target
workspace, because it widens what that workspace's agents can reach.

**Every workspace in a tenant** — *Admin → Tenants → MCP* (MCP capabilities), or
`PUT /admin/tenants/{tenant_id}/mcp-servers`. A tenant grant is materialized onto every
existing workspace and every workspace created later, and survives re-applying a
workflow. Use it for infrastructure everyone should have; use a workspace grant for
anything narrower.

### The fields that matter

| Field | What it does |
| --- | --- |
| `enabled_tools` | The allowlist. A tool not named here cannot be called, whatever the server offers. Not optional, and not a wildcard. |
| `effectful_tools` | Which of those change something outside the platform. These are metered and audited as effectful, and are the ones `require_confirmation` gates. |
| `require_confirmation` | Default `true`. Effectful calls from an agent turn are refused with `confirmation_required`; there is no in-product approval step yet, so a server whose effectful tools agents should call needs this off. Turn it off only for a server you own and trust to be idempotent. |
| `credential_ref` | A *pointer* into a secret manager, never the credential itself. Pasting an obvious live key here is refused, but that check is a crude prefix guardrail (`sk-`, `ghp_`, `AKIA`, …), not a secret detector — do not rely on it to catch your mistake. |
| `max_calls_per_session` | How many times one session may call this server. Blank means unlimited. The cap lives here because an external server is never told which session is calling, so any budget it kept itself would be one pool shared by every concurrent session. A capped-out caller gets a plain `session_call_cap_reached` refusal it can reason about. |
| `timeout_seconds` | How long to wait, default 120. A property of the server, not the deployment: a lookup tool that answers instantly should fail fast, while an engine tool legitimately runs a build for ten minutes. |
| `max_result_chars` | Caps one answer, default 100k, so a single server cannot flood a context. |
| `options` | Transport-specific knobs for this server, e.g. `{"engines": "bing,duckduckgo"}` for a SearXNG instance. Each transport reads only its own keys. |

The allowlist is enforced on the call path, not suggested to the model. Adding a server
with three tools grants three tools, even if the server later advertises thirty.

## Web search is bundled, and is not an MCP server you attach

Agent web search is a persona toggle, served by a SearXNG instance that ships with the
deployment — compose runs one, and Kubernetes runs one from `base/searxng.yaml`. You do not attach it as an MCP server; `web_search`
is a reserved key served by its own transport.

Nothing reaches it unless a persona has the toggle on, and it is never exposed outside
the deployment's own network.

- **Point it somewhere else**: set `PYRRHULA_WEB_SEARCH_URL` to your own SearXNG (or
  compatible) host and drop the bundled service.
- **Turn it off**: clear `PYRRHULA_WEB_SEARCH_URL`. The toggle then has nothing behind
  it and the feature is unavailable.

One setting matters if you run your own: the platform queries `/search?format=json`,
which stock SearXNG refuses with `403` until the JSON format is enabled — see
`docker/searxng-settings.yml`, mirrored in the k8s ConfigMap.

## No model is configured by default

Related, and a frequent first surprise: a clean install ships **no assistant model**.
Earlier builds defaulted to a specific local ollama tag, so every fresh deployment
pointed at a host and a model that were not there. Now you choose:

- In the UI, set a model on the **Assistant model** profile, and attach the provider key.
  There is no deploy-time variable for it: a deployment does not know what models its
  tenants have, so it names none.

Until one is set, the assistant exists but any call returns a 409 saying exactly this.

For a host-local model provider on k8s, the ollama wiring is an opt-in component:

```yaml
# deploy/k8s/overlays/dev/kustomization.yaml
components:
  - ../../components/host-ollama
```

## Troubleshooting

**A tool is not offered to the agent.** It is not in `enabled_tools`, or the key
collides with a reserved one (`web_search`, `web_fetch`, `resolution`, `git`, and anything
starting `git-` are served by dedicated transports and are not routable as external servers).

**Calls fail with a connection error.** The grant is fine; the plumbing is not. Test
reachability from inside the api container, not from your laptop — container DNS and
host DNS are different worlds.

**Effectful calls are refused with `confirmation_required`.** `require_confirmation` is
on for that server, and an agent turn cannot supply the confirmation. Turn it off for
that server if its effectful tools are meant to be called by agents.

**The admin console shows servers you did not add.** Look at which block they are in.
*Built-in tools* is the bundled pack's own `pyrrhula://` tooling. Only *External MCP
servers* is an approval decision.

## See also

* [`docs/delegation.md`](delegation.md) — the coding-agent loop that reaches a repository through the git transport
