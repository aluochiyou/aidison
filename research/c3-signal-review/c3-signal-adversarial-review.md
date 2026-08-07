# Aidison Slice C3 — Signal Wake & Join State Machine Adversarial Review

- Date: 2026-08-08
- Base: `main` @ `f8a511b` (child worktree `slice-c3-signal-review`)
- Role: OpenCode independent read-only reviewer
- Scope: busy polling, join state machine, durable signal design, failure modes,
  PostgreSQL locking / LISTEN-NOTIFY portability, asyncpg pooling, lost wakeups,
  dedupe, generation fencing. Decide the signal mechanism. No Redis authority.
- Verification: `passed` = ran against real PostgreSQL; `failed` = reproducible
  failure; `not_checked` = static analysis only.

## 1. Executive summary

Aidison already has a correct, durable join/fencing core: `PostgresRuntime`
serializes join transitions with row locks, deterministic result hashes, and
lease/generation fencing. The only real inefficiency is **busy polling**:
the worker claims every 0.5 s (`run_forever` + `_poll_seconds`), and each
parent waits for its join by `inspect_join()` + `asyncio.sleep(0.5)` in four
places (`research.py:1101`, `1385`, `1696`, `2114`). The SSE stream polls the
DB every 1 s (`app.py:776`).

Recommendation: **keep PostgreSQL as the sole authority, add a thin
best-effort wake layer.** Concretely:

