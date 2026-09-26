# Exec engines — where delegated coding agents build and test

> Building is half of it; [previews.md](previews.md) covers running the result where a
> human can open it, and how to configure what that runs.

Delegated work runs in an isolated environment per (session, repo). The engine that
provides those environments is pluggable behind `core/ports/exec_env.py`'s
`ExecEnvProvider`, and the **operator declares** which engines a deployment offers;
**tenants pick one** of the declared keys (Repos page → "Agent environments run on";
`tenant.settings.exec_engine`; API `GET /repos/exec-engines`, `PUT /repos/exec-engines/current`).

## The contract every engine must meet

- `run_script(name, image, script, *, registry_auth) -> ExecResult{exit_code, output}` —
  the whole delegation as **one self-contained shell script** (built by
  `GitMcpTransport._build_work_script`): setup commands, a `command -v git` preflight
  (exit 90 = "image lacks git"), clone/push of the hosted repo over **git smart-HTTP**
  (`Settings.git_http_base` + a short-lived per-repo job token in the URL), file writes,
  the repo's test command (`PYR_TEST_RC=` captured in output; test failures are data,
  not script errors), commit+push. `PYR_STEP=` markers name the failing stage.
- `teardown_matching(prefix) -> int` — remove every environment whose name starts with
  `prefix` (`pyr-env-<session8>-`); called when a session is archived, across **all**
  declared engines.
- `provision`/`exec` are optional (interactive engines only — the socket adapter keeps a
  warm container per name; one-shot engines raise `ExecEnvUnavailableError`).
- **Reachability requirement**: the environment must reach the api's
  `/git/{store_key}/…` smart-HTTP endpoints. Local sibling containers use the default
  engine network and `Settings.git_http_base`; a remote engine whose environments
  need a different route declares its own **`git_http_base`** (per-engine override —
  e.g. k8s pods reaching a podman-hosted api by host IP, or a cloud engine using the
  public URL). No volumes, ever.
