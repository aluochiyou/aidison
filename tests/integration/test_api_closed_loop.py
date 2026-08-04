from __future__ import annotations

import os
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest

from aidison.api.app import create_app
from aidison.application.service import ProjectApplication
from aidison.domain.models import BomItem, ModulePatch, ModuleSelection, SolutionPlanStep
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.store import PostgresDomainStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_http_closed_loop_etag_idempotency_errors_and_cursor_replay() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    api = create_app(factory)
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
