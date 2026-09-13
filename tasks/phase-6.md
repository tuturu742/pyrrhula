# Phase 6 — Scale & hardening (ongoing)

**Status:** todo · **Plan refs:** §15.9, §16.1.
**Target:** p95 turn latency < 8s at 100 concurrent sessions.

Trigger-driven, not calendar-driven. Each item has an explicit trigger — do not start it
before the trigger fires:

| Item | Trigger |
|---|---|
| Qdrant migration behind `VectorStore` | >2M vectors, **or** p95 retrieval >150ms, **or** filtered-recall degradation in eval — not before |
| ParadeDB `pg_search` (real BM25) | eval shows `ts_rank_cd` is the retrieval bottleneck |
| Read replicas | primary read saturation |
| Per-tenant rate limits + quotas | first noisy-neighbour incident (or SaaS launch) |
| Context-cost optimisation round | Q7 telemetry (cost per session hour) identifies the hot spots |
| WebSockets for presence/typing | Phase-4 multi-participant demand justifies it |
| Pack marketplace | **deliberately rejected (Q2)** — revisit only as an explicit product decision |
| WASM sandbox for pack logic | CEL proven insufficient with real pack-author evidence (Q8) |
| Code-aware chunking/embeddings (tree-sitter, symbol-level) (D15) | retrieval eval on real swdev usage shows G4.15's docs-first chunking failing code-targeted queries — not before |
| Incremental repo sync (webhook-driven re-ingest) (D15) | full snapshot-per-SHA exceeds the ingestion budget on a real repo |
| Board/kanban view for `status_set` entities (D15) | swdev usage demand |
| Native sandboxed code execution in core | **deliberately rejected (D15)** — revisit only as an explicit product decision, mirroring the Q2 marketplace row |