- Registry credentials: socket engines get the Docker `X-Registry-Auth` payload per
  pull (from the repo's sealed registry credentials); Kubernetes uses a pre-created
  `image_pull_secret` named in the engine declaration.

## Visibility: the tracked environment registry

Every `run_script` writes (best-effort, never failing the work) to the tenant-scoped
`exec_environment` table: the engine-side name, engine key, image, the **spawning
actor** (the assigned dev persona whose work is executing), the session, and a live
status — `running`, `idle` (a socket engine's warm container awaiting reuse),
`completed`/`failed` (one-shot engines, with exit code), `kill_requested`, `killed`,
`removed` (session-archive teardown). The Repos page shows active environments with
a **Kill** button; `GET /repos/exec-environments` lists them and
`POST /repos/exec-environments/{id}/kill` (gated `repo:manage`) enqueues a worker
job that calls the engine's `teardown_matching(name)` — the recovery path for a
stuck or orphaned environment. The engine stays the source of truth for execution;
the registry is attribution and lifecycle visibility.

## Declaring engines

`PYRRHULA_EXEC_ENGINES` — a JSON list; first entry is the default. With it unset and
the legacy `PYRRHULA_EXEC_SOCKET` set, one `{"key": "local", "kind": "socket"}` engine
is synthesized.

```json
[
  {"key": "local", "kind": "socket", "socket": "/var/run/podman.sock",
   "network": "pyrrhula-envs", "label": "Local containers"},
  {"key": "docker", "kind": "socket", "socket": "/var/run/docker.sock",
   "label": "Docker host"},
  {"key": "k8s", "kind": "kubernetes", "namespace": "pyrrhula-envs",
   "label": "Shared cluster",
   "api_base": "https://k8s.example:6443", "token_file": "/secrets/k8s-token",
   "ca_file": "/secrets/k8s-ca.crt", "image_pull_secret": "regcred",
   "job_ttl_seconds": 3600, "run_timeout_seconds": 1800}
]
```

### `socket` (implemented)

Plain Docker REST API over a unix socket — **works identically for podman and docker**
(`adapters/exec_env/docker_socket.py`). Mount the socket into the worker
(`-v /run/user/1000/podman/podman.sock:/var/run/podman.sock` or
`-v /var/run/docker.sock:/var/run/docker.sock`) and point the declaration (or legacy
`PYRRHULA_EXEC_SOCKET`) at it. **`network`**: environments must reach `git_http_base`;
declare a dedicated network (e.g. `podman network create pyrrhula-envs`) that the API
container joins (`podman network connect pyrrhula-envs pyrrhula_api_1`) but the
database does NOT -- agent-run code gets git access without stack access. Without
`network`, envs sit on the engine default network, which cannot resolve the api
container by name (observed live: 'Could not resolve host'). Rootless podman: enable `podman.socket` (user), run the
worker `--user 0` (socket is root-owned in-container). Docker: the worker's user needs
the docker group's gid or root.

### `kubernetes` (implemented)

`adapters/exec_env/kubernetes.py` — one **Job** per `run_script` (unique name, label
`pyrrhula.dev/exec-env: <env name>`, `backoffLimit: 0`, `ttlSecondsAfterFinished`),
poll the pod, read logs, exit code from the terminated container status. In-cluster:
omit `api_base`/`token` (serviceaccount token + CA auto-detected); out-of-cluster:
declare them. RBAC needed: create/list/delete `jobs`, list `pods`, get `pods/log` in
the namespace. Private images: pre-create a docker-registry Secret and name it in
`image_pull_secret`. Verify with kind/minikube: declare the engine, switch a tenant to
it, delegate — a Job appears in the namespace and the PR lands as usual.

### `aws-ecs` (experimental — unverified)

> **Experimental.** The adapter is complete and once ran against a live account, but no
> release is verified against AWS and the Terraform that stood that account up has been
> removed. Nothing in the default install path references it. Treat it as a starting
> point for a cloud runner, not a supported target; a supported one is on the roadmap.

`adapters/exec_env/aws_ecs.py` — one **Fargate task** per `run_script`. ECS cannot
override a task's image at run time, so the adapter registers a task-definition
revision per image (family `pyrrhula-env-<sha12>`, reused across runs) and runs it
with the script as the container command. The environment name rides `startedBy`
(≤36 chars); `teardown_matching` lists running tasks and stops those whose
`startedBy` matches the prefix. Output comes from CloudWatch Logs (awslogs driver,
stream `pyr/work/<task-id>`), exit code from the stopped task's container.

```json
{"key": "aws", "kind": "aws-ecs", "region": "eu-central-1", "cluster": "pyrrhula",
 "subnets": ["subnet-..."], "security_groups": ["sg-..."],
 "execution_role_arn": "arn:aws:iam::...:role/pyrrhula-exec",
 "log_group": "/pyrrhula/exec-envs", "cpu": "1024", "memory": "2048",
 "assign_public_ip": true, "run_timeout_seconds": 1800}
```

Credentials use the default AWS chain (the worker's **task role** on ECS; env
vars/profile elsewhere). The worker's role needs: `ecs:RegisterTaskDefinition`,
`ecs:DescribeTaskDefinition`, `ecs:RunTask`, `ecs:DescribeTasks`, `ecs:ListTasks`,
`ecs:StopTask`, `iam:PassRole` on the execution role, and `logs:GetLogEvents` on the
log group. The execution role needs the standard `AmazonECSTaskExecutionRolePolicy`
(ECR pull + awslogs). `assign_public_ip: true` lets envs in public subnets pull
images and reach `git_http_base` without a NAT. Per-run `registry_auth` is not
applicable — use ECR or a pre-registered task definition with
`repositoryCredentials`.

## Cloud engines (design, not yet implemented)

The remaining clouds map onto `run_script` + label-based teardown the same way; each
needs an adapter (~the size of the k8s one) plus its auth plumbing:

| kind | run_script maps to | output | teardown | auth (declaration fields) |
|---|---|---|---|---|
| `gcp-cloudrun` | Cloud Run **Jobs**: create job + `run` execution (or `RunJob` with overrides) | Cloud Logging filter by execution; exit code from execution status | list executions by label + delete job | `project`, `region`; OAuth token via service-account (metadata server or key file) |
| `azure-aci` | Container group create (restart `Never`), command `sh -lc <script>` | `containers/{}/logs`; exit code from instance view `currentState.exitCode` | list groups by tag + delete | `subscription`, `resource_group`, `location`; AAD token (managed identity or client secret) |

Shared requirements the table implies: the declaration carries everything network- and
identity-shaped; `git_http_base` must be internet-routable (or peered) for these; per-
image registry auth uses each service's native mechanism (ECS: private registry auth on
the task definition; Cloud Run/ACI: registry credentials on the job/group spec) — the
adapter translates the repo's sealed credentials where the API allows, else documents a
pre-provisioned secret like k8s. Explicit v1 non-goals: autoscaling pools, per-tenant
cloud accounts, spot handling, cost attribution beyond the existing usage records.

## See also

* [`docs/delegation.md`](delegation.md) — the delegate / review / rework loop these engines run work for
