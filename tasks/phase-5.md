# Phase 5 — Enterprise overlay (8–10 weeks)

**Status:** todo (decompose before phase start; **order is customer-driven**, per Q1/§14.4 —
this is "add enterprise-grade controls to the product", not "build the enterprise product")
**Plan refs:** §14.3, §15.8.

The D11 ports pay off here: each item should be an adapter swap or an additive table, with
call sites unchanged. If an item requires touching call sites, that's a signal the port was
violated somewhere — fix the violation, don't spread it.

| ID | Task | Key content | Deps | Est |
|---|---|---|---|---|
| H5.1 | OIDC + SAML | providers behind `IdentityProvider`; JIT provisioning; group→role mapping | T0.3 | 8d |
| H5.2 | SCIM 2.0 | user/group provisioning | H5.1 | 6d |
| H5.3 | Fine-grained RBAC | `permission_grant(principal, action, resource_id)`; custom roles; inheritance; call sites unchanged | T0.3 | 8d |
| H5.4 | Audit retention | retention windows, legal hold, eDiscovery export, WORM/transparency anchoring (§7.4 layer 3); **includes the Q11 decision: retention window on `disclosure_decision` — the gate's rationale is evidence and cuts both ways (needs Legal)** | T0.7 | 6d |
| H5.5 | Data residency | `TenantRouter` regional DSNs; per-region deploy; cross-region admin plane | T0.3 | 8d |
| H5.6 | Tenant isolation escalation | schema-per-tenant + DB-per-tenant migration tooling (honouring `tenant.isolation_mode`) | T0.2 | 6d |
| H5.7 | Per-tenant KMS / BYOK | behind `Encryptor`; key rotation; re-encrypt migration | T0.3 | 5d |
| H5.8 | Cost dashboards | budgets + alerts per model/agent/workspace/tenant (req 30, building on Phase-1 telemetry) | T0.8 | 6d |
| H5.9 | Durable execution | Temporal behind `JobQueue` — **only if operational data says the Postgres queue is inadequate; do not do this on principle** | B1.6 | 8d |
| H5.10 | Enterprise reporting | `decision_summary` from `DisclosureDecision` + concession records (§8.7) — the auditable-decision-log artefact no competitor produces | G4.10 | 5d |
| H5.11 | SOC2-supporting controls | access reviews, change-management evidence, log shipping; **which standard first is Q10 — only a customer can answer** | H5.4 | ongoing |
| H5.12 | Repo permission mapping (D15) | git-provider teams/permissions → workspace membership + scopes; SCIM-adjacent, sits beside H5.1–H5.3 | H5.1, G4.15 | 5d |
| H5.13 | CI/CD integration depth (D15) | `build` entities fed by CI webhooks through the MCP server (G4.13); deployment approvals as process `await`s; feeds H5.11 change-management evidence | G4.13, G4.16 | 6d |
| H5.14 | Delegated-agent cost governance (D15) | budgets + alerts for `purpose='delegation'` spend per agent/workspace/tenant, extending H5.8 | H5.8, G4.16 | 3d |

## Exit gate

- [ ] SSO login works end to end; a custom role is created and enforced; an audit export
      passes tamper-evidence verification.
- [ ] Q10 (compliance standard), Q11 (disclosure_decision retention), Q12 (marketing the
      secrets model as a compliance control — only with published eval numbers, never the
      adjective) are decided and documented.
