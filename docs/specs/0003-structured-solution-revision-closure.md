# Structured solution and revision closure

Status: accepted for P0 implementation  
Owner: controller (`rick`)  
Depends on: ADR-0001, ADR-0002, `CONTEXT.md`, specifications 0001 and 0002

## Problem

The current API and browser can author arbitrary `module_snapshots`, BOM, implementation steps,
verification steps and module patches. The database persists immutable versions and command
receipts, but it cannot prove that a frozen Solution came from accepted Agent evidence, the
approved Decision basis or a reviewed Impact proposal. Module dependencies exist in the Domain
model but cannot be expressed through the requirements API, so Observation impact is only the
user's direct hint rather than a deterministic dependency closure.

This is a product-semantic gap, not a storage gap. The next milestone makes SolutionVersion the
real core asset without adding another runtime or a domain-specific Drone schema.

## P0 outcome

```text
Research Decision
→ user selects one option on its exact basis
→ durable solution-proposer Job
→ staged typed SolutionProposal Artifact
→ Domain accepts a reviewable SolutionProposal
→ user approves proposal ID + basis only
→ server freezes immutable SolutionVersion v1
→ user submits Observation + direct-impact hints
→ durable impact-proposer Job
→ deterministic Module dependency closure
→ Domain accepts ImpactAnalysis with proposed PatchSet
→ user approves impact ID + basis only
→ server creates PatchSet and immutable SolutionVersion v2
→ UI shows affected/reused semantic diff
```

The same contracts must support a four-rotor fixture and at least one non-drone fixture. Module
keys and engineering data remain project data; no `Drone*` entity, prompt branch or route is
allowed.

## Canonical contracts

### Module graph

Requirements accept `dependency_keys`. The application resolves them to `dependency_ids` only
after all Module IDs exist. Unknown keys, self-dependencies and cycles fail closed. For an
Observation, directly affected modules are expanded to every module that transitively depends on
them. The deterministic closure is authoritative even when an Agent proposes a smaller set.

### SolutionProposal

`SolutionProposal` is a typed, reviewable canonical proposal, not an approved Solution. It binds:

- project, approved Decision, RequirementRevision and exact Decision basis;
- exactly one selected Candidate per required Module;
- supporting Evidence and CompatibilityFinding references;
- typed BOM items with quantity, unit and source Candidate/Evidence references;
- typed implementation and verification steps with module scope;
- explicit risks, unknowns and expected consequences;
- the staged Artifact and AgentProfile revision that produced it.

Cross-project refs, missing modules, duplicate selections, incompatible findings and unknown
references fail closed. `unknown` and `needs_test` findings remain explicit and must appear in the
verification plan.

### Solution approval

The browser submits only `solution_proposal_id` and `basis_hash` plus command headers. The server
copies the accepted proposal into a new immutable `SolutionVersion`; it never accepts browser-
authored BOM, module snapshots or steps. Replaying the approval returns the same version.

### ImpactAnalysis and PatchSet

Observation records user facts and direct-impact hints, then queues one durable impact proposal.
The Agent may propose changes only inside the deterministic dependency closure. The Domain stores
direct, transitive, affected, unaffected and stale-evidence sets separately.

Impact approval submits only the impact ID and basis. The server freezes the already accepted
proposed changes into a PatchSet. A patch outside the affected closure fails closed. The next
SolutionVersion changes only affected snapshots/BOM/steps; unaffected module snapshot hashes and
references are preserved exactly.

## Agent and runtime contract

- Add immutable `solution-proposer` and `impact-proposer` Profile revisions.
- Both are proposal-only and use the existing PostgreSQL Job/Attempt/Delegation/JoinReceipt,
  binding manifest, BudgetAccount/allocation/operation ledger, Artifact store, lease and fencing.
- Each stage has one durable child; no native Deep Agents subagent, recursive Agent, checkpoint
  truth or second scheduler is introduced.
- Solution proposer may read accepted project Evidence/Candidates/Findings but has no external
  side effects. Impact proposer reads the frozen Solution, Observation and module graph; web search
  is not enabled in P0.
- A child result is staged first. Only an eligible result and committed JoinReceipt can invoke the
  corresponding idempotent Domain command.
- Reclaim and replay reuse frozen Profile revisions and root budgets. Stale results are
  quarantined; model usage is still settled independently.

## API and UI boundary

- `ModuleInput` adds `dependency_keys`.
- Decision resolution uses a stable option ID and basis hash.
- Add start/query projection for solution and impact proposal Jobs through the existing snapshot
  and event stream; do not add a second task API.
- Solution approval accepts only proposal identity and basis.
- Impact approval accepts only impact identity and basis.
- The Project Console displays candidate/evidence bindings, BOM, implementation and bench
  verification plans, risks/unknowns, direct/transitive impact, and v1→v2 affected/reused diff.
- Never display chain-of-thought, full prompts or secrets.

## Migration and compatibility

- Add canonical storage only for facts that cannot be represented by the existing JSONB rows;
  prefer typed payload plus indexed identity/basis/status columns.
- Seed the two new Profile revisions in a new Alembic revision. Historical Decision/Solution rows
  remain readable as `legacy_unbound`; they cannot be used for a new strict approval or patch.
- Do not rewrite or invent bindings for historical browser-authored Solution payloads.
- No destructive table/column removal is part of P0.

## Acceptance matrix

- unknown dependency key, self-edge and cycle are rejected;
- direct impact expands to the full deterministic dependent closure;
- proposal references to another project or missing Candidate/Evidence/Module are rejected;
- every required Module has exactly one selection;
- `incompatible` blocks approval; `unknown/needs_test` remain visible in verification;
- browser-authored BOM/steps/patch fields are rejected by the API;
- stale Decision, SolutionProposal and Impact basis return precondition failure;
- solution/impact Agent calls use frozen Profile revisions and durable budget operations;
- crash/reclaim yields one accepted result and one JoinReceipt per proposal stage;
- duplicate approval returns the original receipt and does not create another version;
- database rejects UPDATE/DELETE of SolutionVersion;
- patches cannot touch unaffected modules;
- v2 preserves unaffected module snapshot identity/hash and points to v1;
- four-rotor and non-drone fixtures use the same Domain/Profile/Graph contracts;
- refresh and cursor SSE restore proposal, approval, budget and semantic diff state;
- isolated PostgreSQL non-live suite, Alembic drift check, Ruff, strict mypy, frontend lint/build
  and browser E2E pass.

## Deferred

- automatic shopping/payment, public A2A, device control, RBAC/multi-tenant;
- LangSmith/Langfuse beyond an optional disposable trace sink;
- price freshness, offer normalization and purchase reconciliation;
- provider-side usage reconciliation and exact vendor-bill equality;
- dynamic swarm, recursive proposal Agents or a second durable runtime.

The milestone is not complete when only the typed models or UI cards exist. Completion requires
the durable proposal jobs, server-owned approvals, deterministic impact closure, v2 reuse and
browser-visible four-rotor journey to be verified together.
