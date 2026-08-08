# C5 Read-only Audit: Smallest Real Scoped Approval Gate (main@4b0b1d5)

Status: not_checked (read-only audit, no code executed)

## 1. Business DecisionRequest vs execution-effect approval

Business decision already exists and is healthy: `DecisionRequest` (domain/models.py:187-242) with `DecisionStatus` pending/approved/rejected/expired, frozen `basis_hash`, immutable options, resolution validator; resolved via `resolve_decision` (application/service.py:315-375), route app.py:376-410. It answers "what to build".

The gap is **execution-effect approval**: no durable record authorizes an agent/runtime side effect. V0 hard-forbids canonical writes at the contract (`DelegationSpec.enforce_v0_write_boundary`, runtime/contracts.py:159-163) and profiles only grant `allowed_effects=("discovery","read")` (agents/profiles.py:100,143,171,211,253). Nothing can ever flip a scoped write or external effect on.

## 2. Where real external side effects happen

`ShoppingApplication.create_checkout_handoff` (application/shopping.py:452-599) is the only external side effect besides search: it calls the provider `create_cart`. Today it is gated only by `status is READY` (shopping.py:485), recomputed basis (492-503), and offer not expired (514-516); the `checkout.handoff_created` event (589) fires immediately. Idempotency is correct (PREPARED+receipt committed before provider call, 527-532). Missing: an expiring, scoped, resolvable approval that separates "READY" from "user authorized the provider call".

## 3. Proposal — one durable `EffectApproval`

- **State**: `requested` → `approved | denied | expired` (terminal; no reopen). TTL via `expires_at`; lazy expiry check mirrors shopping.py:409-410.
- **Exact scope binding**: `effect_kind` (`checkout_handoff`, later `canonical_write`) + `target_ref` (proposal_id) + frozen `basis_hash` + `project_revision` CAS.
- **Who resolves**: the single local user (local-first; no RBAC), via idempotent `POST` + `If-Match`; deny requires `reason`.
- **Where it blocks/resumes**: in the application service, before the provider side effect — `create_checkout_handoff` raises 409 until an unexpired approved approval exists; retry of the same idempotency key resumes after approval. No new runtime job (parent already completes after join; DecisionRequest→solution_wave job creation is app.py:393-404).
- **Replay/fencing**: Idempotency-Key + `CommandReceiptRow` + terminal-state guard (mirror service.py:331-342, decision already resolved guard 341-342).
- **Adopter**: PurchaseProposal checkout — `confirm_proposal_lines` auto-requests approval on READY (shopping.py:422-427); handoff requires it.

## 4. Minimal implementation scope (~9 files)

1. domain/models.py: `EffectApprovalStatus` + `EffectApproval` (requested_at/expires_at/resolved_at/resolved_by/reason validator like models.py:232-242).
2. infrastructure/orm.py: `EffectApprovalRow` (status CHECK, basis_hash, payload JSONB, expires_at, resolved_by).
3. migrations/versions/<new>: `effect_approvals` (pattern: 8a1f3c5e7b92).
4. application/ports.py + infrastructure/store.py: add/get/update (mirror decision methods store.py:325-364).
5. application/service.py: `resolve_effect_approval` (mirror resolve_decision 315-375).
6. application/shopping.py: request approval in confirm; gate create_checkout_handoff.
7. api/schemas.py `ResolveEffectApprovalRequest`; api/app.py route (mirror app.py:376-410).
8. Tests: unit test_domain_contracts + test_application_closed_loop; integration test_api_closed_loop.
9. Docs: docs/ARCHITECTURE.md remaining-work line 90 already names "扩展 scoped approval gate"; update it.

Explicitly out of scope: generic policy engines, RBAC/multi-tenant, Dapr-style workflow engines.

## 5. Security failure cases (each has an in-repo precedent to copy)

- stale-basis replay → If-Match + recomputed basis_hash (shopping.py:397-403)
- double-resolve → command receipt + terminal guard (service.py:331-342)
- approve-after-expiry → `expires_at < now` (shopping.py:409-410)
- cross-project approval target → project_id equality (app.py:820-821)
- wrong-effect authorization → effect_kind/target_ref/basis binding validated on stored payload
- denied then unexpire → immutable terminal status (domain validator)

PostgreSQL remains the authority; the audit changed no code.
