# Source categories and conflict rules

- Observed implementation truth: running code, actual config and executed tests.
- Intended behavior: current user-confirmed acceptance criteria or an available authoritative change specification.
- Decision rationale: accepted ADR.
- Domain language: user-confirmed CONTEXT.md definitions.
- Current coordination state: `docs/status/team.json`.
- Durable structure/lifecycle: ARCHITECTURE.md and STATUS.md.
- Dashboard projection: Mai.

Sources from different categories do not overwrite one another. Any disagreement is drift and must be reported with evidence. Code does not automatically mean the design is correct; when implementation conflicts with intended behavior or an accepted ADR, report both instead of silently rewriting history.
