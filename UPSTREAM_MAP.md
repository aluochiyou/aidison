# Upstream Source Map

Aidison owns the imported source files. Upstream repositories are not Git submodules and do not own Aidison runtime, Domain state or release decisions.

## Deep Agents Core

- Upstream: `https://github.com/langchain-ai/deepagents`
- Exact commit: `0d38eb2df39b0652d6f2ad92e8d14ae5edc78398`
- Upstream tag: `deepagents==0.7.1`
- Imported path: `packages/deepagents`
- Imported scope: upstream `libs/deepagents` only, including its package tests and documentation
- Excluded: `deepagents-code`, CLI, ACP, Talon, Evals and the rest of the monorepo

### Adoption boundary

- Reuse and modify graph, middleware, backends, synchronous subagent executor and tests.
- Do not use Deep Agents graph/message/file state as Aidison canonical truth.
- Do not adopt `AsyncTask` as the durable task ledger.
- Aidison owns Job, Attempt, Delegation, JoinReceipt, budgets, fencing, cancel and late-result quarantine.

## Deep Agents UI

- Upstream: `https://github.com/langchain-ai/deep-agents-ui`
- Exact commit: `f6a4f34565b42688be06498031fc9351c152614e`
- Imported path: `web`
- Package version at import: `0.1.0`

### Adoption boundary

- Reuse visual primitives, file/message rendering and selected stream interactions.
- Replace chat/thread-first navigation and direct LangGraph state ownership with Aidison Project/Module/query DTOs and cursor SSE.
- The browser never reads PostgreSQL, checkpoint tables or hidden prompts directly.

## Modification log

| Date | Upstream symbol/path | Aidison change | Reason | Verification |
|---|---|---|---|---|
| 2026-08-02 | `libs/deepagents` | Imported exact 0.7.1 source snapshot | Establish owned worker runtime baseline | 132 passed, 1 skipped |
| 2026-08-02 | `create_deep_agent()` | Added caller tool exclusions and explicit native-subagent disable gate | Run Aidison durable child Jobs without a second task ledger | targeted tests 3 passed; full Core regression passed |
| 2026-08-02 | deep-agents-ui root | Imported exact source snapshot and replaced the active entry point with a Project-first console | Project/Module/Decision/Runtime state must come from Aidison APIs | lint 0 errors; production build passed; browser vertical slice passed |
