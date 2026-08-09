# Aidison release learning evidence report

- Date: 2026-08-08; refreshed 2026-08-09
- Scope: C5 baseline revision `0dca4b8`, plus the current repository-local documentation audit at `38b5efe`
- Method: local source, migrations, tests, ADRs and pinned upstream map first
- Labels: `[F]` verified fact, `[J]` design judgment, `[H]` hypothesis/pending work, `[X]` rejected or superseded claim

## Release claims

| ID | Claim | Status | Evidence |
|---|---|---|---|
| E-001 | Top-level durable orchestration is Aidison-owned and PostgreSQL-backed; it is not delegated to LangGraph or DeepAgents. | `[F]` | `src/aidison/infrastructure/runtime.py`, `src/aidison/infrastructure/planning.py`, ADR-0001/0004 |
| E-002 | `DurablePlanExecutor` is business-neutral and is used by Research, Solution and Impact; dynamic orchestration is no longer Research-only. | `[F]` | `src/aidison/application/execution.py`, Research/Solution/Impact integration tests, ADR-0004 |
| E-003 | The executor currently consumes a bounded ready wave; it is not an arbitrary-depth autonomous DAG scheduler. | `[F]` | `DurablePlanExecutor.dispatch_ready_wave`, ADR-0004 consequences |
| E-004 | Plan history supports immutable revisions, ready frontier, task-to-Job binding and compare-and-swap replan. | `[F]` | `src/aidison/infrastructure/planning.py`, `tests/integration/test_plan_store.py` |
| E-005 | Runtime supports `ALL_REQUIRED`, `BOUNDED_PARTIAL` and deterministic `FIRST_VALID` Join policies. | `[F]` | `src/aidison/runtime/contracts.py`, `_evaluate_open_join`, ADR-0005 |
| E-006 | Lease generation, fencing, receipts, sibling cancellation, late-result quarantine and budget reconciliation are PostgreSQL state transitions. | `[F]` | `src/aidison/infrastructure/runtime.py`, integration runtime/join tests |
| E-007 | PostgreSQL `LISTEN/NOTIFY` is only a wake optimization; correctness always comes from rereading durable state. | `[F]` | `DurableJoinWaiter`, `src/aidison/infrastructure/signals.py`, ADR-0003 |
| E-008 | Shopping requires a one-time, server-scoped `EffectApproval` before provider `create_cart`; the provider handoff is prepared durably first. | `[F]` | `src/aidison/application/shopping.py`, migration `b7d3e5f91a20`, ADR-0006 |
| E-009 | Current shopping provider boundary is not a real Taobao purchase integration. | `[F]` | ADR-0006, `docs/STATUS.md`, shopping provider contracts/tests |
| E-010 | Deep Agents Core 0.7.1 and deep-agents-ui are pinned imported source snapshots; their canonical state ownership is deliberately restricted. | `[F]` | `UPSTREAM_MAP.md` |
| E-011 | LoopX and OpenRath are protocol/design donors, not runtime dependencies or wholesale forks. | `[F]` | `REFERENCE_PORTFOLIO_23.md`, LoopX/OpenRath research reports, dependency manifests |
| E-012 | Globex is currently a local tutorial/reference corpus and has not been deeply integrated into Aidison code or architecture. | `[F]` | no Globex imports/dependencies; local corpus under `cankao_ws/完整学习项目/globex电商采购助手` |
| E-013 | Globex is a useful future donor for product search, price comparison, shipping, evaluation and commerce-Agent UX, but its claims require source/fixture validation before adoption. | `[J]` | local tutorial titles; no executable integration evidence yet |
| E-014 | PostgreSQL 17 and 18.4 fresh migrations and the C5 integration suite passed; unit suite reported 155 passed. | `[F]` | `docs/STATUS.md`, C5 commit history and release verification notes |
| E-015 | Repeated four-rotor live-provider execution is not stable; exact provider billing equality is not checked. | `[F]` | `docs/STATUS.md` open work |
| E-016 | The current approval resolver is a local single-user control plane, not IAM/RBAC or multi-tenant authorization. | `[F]` | ADR-0006 |

## Architecture judgment

`[J]` Aidison's differentiator is not “calling multiple LLMs”. It is the separation of:

1. immutable business truth and human decisions;
2. durable execution facts and recovery receipts;
3. bounded Agent reasoning that can only produce typed proposals;
4. explicit effect approval before irreversible external actions.

This separation is justified by crash recovery, replay, audit and stale-write tests. It costs more schema and state-machine work than a demo-grade in-process graph.

## Reference lineage

| Reference | Degree | What was used | What was not used |
|---|---|---|---|
| Deep Agents Core | deep, owned source snapshot | leaf Agent graph, middleware, backends, harness | canonical Job/plan/domain truth |
| deep-agents-ui | deep UI lineage | rendering and interaction primitives | chat/thread-first product ownership |
| LangGraph | medium, library | leaf graph execution and structured Agent output | top-level durable scheduler |
| OpenRath | medium design/protocol donor | lease/fencing, effect ledger, interrupt/event-cursor ideas | v1/v2 dual runtime and whole repository |
| LoopX | medium protocol donor | generation/CAS/writeback/ack thinking | daemon, tmux workflow and file-based truth |
| Globex | shallow/pending | commerce problem map and learning topics | production code, Taobao connector, proven algorithms |
| Tavily/GitHub MCP | direct integrations | bounded external search/source tools | unrestricted Agent access |

## Claims that must not appear on a résumé yet

- `[X]` “The system is production-ready.”
- `[X]` “Aidison supports real Taobao ordering.”
- `[X]` “Aidison invented or copied the OpenRath/LoopX runtime.”
- `[X]` “LangGraph is the canonical top-level orchestrator.”
- `[X]` “The Agent autonomously purchases products without human approval.”
- `[X]` “Live provider runs are deterministic and exact billing is reconciled.”

## Next evidence needed

- `[H]` A real, callable Taobao keyword-search API or approved MCP contract. `item.info.get` only accepts existing item IDs and cannot satisfy discovery by itself.
- `[H]` Authenticated actor/RBAC binding for approval resolution.
- `[H]` Provider-side usage reconciliation and repeatable live end-to-end gate.
- `[H]` A second non-Research workflow using multi-wave replan before expanding into a generic DAG scheduler.
- `[H]` Focused executable audit of Globex examples/fixtures before borrowing retrieval or evaluation claims.
