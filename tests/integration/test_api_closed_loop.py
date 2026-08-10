from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from aidison.api.app import create_app
from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    BomItem,
    ModulePatch,
    ModuleSelection,
    SolutionPlanStep,
)
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.operations.fixture import (
    SCENARIO_MAX_TOTAL,
    SCENARIO_OFFER,
    SCENARIO_SEARCH_QUERY,
    FakeShoppingProvider,
)
from aidison.providers.shopping import ShoppingProviderError

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_http_closed_loop_etag_idempotency_errors_and_cursor_replay() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    fake_provider = FakeShoppingProvider()
    api = create_app(factory, shopping_provider=fake_provider)
    transport = httpx.ASGITransport(app=api)
    key_prefix = str(uuid4())
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            assert health.json() == {"status": "ok"}

            missing_header = await client.post(
                "/api/projects",
                json={"name": "Fixture", "goal": "Must require idempotency"},
            )
            assert missing_header.status_code == 422
            assert missing_header.json()["error"]["code"] == "validation_error"

            project_payload = {
                "name": "DIY quadcopter",
                "goal": "Build and verify a repairable quadcopter",
            }
            created = await client.post(
                "/api/projects",
                json=project_payload,
                headers={"Idempotency-Key": f"{key_prefix}:project"},
            )
            assert created.status_code == 201
            assert created.headers["etag"] == '"1"'
            project = created.json()
            project_id = project["id"]

            listed = await client.get("/api/projects")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()] == [project_id]

            replay = await client.post(
                "/api/projects",
                json=project_payload,
                headers={"Idempotency-Key": f"{key_prefix}:project"},
            )
            assert replay.json()["id"] == project_id
            conflict = await client.post(
                "/api/projects",
                json={"name": "Different", "goal": "Same key must fail"},
                headers={"Idempotency-Key": f"{key_prefix}:project"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "idempotency_conflict"

            requirements_payload = {
                "goal": project_payload["goal"],
                "hard_constraints": ["No autonomous flight in V0"],
                "preferences": ["Repairable"],
                "available_resources": ["Soldering iron"],
                "unknowns": ["Motor availability"],
                "modules": [
                    {
                        "key": "frame",
                        "name": "Frame",
                        "responsibility": "Carry components",
                    },
                    {
                        "key": "propulsion",
                        "name": "Propulsion",
                        "responsibility": "Generate thrust",
                        "dependency_keys": ["frame"],
                    },
                ],
            }
            stale = await client.post(
                f"/api/projects/{project_id}/requirements",
                json=requirements_payload,
                headers={
                    "Idempotency-Key": f"{key_prefix}:requirements-stale",
                    "If-Match": '"99"',
                },
            )
            assert stale.status_code == 412
            assert stale.json()["error"]["code"] == "precondition_failed"

            requirements = await client.post(
                f"/api/projects/{project_id}/requirements",
                json=requirements_payload,
                headers={
                    "Idempotency-Key": f"{key_prefix}:requirements",
                    "If-Match": 'W/"1"',
                },
            )
            assert requirements.status_code == 200
            assert requirements.headers["etag"] == '"2"'
            modules = requirements.json()["modules"]

            started = await client.post(
                f"/api/projects/{project_id}/research-runs",
                headers={
                    "Idempotency-Key": f"{key_prefix}:research-run",
                    "If-Match": '"2"',
                },
            )
            assert started.status_code == 202
            assert started.headers["etag"] == '"2"'
            replayed_run = await client.post(
                f"/api/projects/{project_id}/research-runs",
                headers={
                    "Idempotency-Key": f"{key_prefix}:research-run",
                    "If-Match": '"2"',
                },
            )
            assert replayed_run.status_code == 202
            assert replayed_run.json()["job_id"] == started.json()["job_id"]

            evidence_id = str(uuid4())
            candidate_id = str(uuid4())
            alternate_candidate_id = str(uuid4())
            propulsion_candidate_id = str(uuid4())
            finding_id = str(uuid4())
            evidence = {
                "id": evidence_id,
                "project_id": project_id,
                "module_id": modules[0]["id"],
                "claim": "The frame interface dimensions are documented.",
                "source_url": "https://example.com/frame",
                "snapshot_hash": sha256(b"frame").hexdigest(),
                "span_text": "Mount pattern 30.5 x 30.5 mm",
                "status": "supported",
                "applicability": [],
                "observed_at": datetime.now(UTC).isoformat(),
            }
            candidate = {
                "id": candidate_id,
                "project_id": project_id,
                "module_id": modules[0]["id"],
                "name": "Open frame",
                "description": "A documented repairable frame",
                "attributes": {},
                "evidence_binding_ids": [evidence_id],
                "risks": [],
            }
            alternate_candidate = {
                "id": alternate_candidate_id,
                "project_id": project_id,
                "module_id": modules[0]["id"],
                "name": "Reinforced frame",
                "description": "A documented alternate frame for measured loads",
                "attributes": {},
                "evidence_binding_ids": [evidence_id],
                "risks": ["Bench load verification required"],
            }
            propulsion_candidate = {
                "id": propulsion_candidate_id,
                "project_id": project_id,
                "module_id": modules[1]["id"],
                "name": "Documented propulsion",
                "description": "A propulsion route with documented mounting interfaces",
                "attributes": {},
                "evidence_binding_ids": [evidence_id],
                "risks": [],
            }
            finding = {
                "id": finding_id,
                "project_id": project_id,
                "module_ids": [item["id"] for item in modules],
                "rule_id": "generic.interface.documented",
                "status": "compatible",
                "summary": "Documented interfaces match.",
                "evidence_binding_ids": [evidence_id],
                "required_test": None,
            }
            legacy_research = await client.post(
                f"/api/projects/{project_id}/research-proposals",
                json={
                    "evidence": [evidence],
                    "candidates": [candidate, alternate_candidate, propulsion_candidate],
                    "findings": [finding],
                    "decision_question": "Freeze this route?",
                    "decision_options": ["approve", "reject"],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:legacy-research",
                    "If-Match": '"2"',
                },
            )
            assert legacy_research.status_code == 422

            research = await client.post(
                f"/api/projects/{project_id}/research-proposals",
                json={
                    "evidence": [evidence],
                    "candidates": [candidate, alternate_candidate, propulsion_candidate],
                    "findings": [finding],
                    "decision_question": "Freeze this route?",
                    "decision_options": [
                        {
                            "option_id": "approve",
                            "label": "Use documented route",
                            "summary": (
                                "Select the open frame and documented propulsion candidates."
                            ),
                            "candidate_ids": [candidate_id, propulsion_candidate_id],
                            "evidence_binding_ids": [evidence_id],
                            "risks": [],
                            "legacy_unbound": False,
                        },
                        {
                            "option_id": "alternate",
                            "label": "Use reinforced frame",
                            "summary": (
                                "Select the reinforced frame and documented propulsion candidates."
                            ),
                            "candidate_ids": [alternate_candidate_id, propulsion_candidate_id],
                            "evidence_binding_ids": [evidence_id],
                            "risks": ["Bench load verification required"],
                            "legacy_unbound": False,
                        },
                    ],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:research",
                    "If-Match": '"2"',
                },
            )
            assert research.status_code == 200
            decision = research.json()["decision"]

            legacy_resolve = await client.post(
                f"/api/decisions/{decision['id']}/resolve",
                json={"selected_option": "approve", "basis_hash": decision["basis_hash"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:legacy-decision",
                    "If-Match": '"3"',
                },
            )
            assert legacy_resolve.status_code == 422

            resolved = await client.post(
                f"/api/decisions/{decision['id']}/resolve",
                json={"selected_option_id": "approve", "basis_hash": decision["basis_hash"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:decision",
                    "If-Match": '"3"',
                },
            )
            assert resolved.status_code == 200
            assert resolved.json()["decision"]["status"] == "approved"

            async with factory() as session:
                proposal = await ProjectApplication(
                    PostgresDomainStore(session)
                ).submit_solution_proposal(
                    project_id=UUID(project["id"]),
                    expected_project_revision=4,
                    decision_id=UUID(decision["id"]),
                    module_selections=tuple(
                        ModuleSelection(
                            module_id=item["id"],
                            candidate_id=(candidate_id if index == 0 else propulsion_candidate_id),
                            candidate_name=(
                                "Open frame" if index == 0 else "Documented propulsion"
                            ),
                            rationale="Use the accepted evidence-backed route.",
                            evidence_binding_ids=(evidence_id,),
                        )
                        for index, item in enumerate(modules)
                    ),
                    evidence_binding_ids=(UUID(evidence_id),),
                    compatibility_finding_ids=(UUID(finding_id),),
                    bom=(
                        BomItem(
                            line_id="open-frame",
                            module_id=modules[0]["id"],
                            candidate_id=candidate_id,
                            name="Open frame",
                            quantity=1,
                            unit="piece",
                            evidence_binding_ids=(evidence_id,),
                        ),
                    ),
                    implementation_steps=(
                        SolutionPlanStep(
                            step_id="assemble",
                            title="Assemble",
                            instruction="Assemble the accepted modules.",
                            module_ids=tuple(item["id"] for item in modules),
                        ),
                    ),
                    verification_steps=(
                        SolutionPlanStep(
                            step_id="bench-test",
                            title="Bench test",
                            instruction="Verify interfaces before operation.",
                            module_ids=tuple(item["id"] for item in modules),
                        ),
                    ),
                    risks=(),
                    unknowns=(),
                    consequences=("The route remains repairable.",),
                    artifact_ref="artifact://api-solution-proposal",
                    profile_id="solution-proposer",
                    profile_revision=1,
                    idempotency_key=f"{key_prefix}:solution-proposal",
                )

            browser_authored_solution = await client.post(
                f"/api/projects/{project_id}/solutions",
                json={
                    "decision_id": decision["id"],
                    "module_snapshots": [{"module_id": modules[0]["id"], "selection": "fake"}],
                    "bom": [{"item": "fake", "quantity": 1}],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:browser-authored-solution",
                    "If-Match": '"5"',
                },
            )
            assert browser_authored_solution.status_code == 422

            solution = await client.post(
                f"/api/projects/{project_id}/solutions",
                json={
                    "solution_proposal_id": str(proposal.id),
                    "basis_hash": proposal.basis_hash,
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:solution",
                    "If-Match": '"5"',
                },
            )
            assert solution.status_code == 200
            assert solution.json()["solution"]["version"] == 1
            base_solution = solution.json()["solution"]

            observation = await client.post(
                f"/api/projects/{project_id}/observations",
                json={
                    "statement": "The frame flexes under load.",
                    "affected_module_ids": [modules[0]["id"]],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:observation",
                    "If-Match": '"6"',
                },
            )
            assert observation.status_code == 200
            assert observation.json()["impact_job_id"]

            frame_snapshot = next(
                item
                for item in base_solution["module_snapshots"]
                if item["module_id"] == modules[0]["id"]
            )
            async with factory() as session:
                impact = await ProjectApplication(
                    PostgresDomainStore(session)
                ).submit_impact_analysis(
                    project_id=UUID(project_id),
                    expected_project_revision=7,
                    observation_id=UUID(observation.json()["observation"]["id"]),
                    module_patches=(
                        ModulePatch(
                            module_id=modules[0]["id"],
                            base_snapshot_hash=frame_snapshot["snapshot_hash"],
                            replacement=ModuleSelection(
                                module_id=modules[0]["id"],
                                candidate_id=alternate_candidate_id,
                                candidate_name="Reinforced frame",
                                rationale="Use the documented load-bearing alternate.",
                                evidence_binding_ids=(evidence_id,),
                                risks=("Bench load verification required",),
                            ),
                        ),
                    ),
                    stale_evidence_binding_ids=(),
                    replacement_bom_items=(
                        BomItem(
                            line_id="reinforced-frame",
                            module_id=modules[0]["id"],
                            candidate_id=alternate_candidate_id,
                            name="Reinforced frame",
                            quantity=1,
                            unit="piece",
                            evidence_binding_ids=(evidence_id,),
                        ),
                    ),
                    replacement_implementation_steps=(
                        SolutionPlanStep(
                            step_id="replace-frame",
                            title="Replace frame",
                            instruction="Install the reinforced frame.",
                            module_ids=(modules[0]["id"],),
                        ),
                    ),
                    replacement_verification_steps=(
                        SolutionPlanStep(
                            step_id="load-test",
                            title="Bench load test",
                            instruction="Repeat the measured load test.",
                            module_ids=(modules[0]["id"],),
                            acceptance=("No visible frame flex",),
                        ),
                    ),
                    summary="Replace the directly affected frame and recheck the load.",
                    risks=("Propulsion alignment must be rechecked.",),
                    artifact_ref="artifact://api-impact-proposal",
                    profile_id="impact-proposer-ro",
                    profile_revision=1,
                    idempotency_key=f"{key_prefix}:impact-proposal",
                )
            assert {str(item) for item in impact.affected_module_ids} == {
                item["id"] for item in modules
            }
            assert impact.unaffected_module_ids == ()

            browser_authored_patch = await client.post(
                f"/api/impacts/{impact.id}/approve",
                json={
                    "module_patches": [{"module_id": modules[0]["id"], "selection": "Browser fake"}]
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:browser-authored-patch",
                    "If-Match": '"8"',
                },
            )
            assert browser_authored_patch.status_code == 422

            stale_impact_basis = await client.post(
                f"/api/impacts/{impact.id}/approve",
                json={"basis_hash": sha256(b"stale-impact-basis").hexdigest()},
                headers={
                    "Idempotency-Key": f"{key_prefix}:stale-impact-basis",
                    "If-Match": '"8"',
                },
            )
            assert stale_impact_basis.status_code == 412

            patched = await client.post(
                f"/api/impacts/{impact.id}/approve",
                json={"basis_hash": impact.basis_hash},
                headers={
                    "Idempotency-Key": f"{key_prefix}:patch",
                    "If-Match": '"8"',
                },
            )
            assert patched.status_code == 200
            assert patched.headers["etag"] == '"9"'
            assert patched.json()["solution"]["version"] == 2

            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200
            assert snapshot.json()["project"]["revision"] == 9
            assert len(snapshot.json()["solution_proposals"]) == 1
            assert snapshot.json()["solution_proposals"][0]["status"] == "approved"
            assert len(snapshot.json()["solutions"]) == 2
            assert len(snapshot.json()["patch_sets"]) == 1
            assert len(snapshot.json()["runtime"]["jobs"]) == 3
            assert {item["kind"] for item in snapshot.json()["runtime"]["jobs"]} == {
                "research_wave",
                "solution_wave",
                "impact_wave",
            }
            assert all(item["status"] == "queued" for item in snapshot.json()["runtime"]["jobs"])
            assert len(snapshot.json()["runtime"]["budget_accounts"]) == 3
            job_kind_by_id = {
                item["id"]: item["kind"] for item in snapshot.json()["runtime"]["jobs"]
            }
            assert {
                job_kind_by_id[item["root_job_id"]]: item["token_cap"]
                for item in snapshot.json()["runtime"]["budget_accounts"]
            } == {
                "research_wave": 8_000,
                "solution_wave": 16_000,
                "impact_wave": 16_000,
            }

            # ── V1 shopping closed loop ────────────────────────────────

            active_solution = patched.json()["solution"]
            active_bom_line_id = active_solution["bom"][0]["line_id"]

            # Step 1: Search offers
            search_payload = {
                "query": SCENARIO_SEARCH_QUERY,
                "bom_line_id": active_bom_line_id,
                "region": "CN",
            }
            search = await client.post(
                f"/api/projects/{project_id}/shopping/offers/search",
                json=search_payload,
                headers={"If-Match": '"9"'},
            )
            assert search.status_code == 200, f"search failed: {search.text}"
            snapshots = search.json()
            assert isinstance(snapshots, list)
            assert len(snapshots) == 1
            snap = snapshots[0]
            assert snap["provider"] == "fake-desktop"
            assert snap["title"] == SCENARIO_OFFER.title
            assert snap["unit_price"] == SCENARIO_OFFER.unit_price
            assert snap["bom_line_id"] == active_bom_line_id
            offer_snapshot_id = snap["id"]

            # Step 2: Replay search — same snapshots returned
            replay_search = await client.post(
                f"/api/projects/{project_id}/shopping/offers/search",
                json=search_payload,
                headers={"If-Match": '"9"'},
            )
            assert replay_search.status_code == 200
            assert len(replay_search.json()) >= 1

            # Step 3: Create purchase proposal with max_total enforcement
            proposal_payload = {
                "solution_version_id": active_solution["id"],
                "offer_snapshot_id": offer_snapshot_id,
                "quantity": 1,
                "region": "CN",
                "currency": "CNY",
                "shipping_estimate": SCENARIO_OFFER.shipping_estimate,
                "tax_estimate": SCENARIO_OFFER.tax_estimate,
                "max_total": SCENARIO_MAX_TOTAL,
            }
            mismatched_binding = await client.post(
                f"/api/projects/{project_id}/purchase-proposals",
                json={**proposal_payload, "region": "US"},
                headers={
                    "Idempotency-Key": f"{key_prefix}:binding-mismatch",
                    "If-Match": '"9"',
                },
            )
            assert mismatched_binding.status_code == 409

            proposal_response = await client.post(
                f"/api/projects/{project_id}/purchase-proposals",
                json=proposal_payload,
                headers={
                    "Idempotency-Key": f"{key_prefix}:proposal",
                    "If-Match": '"9"',
                },
            )
            assert proposal_response.status_code == 201, (
                f"proposal failed: {proposal_response.text}"
            )
            prop = proposal_response.json()
            assert prop["status"] == "draft"
            assert prop["unit_price"] == SCENARIO_OFFER.unit_price
            assert prop["max_total"] == SCENARIO_MAX_TOTAL
            assert proposal_response.headers["etag"] == '"10"'

            # max_total violation
            over_total = await client.post(
                f"/api/projects/{project_id}/purchase-proposals",
                json={**proposal_payload, "max_total": "100.00"},
                headers={
                    "Idempotency-Key": f"{key_prefix}:over-total",
                    "If-Match": '"9"',
                },
            )
            assert over_total.status_code == 409
            assert "exceeds max_total" in over_total.json()["error"]["message"]

            # Step 4: Confirm BOM line
            confirm_payload = {"confirmed_line_ids": [active_bom_line_id]}
            confirm = await client.post(
                f"/api/purchase-proposals/{prop['id']}/confirm-lines",
                json=confirm_payload,
                headers={
                    "Idempotency-Key": f"{key_prefix}:confirm",
                    "If-Match": '"10"',
                },
            )
            assert confirm.status_code == 200, f"confirm failed: {confirm.text}"
            confirmed = confirm.json()
            assert confirmed["status"] == "ready"
            assert active_bom_line_id in confirmed["confirmed_line_ids"]

            # Confirm with invalid line_id
            bad_confirm = await client.post(
                f"/api/purchase-proposals/{prop['id']}/confirm-lines",
                json={"confirmed_line_ids": ["wrong-line"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:bad-confirm",
                    "If-Match": '"10"',
                },
            )
            assert bad_confirm.status_code == 409

            # Step 5: Explicit scoped effect approval
            approval_request = await client.post(
                f"/api/purchase-proposals/{prop['id']}/effect-approvals",
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval-request",
                    "If-Match": '"11"',
                },
            )
            assert approval_request.status_code == 201, approval_request.text
            approval = approval_request.json()
            assert approval["status"] == "requested"
            assert approval["effect_kind"] == "shopping.create_cart"
            assert approval["target_ref"] == prop["id"]
            assert approval_request.headers["etag"] == '"12"'

            # Database constraints remain authoritative even when a writer
            # bypasses the application store/CAS layer.
            async with factory() as session:
                with pytest.raises(DBAPIError, match="scope is immutable"):
                    await session.execute(
                        text(
                            "UPDATE effect_approvals SET scope_hash = :scope_hash "
                            "WHERE id = CAST(:approval_id AS uuid)"
                        ),
                        {"scope_hash": "e" * 64, "approval_id": approval["id"]},
                    )
                    await session.commit()
                await session.rollback()

            async with factory() as session:
                duplicate_id = str(uuid4())
                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            """
                            INSERT INTO effect_approvals (
                                id, project_id, effect_kind, target_ref, basis_hash,
                                scope_hash, constraints, status, payload, requested_at,
                                expires_at, resolved_at, consumed_at
                            )
                            SELECT
                                CAST(:duplicate_id AS uuid), project_id, effect_kind,
                                target_ref, basis_hash, scope_hash, constraints, status,
                                payload || jsonb_build_object('id', :duplicate_id),
                                requested_at, expires_at, resolved_at, consumed_at
                            FROM effect_approvals
                            WHERE id = CAST(:approval_id AS uuid)
                            """
                        ),
                        {"duplicate_id": duplicate_id, "approval_id": approval["id"]},
                    )
                    await session.commit()
                await session.rollback()

            duplicate_live_approval = await client.post(
                f"/api/purchase-proposals/{prop['id']}/effect-approvals",
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval-request-again",
                    "If-Match": '"12"',
                },
            )
            assert duplicate_live_approval.status_code == 409

            pending_checkout = await client.post(
                f"/api/purchase-proposals/{prop['id']}/checkout-handoffs",
                json={"effect_approval_id": approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:pending-checkout",
                    "If-Match": '"12"',
                },
            )
            assert pending_checkout.status_code == 409
            assert fake_provider.handoff_create_called == 0

            wrong_scope = await client.post(
                f"/api/effect-approvals/{approval['id']}/resolve",
                json={"decision": "approved", "scope_hash": "f" * 64},
                headers={
                    "Idempotency-Key": f"{key_prefix}:wrong-scope",
                    "If-Match": '"12"',
                },
            )
            assert wrong_scope.status_code == 412

            approval_resolve = await client.post(
                f"/api/effect-approvals/{approval['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": approval["scope_hash"],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval-resolve",
                    "If-Match": '"12"',
                },
            )
            assert approval_resolve.status_code == 200, approval_resolve.text
            assert approval_resolve.json()["status"] == "approved"
            assert approval_resolve.headers["etag"] == '"13"'
            replayed_approval_request = await client.post(
                f"/api/purchase-proposals/{prop['id']}/effect-approvals",
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval-request",
                    "If-Match": '"11"',
                },
            )
            assert replayed_approval_request.status_code == 201
            assert replayed_approval_request.json()["status"] == "approved"
            assert replayed_approval_request.headers["etag"] == '"13"'

            # Step 6: Checkout handoff — successful dispatch and approval consumption
            checkout = await client.post(
                f"/api/purchase-proposals/{prop['id']}/checkout-handoffs",
                json={"effect_approval_id": approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:checkout",
                    "If-Match": '"13"',
                },
            )
            assert checkout.status_code == 201, f"checkout failed: {checkout.text}"
            assert checkout.headers["etag"] == '"14"'
            handoff = checkout.json()
            assert handoff["status"] == "dispatched"
            assert handoff["checkout_url"].startswith("https://")
            assert "fake-cart" in handoff["provider_cart_id"]
            replay_checkout = await client.post(
                f"/api/purchase-proposals/{prop['id']}/checkout-handoffs",
                json={"effect_approval_id": approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:checkout",
                    "If-Match": '"13"',
                },
            )
            assert replay_checkout.status_code == 201
            assert replay_checkout.json()["id"] == handoff["id"]
            assert replay_checkout.headers["etag"] == '"14"'
            assert fake_provider.handoff_create_called == 1
            replayed_approval_resolve = await client.post(
                f"/api/effect-approvals/{approval['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": approval["scope_hash"],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval-resolve",
                    "If-Match": '"12"',
                },
            )
            assert replayed_approval_resolve.status_code == 200
            assert replayed_approval_resolve.json()["status"] == "consumed"
            assert replayed_approval_resolve.headers["etag"] == '"14"'
            consumed_with_new_key = await client.post(
                f"/api/purchase-proposals/{prop['id']}/checkout-handoffs",
                json={"effect_approval_id": approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:checkout-new-key",
                    "If-Match": '"14"',
                },
            )
            assert consumed_with_new_key.status_code == 409
            assert fake_provider.handoff_create_called == 1

            # Proposal should now be handed_off
            final_snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert final_snapshot.status_code == 200
            proposals = final_snapshot.json()["purchase_proposals"]
            assert len(proposals) == 1
            assert proposals[0]["status"] == "handed_off"
            stored_approval = final_snapshot.json()["effect_approvals"][0]
            assert stored_approval["status"] == "consumed"
            assert stored_approval["consumed_at"] is not None

            # Step 7: Ambiguous handoff (provider failure keeps proposal READY)
            # Create a second proposal via the fake provider with cart failure
            search2 = await client.post(
                f"/api/projects/{project_id}/shopping/offers/search",
                json=search_payload,
                headers={"If-Match": '"14"'},
            )
            assert search2.status_code == 200
            snap2_id = search2.json()[0]["id"]

            prop2 = await client.post(
                f"/api/projects/{project_id}/purchase-proposals",
                json={
                    "solution_version_id": active_solution["id"],
                    "offer_snapshot_id": snap2_id,
                    "quantity": 1,
                    "region": "CN",
                    "currency": "CNY",
                    "shipping_estimate": SCENARIO_OFFER.shipping_estimate,
                    "tax_estimate": SCENARIO_OFFER.tax_estimate,
                    "max_total": SCENARIO_MAX_TOTAL,
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:prop2",
                    "If-Match": '"14"',
                },
            )
            assert prop2.status_code == 201
            prop2_id = prop2.json()["id"]
            checkout_after_project_advanced = await client.post(
                f"/api/purchase-proposals/{prop['id']}/checkout-handoffs",
                json={"effect_approval_id": approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:checkout",
                    "If-Match": '"13"',
                },
            )
            assert checkout_after_project_advanced.status_code == 201
            assert checkout_after_project_advanced.headers["etag"] == '"15"'

            confirm2 = await client.post(
                f"/api/purchase-proposals/{prop2_id}/confirm-lines",
                json={"confirmed_line_ids": [active_bom_line_id]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:confirm2",
                    "If-Match": '"15"',
                },
            )
            assert confirm2.status_code == 200

            api.state.effect_approval_ttl_seconds = 1
            expiring_approval_request = await client.post(
                f"/api/purchase-proposals/{prop2_id}/effect-approvals",
                headers={
                    "Idempotency-Key": f"{key_prefix}:expiring-approval-request",
                    "If-Match": '"16"',
                },
            )
            assert expiring_approval_request.status_code == 201
            expiring_approval = expiring_approval_request.json()
            await asyncio.sleep(1.05)
            expired_checkout = await client.post(
                f"/api/purchase-proposals/{prop2_id}/checkout-handoffs",
                json={"effect_approval_id": expiring_approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:expired-checkout",
                    "If-Match": '"17"',
                },
            )
            assert expired_checkout.status_code == 409

            api.state.effect_approval_ttl_seconds = 900
            denied_approval_request = await client.post(
                f"/api/purchase-proposals/{prop2_id}/effect-approvals",
                headers={
                    "Idempotency-Key": f"{key_prefix}:denied-approval-request",
                    "If-Match": '"17"',
                },
            )
            assert denied_approval_request.status_code == 201
            denied_approval = denied_approval_request.json()
            denial_without_reason = await client.post(
                f"/api/effect-approvals/{denied_approval['id']}/resolve",
                json={
                    "decision": "denied",
                    "scope_hash": denied_approval["scope_hash"],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:denial-without-reason",
                    "If-Match": '"18"',
                },
            )
            assert denial_without_reason.status_code == 409
            denied_approval_resolve = await client.post(
                f"/api/effect-approvals/{denied_approval['id']}/resolve",
                json={
                    "decision": "denied",
                    "scope_hash": denied_approval["scope_hash"],
                    "reason": "operator rejected this cart",
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:denied-approval-resolve",
                    "If-Match": '"18"',
                },
            )
            assert denied_approval_resolve.status_code == 200
            assert denied_approval_resolve.json()["status"] == "denied"
            denied_checkout = await client.post(
                f"/api/purchase-proposals/{prop2_id}/checkout-handoffs",
                json={"effect_approval_id": denied_approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:denied-checkout",
                    "If-Match": '"19"',
                },
            )
            assert denied_checkout.status_code == 409

            approval2_request = await client.post(
                f"/api/purchase-proposals/{prop2_id}/effect-approvals",
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval2-request",
                    "If-Match": '"19"',
                },
            )
            assert approval2_request.status_code == 201
            approval2 = approval2_request.json()
            approval2_resolve = await client.post(
                f"/api/effect-approvals/{approval2['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": approval2["scope_hash"],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval2-resolve",
                    "If-Match": '"20"',
                },
            )
            assert approval2_resolve.status_code == 200

            cross_scope = await client.post(
                f"/api/purchase-proposals/{prop2_id}/checkout-handoffs",
                json={"effect_approval_id": approval["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:cross-scope-checkout",
                    "If-Match": '"21"',
                },
            )
            assert cross_scope.status_code == 412

            # Make the FakeProvider fail handoff creation
            fake_provider.set_handoff_failure(ShoppingProviderError("cart service down"))

            ambiguous_checkout = await client.post(
                f"/api/purchase-proposals/{prop2_id}/checkout-handoffs",
                json={"effect_approval_id": approval2["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:amb-checkout",
                    "If-Match": '"21"',
                },
            )
            assert ambiguous_checkout.status_code == 201
            amb_handoff = ambiguous_checkout.json()
            assert amb_handoff["status"] == "ambiguous"
            assert amb_handoff["provider_cart_id"] is None
            assert amb_handoff["checkout_url"] is None

            # Proposal must still be READY (NOT handed_off) on ambiguous
            final_snap2 = await client.get(f"/api/projects/{project_id}/snapshot")
            proposals2 = final_snap2.json()["purchase_proposals"]
            prop2_status = next(p["status"] for p in proposals2 if p["id"] == prop2_id)
            assert prop2_status == "ready", f"expected ready, got {prop2_status}"

            # Step 8: unexpected provider crash preserves consumed + PREPARED boundary
            search3 = await client.post(
                f"/api/projects/{project_id}/shopping/offers/search",
                json=search_payload,
                headers={"If-Match": '"22"'},
            )
            assert search3.status_code == 200
            prop3 = await client.post(
                f"/api/projects/{project_id}/purchase-proposals",
                json={**proposal_payload, "offer_snapshot_id": search3.json()[0]["id"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:prop3",
                    "If-Match": '"22"',
                },
            )
            assert prop3.status_code == 201
            prop3_id = prop3.json()["id"]
            confirm3 = await client.post(
                f"/api/purchase-proposals/{prop3_id}/confirm-lines",
                json={"confirmed_line_ids": [active_bom_line_id]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:confirm3",
                    "If-Match": '"23"',
                },
            )
            assert confirm3.status_code == 200
            approval3_request = await client.post(
                f"/api/purchase-proposals/{prop3_id}/effect-approvals",
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval3-request",
                    "If-Match": '"24"',
                },
            )
            assert approval3_request.status_code == 201
            approval3 = approval3_request.json()
            approval3_resolve = await client.post(
                f"/api/effect-approvals/{approval3['id']}/resolve",
                json={
                    "decision": "approved",
                    "scope_hash": approval3["scope_hash"],
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:approval3-resolve",
                    "If-Match": '"25"',
                },
            )
            assert approval3_resolve.status_code == 200

            fake_provider.set_handoff_failure(RuntimeError("simulated provider process crash"))
            crash_headers = {
                "Idempotency-Key": f"{key_prefix}:crash-checkout",
                "If-Match": '"26"',
            }
            with pytest.raises(RuntimeError, match="simulated provider process crash"):
                await client.post(
                    f"/api/purchase-proposals/{prop3_id}/checkout-handoffs",
                    json={"effect_approval_id": approval3["id"]},
                    headers=crash_headers,
                )
            calls_after_crash = fake_provider.handoff_create_called
            crash_replay = await client.post(
                f"/api/purchase-proposals/{prop3_id}/checkout-handoffs",
                json={"effect_approval_id": approval3["id"]},
                headers=crash_headers,
            )
            assert crash_replay.status_code == 201
            assert crash_replay.json()["status"] == "prepared"
            assert crash_replay.headers["etag"] == '"27"'
            assert fake_provider.handoff_create_called == calls_after_crash
            crash_snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert crash_snapshot.json()["project"]["revision"] == 27
            approval3_state = next(
                item
                for item in crash_snapshot.json()["effect_approvals"]
                if item["id"] == approval3["id"]
            )
            assert approval3_state["status"] == "consumed"
            assert any(
                item["proposal_id"] == prop3_id and item["status"] == "prepared"
                for item in crash_snapshot.json()["checkout_handoffs"]
            )

            events = await client.get(f"/api/projects/{project_id}/events")
            assert [item["sequence"] for item in events.json()] == list(range(1, 37))
            replayed_events = await client.get(
                f"/api/projects/{project_id}/events",
                params={"after": 5},
            )
            assert [item["sequence"] for item in replayed_events.json()] == list(range(6, 37))
            assert replayed_events.json()[0]["id"] == f"{project_id}:6"
    finally:
        await engine.dispose()