1. Use PostgreSQL **LISTEN/NOTIFY** as a *cross-process, latency-only* wake
   accelerator (mirroring OpenRath's `signals.py` `GuardedSignalBus`), on a
   **dedicated, non-pooled** asyncpg connection — never on a pooled
   connection (empirically verified hazard below).
2. Keep the existing durable poll as the correctness backstop. Every wake
   handler must **re-read durable PG state** (LoopX/OpenRath model); the
   notification itself is never authoritative and carries no state.
3. Do **not** add a separate durable `signals` row table: the
   `domain_events` append-only log + `jobs`/`join_groups`/`delegations` rows
   already *are* the durable truth and the replay cursor. A second signal
   table would duplicate state and create a second source of truth, which
   ADR-0001 and the handoff explicitly forbid ("PostgreSQL 是唯一 authority").
4. Do **not** introduce Redis (already decided; no Redis in compose).

DeepAgents/raw LangGraph: **no change needed** in this slice. The signal/wake
layer lives entirely in Aidison's own `research.py` worker loop and a new thin
`SignalNotifier` in `infrastructure`. DeepAgents stays a bounded single-worker
harness; no vendored DeepAgents modification and no fallback to raw LangGraph
is required (consistent with Slice C2's KEEP verdict).

## 2. Verified evidence

- `passed` — `tests/integration/test_postgres_runtime.py` + `test_research_worker.py`:
  **17 passed** on real PostgreSQL 18 (isolated `aidison_test_opencode`, port 55433).
- `passed` — full unit+integration run: **163 passed, 1 failed**. The single
  failure is the pre-existing, non-C3 `test_api_closed_loop.py` ordering
  assertion on `budget_accounts[0].token_cap` (already documented in
  `implementation-report.md:146-148`); reproduces even on a freshly truncated
  DB, so it is a test-order artifact, not a C3 regression.
- `passed` — asyncpg `LISTEN/NOTIFY` live probes against real PG 18:
  - committed-transaction NOTIFY is delivered; **rolled-back NOTIFY is not**
    (transaction-atomic semantics — good).
  - notifications are broadcast to **all** listeners on the channel.
  - NOTIFY sent **before** `add_listener` is **lost** (no replay) →
    a LISTEN-only design without a poll fallback loses wakeups.
  - reaching the raw asyncpg connection through SQLAlchemy
    (`await (await session.connection()).get_raw_connection()).driver_connection`)
    works and `add_listener` fires.
  - **Pooling hazard (confirmed)**: a listener registered on a connection
    obtained from the shared engine pool *survives return to the pool* and the
    callback then fires during later, unrelated checkouts of the same physical
    connection. Pool = `AsyncAdaptedQueuePool`, size 5, overflow 10
    (defaults; only `pool_pre_ping=True` is set). A pooled LISTEN connection
    is therefore unsafe; LISTEN must live on a dedicated connection owned by a
    background task.

## 3. Reference-pattern comparison (LoopX / OpenRath)

| Aspect | OpenRath (Rath) | LoopX | Aidison today | Verdict for C3 |
|---|---|---|---|---|
| Durable source of truth | DB tables (`runs`, `run_events`, effects); `signals.py:1` "never a durable Run source of truth" | append-only JSONL state events + registry (`event_sourced_state.py`); markdown projection | PostgreSQL `jobs/attempts/delegations/join_groups/receipts` + `domain_events` | **Adopt unchanged** — PG is authority |
| Wake mechanism | `SignalBus` (in-mem / Redis list) purely as latency shim; worker still polls `work_once` every loop (`cli.py:118-131`) | no push at all: readiness scheduler polls `loopx todo list` at 2 s and pastes a tmux prompt to make agents **re-read state** (`visible_wake_scheduler.py:215-350`); wake is explicitly non-authoritative | pure busy poll (0.5 s claim, 0.5 s join, 1 s SSE) | **Adopt**: LISTEN/NOTIFY as latency shim + poll backstop; wake forces state re-read |
| Blocking / waits | lease heartbeat thread (`threading.Event.wait`); interrupts are durable rows, resume via claim poll | bounded `time.sleep` polls; waits encoded as `resume_when`/monitor cadence state, not blocking primitives | `asyncio.sleep(_poll_seconds)` loops + `inspect_join` | **Adopt**: event-wait with timeout backstop, never block forever |
| Side effects | durable idempotent effect ledger + `needs_review` for ambiguous (`effects.py:112-148`) | execution-obligation contract; effects decoupled from notify | effects live inside `register_result`/`commit_join` + budget ledger | out of scope for C3 (later slice) |
| Concurrency safety | optimistic version + fencing token; `SELECT..FOR UPDATE SKIP LOCKED` claim | file locks (`file_lock.py`) | `FOR UPDATE SKIP LOCKED` claim + lease/generation fencing | **Adopt unchanged** |

## 4. Join state machine review

The join machine is **correct and safe**:

- `create_delegation_wave` (runtime.py:197) freezes policy/expected IDs, is
  replay-idempotent against the existing group, and validates parent claim +
  basis before creating the wave (runtime.py:221-233).
- `register_result` (runtime.py:643) computes a deterministic result id
  `sha256(attempt_id + result_hash)` (runtime.py:84-86) → idempotent dedupe;
  quarantines stale/late results on generation/lease/basis/join-group checks
  (runtime.py:690-724). `AttemptResultRow` has `UNIQUE(attempt_id, result_hash)`
  (orm.py:771).
- `commit_join` (runtime.py:1030) is single-receipt: `JoinReceiptRow` PK =
  `join_group_id` (orm.py:752), so a group can commit at most one receipt, and
  a committed join is the durable recovery anchor (`find_committed_join`,
  runtime.py:954).
- Generation fencing: parent/child `current_generation`, `lease_token`,
  `claim_generation` are checked in every state transition; reclaim cancels
  open groups and stale children atomically in `claim_next_job` (runtime.py:427-524).

Blockers found in the state machine itself: **none** that are correctness
blockers. The inefficiency is polling, not safety.

## 5. Ranked blockers

### B1 (blocking) — busy polling scales poorly and adds latency, not just load
- Claim loop `run_forever` polls `claim_next_job` every `_poll_seconds` (0.5 s)
  (research.py:783); parent join waits poll `inspect_join` every 0.5 s in four
  loops (1101, 1385, 1696, 2114). SSE polls every 1 s (app.py:776).
- Impact: up to 0.5 s of added latency per child completion → join; each
  waiting parent issues a `SELECT .. FOR UPDATE` + delegations read per tick;
  an idle worker with no tasks wakes 2×/s and hits the DB. For a single-user
  local-first product this is tolerable, but it is the exact thing C3 must fix.
- Fix: LISTEN/NOTIFY wake + poll backstop; raise idle poll interval to ~2-5 s
  once a listener is healthy (OpenRath keeps DB poll every loop, LoopX uses 2 s).

### B2 (blocking) — LISTEN on a pooled asyncpg connection is unsafe (verified)
- Empirically shown: a listener registered on a pooled connection persists
  after checkin and its callback fires inside unrelated later checkouts of the
  same physical connection. This would deliver wake callbacks into the middle
  of unrelated SQL work on that connection.
- Fix: dedicated listener connection(s) owned by a background task per
  process (e.g., one `asyncpg`/engine connection kept open), NOT from the
  `AsyncAdaptedQueuePool`. The pool defaults are otherwise fine (size 5 /
  overflow 10, `pool_pre_ping`).

### B3 (blocking if signal is made authoritative) — lost-wakeup design constraint
- NOTIFY sent before a listener connects is silently dropped (verified), and
  a reconnecting listener misses notifications during the gap. Therefore the
  wake layer must **never** be the only path: every `wait` must carry a
  timeout and fall back to the durable poll (`inspect_join` / `claim_next_job`
  / `list_events`). This is exactly OpenRath's `GuardedSignalBus` ("best-effort,
  swallow failures, DB success preserved") and LoopX's "wake is not workflow".
- Fix: implement wake as `event.wait(timeout=backstop)`; on any notification
  or timeout, re-read PG state. Never `await event.wait()` unconditionally.

### B4 (should-fix) — `inspect_join` takes a write row lock while it is mostly read
- `inspect_join` runs `select(JoinGroupRow).with_for_update()` every poll
  (runtime.py:807), i.e., the read-only poll holds an exclusive row lock on the
  join group each 0.5 s and serializes with `register_result`'s own
  `with_for_update` on the same row (runtime.py:657). Lock contention is brief
  and single-row, but it couples the poll cadence to the join writer.
- Fix: keep `FOR UPDATE` only inside transitions (`commit_join`,
  `register_result`, reclaim); make the readiness snapshot read with
  `READ COMMITTED` and no lock, taking the lock only when transitioning to
  JOINED/FAILED. Acceptance test: concurrent `inspect_join` + `register_result`
  never blocks/retries due to lock wait.

### B5 (should-fix) — SSE re-polls `domain_events` every 1 s per open console
- `stream_events` polls `list_events` each 1 s (app.py:756-776); the frontend
  additionally keeps a 1.5 s cursor poll as fallback. Same class of wake
  problem, lower stakes. Optional: reuse the same LISTEN/NOTIFY channel to
  wake SSE only when the project's `event_sequence` advances; keep the 1 s poll
  as backstop.

### B6 (watch) — multi-worker claim fairness / SKIP LOCKED starvation
- With >1 worker process, `FOR UPDATE SKIP LOCKED` (runtime.py:420) can in
  principle let a hot worker starve short-lived claims; single-worker compose
  makes this moot today. Keep as a watch item; add a stress test if multi-worker
  is ever deployed. Not a blocker.

## 6. Design decision

**Pattern: PostgreSQL LISTEN/NOTIFY as a best-effort, cross-process wake
accelerator over the existing durable DB poll.** Rationale:

- No new durable store. `domain_events` (append-only, `project_seq` cursor)
  is the durable signal log and replay cursor; `jobs/join_groups/delegations`
  are the durable state. A separate "durable signal rows" table would be a
  second source of truth and is **rejected** (violates ADR-0001 / handoff).
- No Redis (compose has none; handoff says Redis/SSE/notifications only wake).
- LISTEN/NOTIFY is transaction-atomic with commit (verified: rollback →
  no notify), broadcast, no payload-size concern for a `job_id`/`join_group_id`
  hint, and requires no schema migration.
- Its weakness (no replay, lost on reconnect) is exactly covered by the
  existing durable poll, which stays as the backstop. This is the same
  "DB-poll authoritative + best-effort signal to cut latency" architecture
  OpenRath ships and the "notify only wakes; worker must re-read PG ready
  frontier" rule Aidison already wrote down in `architecture.md` §4.3/4.4 and
  `handoff.md`.

Recommended minimal surface:

- New `infrastructure/signals.py`: `SignalNotifier` protocol with
  - `InProcessSignalBus` (asyncio.Queue / `asyncio.Event`) for tests + the
    in-process fast path;
  - `PostgresNotifyBus`: one dedicated asyncpg/engine connection per process,
    `add_listener` on `aidison_signal`, background reader task that pushes to
    an internal queue; `publish` = `SELECT pg_notify('aidison_signal', hint)`
    in the same transaction as the state change (so commit atomicity holds);
    `GuardedSignalBus` wrapper that swallows transport failures (OpenRath
    parity).
- Wiring:
  - `register_result`, `commit_join`, `create_delegation_wave`, `claim_next_job`
    publish a hint after the DB commit (same tx or right after).
  - Parent join loops replace `asyncio.sleep(poll_seconds)` with
    `await wake_event.wait(timeout=backstop)` then `inspect_join` (keep the
    deadline/`impossible` handling).
  - `run_forever` idle path uses the same event wait instead of a bare sleep.
  - SSE (`stream_events`) optionally subscribes to the same bus, keyed by
    `project_seq`, keeping the 1 s poll.
- The notifier is **never** the source of truth and never carries policy/
  payload; every handler re-reads durable state.

## 7. Acceptance tests (ranked; each must run on isolated real PG)

- AT1 (lost-wakeup safety): with the notifier disabled or with a notification
  dropped before LISTEN (simulate by not registering the listener), a queued
  job is still claimed and a join still completes within the backstop timeout.
  Assert no unbounded wait.
- AT2 (pooled-connection isolation): a LISTEN bus never shares a pooled
  connection — assert that after checkin, no unrelated session's callback
  fires (regression for B2).
- AT3 (rollback atomicity): a rolled-back `register_result`/`commit_join`
  produces **no** notification and no wake (verified primitive → regression
  test).
- AT4 (wake reduces latency): with a healthy listener, a child result that
  completes the join wakes the waiting parent in well under the poll interval
  (e.g., < 200 ms), and the parent re-reads state from PG before acting.
- AT5 (poll backstop under reconnect): kill/restart the listener connection
  mid-join; the parent still completes via the backstop poll; no double
  receipt, no lost result.
- AT6 (dedupe preserved): duplicate `register_result` returns the same
  `result_id` and creates no second row; `commit_join` still yields exactly one
  `JoinReceiptRow` (existing tests + wake-path variant).
- AT7 (fencing preserved): a late/stale child result after a reclaim is still
  quarantined even when a wake fires; generation mismatch never joins.
- AT8 (no lock amplification): concurrent `inspect_join` and `register_result`
  on the same join group complete without blocking retries (B4 fix);
  contention window < poll interval.
- AT9 (SSE cursor correctness): after a wake, `stream_events` emits exactly the
  events after the client cursor once (no duplicates, no gaps) with the 1 s
  poll as backstop.

## 8. DeepAgents vs raw LangGraph

**No change required.** The wake/signal layer is purely Aidison-owned code in
`application/research.py` + a new `infrastructure/signals.py`. DeepAgents
remains a bounded, single-worker harness (`create_deep_agent` with native
subagents disabled, no checkpointer/store — `agents/research.py:176-185`);
Slice C2's independent review already returned KEEP
(`implementation-report.md:126-127`). No vendored DeepAgents edit and no
migration back to raw LangGraph is needed for C3. Raw LangGraph would be a
*regression*: it would move the truth back into process-local graph state,
contradicting ADR-0001's "PostgreSQL is the authority" invariant.

## 9. Out of scope / not_checked

- `not_checked`: multi-worker (2+) live deployment; LISTEN/NOTIFY under actual
  container restart; broker-free wake latency under load.
- `failed` (pre-existing, non-C3): `test_api_closed_loop.py`
  `budget_accounts[0].token_cap` ordering assertion.
- Not in this slice: durable `AgentMessage`, scoped gates, HITL/effect
  `needs_review`, `BOUNDED_PARTIAL`/`FIRST_VALID` join modes (later slices).
- No changes to `packages/deepagents`, ORM, migrations, or compose were
  proposed or made.
