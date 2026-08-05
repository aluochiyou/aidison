from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest

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
from aidison.operations.fixture import FakeShoppingProvider
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
            assert snapshot.json()["runtime"]["budget_accounts"][0]["token_cap"] == 64_000

            # ── V1 shopping closed loop ────────────────────────────────

            fake_provider = FakeShoppingProvider()
            rev9_solution = solution.json()["solution"]

            # Step 1: Search offers
            search_payload = {
                "query": "Raspberry Pi 5 8GB",
                "bom_line_id": "open-frame",
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
            assert snap["title"] == "Raspberry Pi 5 8GB"
            assert snap["unit_price"] == "499.00"
            assert snap["bom_line_id"] == "open-frame"
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
                "solution_version_id": rev9_solution["id"],
                "offer_snapshot_id": offer_snapshot_id,
                "quantity": 1,
                "region": "CN",
                "currency": "CNY",
                "max_total": "600.00",
            }
            proposal = await client.post(
                f"/api/projects/{project_id}/purchase-proposals",
                json=proposal_payload,
                headers={
                    "Idempotency-Key": f"{key_prefix}:proposal",
                    "If-Match": '"9"',
                },
            )
            assert proposal.status_code == 201, f"proposal failed: {proposal.text}"
            prop = proposal.json()
            assert prop["status"] == "draft"
            assert prop["unit_price"] == "499.00"
            assert prop["max_total"] == "600.00"
            assert proposal.headers["etag"] == '"10"'

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
            confirm_payload = {"confirmed_line_ids": ["open-frame"]}
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
            assert "open-frame" in confirmed["confirmed_line_ids"]

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

            # Step 5: Checkout handoff — successful dispatch
            checkout = await client.post(
                f"/api/purchase-proposals/{prop['id']}/checkout-handoffs",
                headers={
                    "Idempotency-Key": f"{key_prefix}:checkout",
                    "If-Match": '"11"',
                },
            )
            assert checkout.status_code == 201, f"checkout failed: {checkout.text}"
            handoff = checkout.json()
            assert handoff["status"] == "dispatched"
            assert handoff["checkout_url"].startswith("https://")
            assert "fake-cart" in handoff["provider_cart_id"]

            # Proposal should now be handed_off
            final_snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert final_snapshot.status_code == 200
            proposals = final_snapshot.json()["purchase_proposals"]
            assert len(proposals) == 1
            assert proposals[0]["status"] == "handed_off"

            # Step 6: Ambiguous handoff (provider failure keeps proposal READY)
            # Create a second proposal via the fake provider with cart failure
            search2 = await client.post(
                f"/api/projects/{project_id}/shopping/offers/search",
                json=search_payload,
                headers={"If-Match": '"12"'},
            )
            assert search2.status_code == 200
            snap2_id = search2.json()[0]["id"]

            prop2 = await client.post(
                f"/api/projects/{project_id}/purchase-proposals",
                json={
                    "solution_version_id": rev9_solution["id"],
                    "offer_snapshot_id": snap2_id,
                    "quantity": 1,
                    "region": "CN",
                    "currency": "CNY",
                    "max_total": "600.00",
                },
                headers={
                    "Idempotency-Key": f"{key_prefix}:prop2",
                    "If-Match": '"12"',
                },
            )
            assert prop2.status_code == 201
            prop2_id = prop2.json()["id"]

            confirm2 = await client.post(
                f"/api/purchase-proposals/{prop2_id}/confirm-lines",
                json={"confirmed_line_ids": ["open-frame"]},
                headers={
                    "Idempotency-Key": f"{key_prefix}:confirm2",
                    "If-Match": '"13"',
                },
            )
            assert confirm2.status_code == 200

            # Make the FakeProvider fail cart creation
            fake_provider.set_cart_failure(ShoppingProviderError("cart service down"))

            ambiguous_checkout = await client.post(
                f"/api/purchase-proposals/{prop2_id}/checkout-handoffs",
                headers={
                    "Idempotency-Key": f"{key_prefix}:amb-checkout",
                    "If-Match": '"14"',
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
            prop2_status = next(
                p["status"] for p in proposals2 if p["id"] == prop2_id
            )
            assert prop2_status == "ready", (
                f"expected ready, got {prop2_status}"
            )

            events = await client.get(f"/api/projects/{project_id}/events")
            assert [item["sequence"] for item in events.json()] == list(range(1, 13))
            replayed_events = await client.get(
                f"/api/projects/{project_id}/events",
                params={"after": 5},
            )
            assert [item["sequence"] for item in replayed_events.json()] == list(range(6, 13))
            assert replayed_events.json()[0]["id"] == f"{project_id}:6"
    finally:
        await engine.dispose()
