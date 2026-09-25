# Security policy

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's private reporting on this repository: **Security → Report a vulnerability**.
That opens a channel visible only to the maintainers.

Please include what you did, what you expected, and what happened instead. A minimal
reproduction is worth more than a long description. If you have a fix, say so — but send
the report first rather than opening a pull request that describes the flaw in public.

You should get an acknowledgement within a week. This is a small project; if you hear
nothing, assume the message was missed rather than ignored, and send it again.

## What counts as a vulnerability here

Pyrrhula's central claim is that **who-knows-what is enforced by the system, not requested
of the model**. Anything that breaks that claim is a security issue even if nothing
crashes:

- **Cross-tenant leakage.** Any path where one tenant's data is reachable from another —
  a query that skips `tenant_scope()`, an RLS policy that does not filter, an id that is
  guessable and unchecked.
- **Secret plaintext reaching somewhere it was excluded from.** A concealed secret
  appearing in a generation context, an export, a report, a manifest, or another
  participant's view. Exclusion is the mechanism; an instruction to the model to keep
  quiet is not a control, and its absence is not the bug — the plaintext's presence is.
- **Privilege escalation between workspace roles**, or a permission check that can be
  reached around rather than through `PermissionService`.
- **Credential exposure.** Provider API keys are stored encrypted and referenced
  indirectly; a path that returns one, logs one, or writes one into an export is a
  vulnerability.
- **A share link doing more than it should.** Preview links are unauthenticated by design
  and scoped to one preview; anything reachable beyond that is in scope.

## What is not a vulnerability

- **A model saying something unwise.** Pyrrhula constrains what a model can *see*, not
  what it chooses to say about what it legitimately knows. A persona revealing its own
  secret is the disclosure gate working, not a leak.
- **Findings that require an operator to attack their own deployment.** A platform admin
  can already read the database.
- **Bundles you were given.** A `.pyr` is attacker-controlled input and is treated as
  such — its content is scanned and quarantined — but importing one from a stranger and
  then approving its quarantined entries is a choice, not a flaw.

## Supported versions

Pre-1.0: only the latest `main`. There are no maintained release branches yet.
