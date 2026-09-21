# Pyrrhula on AWS (ECS Fargate demo)

Everything runs on AWS: the api, worker, web UI and platform-admin console as Fargate
services behind an ALB; RDS Postgres 16 (pgvector), ElastiCache Redis, EFS for the
blob/git store and the embedding-model cache; Secrets Manager for every secret; and —
the interesting part — **delegated coding agents execute as one-shot Fargate tasks in
the same cluster** (the `aws-ecs` exec engine), isolated by security group from
everything except the api's git smart-HTTP endpoint.

Model calls use **cloud API keys** you add in the UI (Connections page) — there is no
GPU anywhere in this stack.

> **Verification status**: the exec-engine adapter has unit tests and the Terraform
> plans cleanly, but this stack has **not** been run live end-to-end. Expect to be the
> first; read the plan output before applying, and keep the budget alarm from step 3.

## First time on AWS? Start here

Everything below assumes only that you can create an AWS account. Once, up front:

1. **Account**: sign up at aws.amazon.com. Turn on MFA for the root user
   (IAM → root user → MFA), then stop using root.
2. **An IAM identity for yourself**: IAM → Users → Create user (e.g. `deployer`) →
   attach the `AdministratorAccess` policy (fine for a personal demo account; narrow
   later). Create an **access key** for it (Security credentials → Create access key
   → "CLI") and note the two values.
3. **Cost guardrail** (do this before deploying anything): Billing → Budgets →
   Create budget → monthly, e.g. $150, with an email alert at 80%. This stack costs
   ~$115/mo while it exists; `terraform destroy` stops all of it.
4. **Local tools** (Arch/CachyOS): `sudo pacman -S aws-cli terraform` (or
   `opentofu`). Then `aws configure` — paste the access key pair, set your region
   (e.g. `eu-central-1`), output `json`.
5. Sanity check: `aws sts get-caller-identity` prints your account id → you're set.

Vocabulary for what Terraform will create: a **VPC** (private network), **RDS**
(managed Postgres), **ElastiCache** (managed Redis), **EFS** (shared filesystem for
the git store), **ECR** (private image registry), **ECS/Fargate** (runs containers
without servers), an **ALB** (the public URL), **Secrets Manager** (holds the five
generated secrets), and IAM roles gluing it together. `terraform destroy` removes
every one of them.

## Prereqs

- Terraform ≥ 1.6 (or OpenTofu), AWS CLI v2 with credentials that can create
  VPC/RDS/ECS/IAM/etc., and docker or podman locally to build images.
- Nothing else. No domain needed (the ALB DNS name is the URL); no manual secrets
  (Terraform generates all five and stores them in Secrets Manager).

## Install

One command from the repo root (wraps everything below):

```bash
./install.sh aws        # add --check to verify prerequisites first
```

Or step by step:

```bash
cd deploy/aws
terraform init
terraform apply                       # ~10-15 min, RDS is the slow part
./build-and-push.sh                   # build + push both images to ECR
./migrate.sh                          # schema + roles; wait for "migration OK"
terraform output url                  # open it, Sign up, done
```

The services start crash-looping the moment `apply` finishes (no image yet) and heal
on their own after the push; give them a minute after `migrate.sh`. The installer
finishes by running `smoke.sh` — the same post-install check every deployment shape
runs, as a one-shot task on the api task definition — and fails if the stack is up
but cannot do real work.

Retrieval models are not downloaded during install. Pods load them strictly from the
EFS cache (`hf_offline=1`), and which model this deployment runs is chosen in
**Admin → Models**, which downloads into that same cache as a background job.
`prewarm.sh` is still here for an operator who wants the cache populated before
anyone logs in.

Routing: the ALB serves the UI at `/` and sends `/api/*` and `/git/*` straight to
the api (the app tolerates the unstripped `/api` prefix), so api redeploys never
leave the UI pointing at a dead task IP. Agent web search ships as a bundled
SearXNG service, internal-only at `searxng.<name>.local`.

Debug shell into any task (the only "ssh" Fargate has; needs the
session-manager-plugin package):

```bash
aws ecs execute-command --cluster pyrrhula --task <task-id> \
  --container api --interactive --command /bin/sh
```

First login flow: **Sign up** creates your organization + first workspace; the setup
checklist walks you through adding a model connection (paste an Anthropic/OpenAI/
Gemini key — stored AES-256-GCM-sealed), creating a starter team, and launching a
first session. To use the workspace assistant, attach your key to the "Assistant
model" connection (its cold-start model is the `assistant_model` variable,
default `anthropic/claude-sonnet-5`).

## Usage limits (the only billing control you need)

Organization owners set daily hard caps under **Projects → Daily usage limits**
(or `PUT /limits`): total per organization, per connection, per persona, per user.
0 = unlimited. When a cap is hit, sessions pause with the reason and assistant calls
return 429 until midnight UTC. Set a small `tenant_daily_tokens` on day one — it is
the platform-side backstop for your provider API bills.

## Delegated coding work on Fargate

The worker's `PYRRHULA_EXEC_ENGINES` (wired by Terraform) declares one engine:
`{"kind": "aws-ecs", "label": "AWS Fargate", ...}` pointing at this cluster. Tenants
see it on the Repos page ("Agent environments run on"). Each delegation runs a
one-shot Fargate task (public image like `node:22-bookworm`, or your ECR image via
the repo's custom-image field) whose only inbound reach is the api's `/git/...`
endpoints; build/test logs come back through CloudWatch (`/pyrrhula/exec-envs`).

## The admin console

Off by default. Set `admin_cidrs = ["<your-ip>/32"]` and re-apply to expose it on
port 8100 of the ALB; the token is in Secrets Manager (`pyrrhula/admin-token`).

## Operating it

- **Upgrade**: `./build-and-push.sh && ./migrate.sh`, then
  `aws ecs update-service --cluster pyrrhula --service api --force-new-deployment`
  (repeat for `worker`, `web`, `admin`).
- **Secrets**: everything is under `pyrrhula/` in Secrets Manager. The
  **encryption key** (`pyrrhula/encryption-key`) seals every credential at rest —
  losing it orphans them all. Don't delete it; consider a backup.
- **Backups**: turn on RDS automated backups for anything beyond a demo
  (`backup_retention_period` on the `aws_db_instance`); EFS holds the git repos and
  uploaded blobs.
- **Logs**: CloudWatch `/pyrrhula/app` (svc/api, svc/worker, svc/web, svc/admin,
  svc/migrate) and `/pyrrhula/exec-envs`.
- **Costs** (rough, eu-central-1): 2×1vCPU/8GB Fargate ≈ $70/mo, db.t4g.micro +
  cache.t4g.micro ≈ $25/mo, ALB ≈ $20/mo, plus pennies for EFS/ECR/logs.
  `terraform destroy` removes everything (RDS without a final snapshot — demo mode).

## Demo topology, and what production would change

Deliberate demo simplifications: public subnets with security-group isolation
instead of private subnets + NAT (saves the NAT gateway, ~$35/mo); HTTP on the ALB
(add an ACM certificate + 443 listener + redirect for TLS — required before real
users type passwords); one instance of each service; `skip_final_snapshot` on RDS.
The application config is production-shaped already: RLS-enforced tenancy, sealed
credentials, no insecure defaults — every secret is generated and injected by ARN.
