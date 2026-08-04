# Document Ownership

| Artifact | Semantic responsibility | Writer in multi-Agent work |
|---|---|---|
| CONTEXT.md | `domain-modeling` authors confirmed vocabulary; project-memory-manager may initialize or audit | controller only |
| docs/ARCHITECTURE.md | project-memory-manager syncs verified durable structure | controller only |
| docs/STATUS.md | project-memory-manager syncs durable project lifecycle | controller only |
| docs/adr/ | `domain-modeling` creates qualifying decisions; project-memory-manager audits/indexes | controller only |
| docs/status/team.json | `codex-team-workflow` owns active coordination state | controller only |
| docs/status/<module>.md | assigned module handoff | assigned module writer |
| Mai entities/events | `mai-sync` projects verified state | controller only |

Per-change specifications follow the repository's existing convention. They are not long-term memory and must not be copied wholesale into these documents.
