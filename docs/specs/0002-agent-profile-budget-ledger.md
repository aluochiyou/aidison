# AgentProfile revision and durable budget ledger

Status: accepted for P0 implementation  
Owner: controller (`rick`)  
Depends on: ADR-0001, `CONTEXT.md`, minimal closed loop specification

## Problem

The runtime currently stores `profile_id`, `profile_revision`, token budget, and tool-call
budget on Jobs/Delegations, but it does not persist the profile definition and does not enforce
or settle those budgets. A restarted worker therefore cannot prove which prompt and policy it
used, and concurrent/replayed calls can spend beyond a declared cap.

## P0 boundary

P0 adds these facts to the existing PostgreSQL runtime:

1. Immutable `AgentProfileRevision` definitions plus an active pointer used only when a new
   root run is created.
2. A frozen root-run profile manifest so later active-pointer changes cannot affect replay.
3. One root `BudgetAccount`, child/parent allocations, and an append-only operation ledger for
   every physical model or tool invocation.
4. Durable reservation before an external call, settlement after a response, and conservative
   full charging when a dispatched call has an unknowable outcome.

P0 does not add Redis, Celery, another scheduler, another Agent runtime, price/currency billing,
provider-side reconciliation, or a profile-management UI. PostgreSQL remains the only balance
and runtime fact source. Agents remain proposal-only.

## Profile contract

An `AgentProfileRevision` is identified by `(profile_id, revision)` and contains its complete
replayable definition: purpose, prompt text and hash, input/output schema references, permitted
tools/effects, memory scopes, model requirements, caps, timeout/retry/evaluator policy, and a
canonical definition hash. Reusing the same identity with different content fails closed.

Profile revisions cannot be updated or deleted after insertion. The mutable active pointer is
resolved only while creating a new root Job. The root Job freezes all role bindings in a
`job_profile_bindings` manifest. Child Jobs and Delegations reference a revision from that
manifest; retry and reclaim never consult the active pointer.

The deterministic research orchestrator is a runtime role, not a model-calling Agent profile.
The first built-in Agent profile is `research-worker-ro`. Both research shards use the same
revision; shard identity belongs to the Delegation, not to the Profile.

## Budget contract

Each root Job owns one `BudgetAccount`. Its caps cover all current and future generations,
waves, child retries, and model/tool operations. A child Delegation receives one idempotent
allocation; reclaiming the child or creating another Attempt never resets that allocation.

The lock order is account, allocation, operation. Account committed values include consumed
usage plus grants that have not been released. Allocation counters must always satisfy:

```text
0 <= consumed + reserved <= grant
0 <= account committed <= account cap
```

Each physical external call has a unique stable operation key. Lifecycle:

```text
reserved -> dispatched -> settled
reserved -> released
dispatched -> ambiguous
```

- Reservation is committed before network dispatch.
- Duplicate reserve/dispatch/settle returns the original operation and never charges twice.
- A tool dispatch consumes one tool call even when the remote call fails.
- A model operation reserves a conservative input/output token upper bound. Successful usage
  settles to actual provider-reported tokens and releases the unused portion.
- If a call may have been dispatched but no trustworthy usage result exists, it becomes
  `ambiguous` and consumes its complete reservation. It is not transparently replayed.
- Cancellation rejects new operations. Already-dispatched operations can still settle or become
  ambiguous and are not refunded.
- Stale-generation results remain quarantined by the existing runtime, but usage already incurred
  is still settled. Result acceptance and budget accounting are independent decisions.

Provider SDK hidden retry is disabled (`max_retries=0`). Any retry is a new physical operation
against the same allocation and therefore the same root cap.

## Migration

The migration creates profile, binding, account, allocation, and operation tables without
deleting compatibility columns. It seeds current built-in and legacy profile identities before
adding composite foreign keys. Historical Jobs receive an explicit `legacy_unknown` budget
state; the migration never invents zero usage. Non-terminal deployed Jobs must be drained or
cancelled before production migration because their already-issued external calls cannot be
reconstructed safely.

## Acceptance

P0 is accepted only when PostgreSQL tests prove all of the following:

- profile revision UPDATE/DELETE is rejected;
- changing an active pointer affects a new Job but not an existing Job or its replay;
- the same profile identity with different definition content fails closed;
- concurrent allocation/reservation cannot overspend a root or child cap;
- delegation-wave replay returns the original allocation without adding committed budget;
- reclaim/retry consumes the original child allocation;
- duplicate operation reserve/settle charges once;
- with one tool call remaining, concurrent tool reservations allow at most one dispatch;
- stale claim identity cannot create a new operation;
- crash before dispatch can release a reservation;
- crash after dispatch is conservatively charged and is not automatically retried;
- provider success settles reported input/output usage;
- existing JoinReceipt, Domain receipt, result quarantine, and project revision tests remain green;
- Alembic detects no model/schema drift, Ruff and strict mypy pass.

Real provider reconciliation and exact vendor-bill equality remain `not_checked` in P0. The hard
guarantee is no local cap oversell under the stated reservation upper bounds.
