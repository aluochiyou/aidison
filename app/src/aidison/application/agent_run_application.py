"""Application command that turns an approved execution plan into one AgentRun."""

from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from aidison.application.service import (
    DomainConflictError,
    DomainNotFoundError,
    PreconditionFailedError,
    ProjectApplication,
    canonical_hash,
)
from aidison.domain.models import DecisionStatus, EvidenceStatus, ExecutionPlanStatus
from aidison.impact.contracts import ImpactContract
from aidison.infrastructure.agent_run_budget import AgentRunBudgetLedger
from aidison.infrastructure.agent_runs import AgentRunConflictError, AgentRunControl
from aidison.infrastructure.artifacts import ContentAddressedArtifactStore
from aidison.infrastructure.store import PostgresDomainStore
from aidison.research.coverage import CoverageCompileInput, compile_coverage_contract
from aidison.research.strategy import ResearchRunContract, compile_research_execution_policy
from aidison.runtime.agent_run_events import AgentRunEventType
from aidison.runtime.agent_runs import AgentRun, AgentRunKind, AgentRunStatus
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.solution.contracts import (
    SolutionChangeKind,
    SolutionChangeSet,
    SolutionContract,
    SolutionRiskClass,
    compile_solution_coverage_contract,
)

DEFAULT_RESEARCH_RUNTIME_BINDING = RuntimeBinding(
    runtime_family=RuntimeFamily.LANGGRAPH_V1,
    runtime_revision="runtime-v1",
    graph_key="research",
    graph_revision="research-v1",
    state_schema_version="research-state-v1",
    profile_binding_ref="profile://research/1",
    policy_binding_ref="policy://research/1",
)

DEFAULT_SOLUTION_RUNTIME_BINDING = RuntimeBinding(
    runtime_family=RuntimeFamily.LANGGRAPH_V1,
    runtime_revision="runtime-v1",
    graph_key="solution",
    graph_revision="solution-v1",
    state_schema_version="solution-state-v1",
    profile_binding_ref="profile://solution/1",
    policy_binding_ref="policy://solution/1",
)

DEFAULT_IMPACT_PROPOSAL_RUNTIME_BINDING = RuntimeBinding(
    runtime_family=RuntimeFamily.LANGGRAPH_V1,
    runtime_revision="runtime-v1",
    graph_key="impact",
    graph_revision="impact-v2",
    state_schema_version="impact-proposal-state-v1",
    profile_binding_ref="profile://impact-analyst/1",
    policy_binding_ref="policy://impact-proposal/1",
)


class AgentRunApplication:
    """Create the one new-runtime Run authorized by one approved plan."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        artifact_root: Path = Path("artifacts/data"),
    ) -> None:
        self._session = session
        self._store = PostgresDomainStore(session)
        self._artifact_root = artifact_root

    async def enqueue_research_run(
        self,
        *,
        project_id: UUID,
        execution_plan_id: UUID,
        expected_project_revision: int,
        runtime_binding: RuntimeBinding,
        retry_failed: bool = False,
        commit: bool = True,
    ) -> AgentRun:
        project = await self._store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before research")
        requirement = await self._store.get_requirement_revision(
            project.active_requirement_revision_id
        )
        if requirement is None:
            raise DomainConflictError("active requirement revision is unavailable")
        plan = await self._store.get_execution_plan_proposal(execution_plan_id)
        if plan is None or plan.project_id != project_id:
            raise DomainNotFoundError("execution plan not found")
        if plan.status is not ExecutionPlanStatus.APPROVED:
            raise PreconditionFailedError("execution plan is not approved")
        strategy = plan.research_strategy
        if strategy is None:
            raise DomainConflictError(
                "Research Run requires an AI-generated reviewable research strategy"
            )

        modules = await ProjectApplication(self._store)._active_modules(project)
        if not modules:
            raise DomainConflictError("research requires at least one active module")
        basis_hash = canonical_hash(project.active_requirement_revision_id, modules)
        if plan.basis_hash != basis_hash:
            raise PreconditionFailedError("execution plan basis is stale")
        if plan.scope_hash is None:  # defensive for legacy rows predating scope hashing
            raise DomainConflictError("execution plan scope hash is unavailable")
        modules_by_id = {item.id: item for item in modules}
        scope_ids = strategy.scope_module_ids
        if any(item not in modules_by_id for item in scope_ids):
            raise PreconditionFailedError("research strategy scope is no longer active")
        if any(len(item.module_ids) != 1 for item in strategy.tasks):
            raise DomainConflictError("research strategy tasks must target exactly one module")
        scope_modules = tuple(
            modules_by_id[item]
            for item in sorted(scope_ids, key=lambda value: str(value))
        )

        key = f"research-plan:{plan.id}"
        run_id = uuid5(NAMESPACE_URL, f"aidison://agent-run/{key}")
        existing = await AgentRunControl(self._session).get(run_id)
        if retry_failed:
            if existing is None or existing.status is not AgentRunStatus.FAILED:
                raise AgentRunConflictError("only a failed research Run can be retried")
            retry_suffix = uuid4()
            key = f"{key}:retry:{retry_suffix}"
            run_id = retry_suffix
        run = AgentRun(
            id=run_id,
            project_id=project_id,
            kind=AgentRunKind.RESEARCH,
            idempotency_key=key,
            basis_hash=basis_hash,
            basis_project_revision=project.revision,
            runtime_binding=runtime_binding,
            thread_id=f"agent-run:{run_id}",
        )
        control = AgentRunControl(self._session)
        created = await control.create(run)
        # The account is created with the Run in the same command transaction.
        # Physical model calls may only reserve against this durable, user-
        # approved ceiling; a retry of the idempotent enqueue reuses the same
        # account and cannot silently change its cap.
        await AgentRunBudgetLedger(self._session).create_account(
            agent_run_id=created.id,
            token_cap=plan.max_token_budget,
            tool_call_cap=0,
        )
        run_contract = ResearchRunContract(
            agent_run_id=created.id,
            execution_plan_id=plan.id,
            execution_plan_scope_hash=plan.scope_hash,
            basis_hash=basis_hash,
            scope_module_ids=tuple(item.id for item in scope_modules),
            max_concurrency=plan.max_concurrency,
            max_token_budget=plan.max_token_budget,
            max_duration_seconds=plan.max_duration_seconds,
            requires_independent_verification=plan.requires_independent_verification,
            execution_policy=compile_research_execution_policy(
                research_depth=plan.research_depth
            ),
            research_strategy=strategy,
        )
        artifacts = ContentAddressedArtifactStore(self._session, self._artifact_root)
        run_contract_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="research_run_contract",
            value=run_contract.model_dump(mode="json"),
            commit=commit,
        )
        created = await control.bind_run_contract(
            run_id=created.id,
            run_contract_ref=run_contract_artifact.ref,
        )
        contract = compile_coverage_contract(
            CoverageCompileInput(
                basis_hash=basis_hash,
                objective=plan.objective,
                modules=scope_modules,
                strategy_tasks=strategy.tasks,
                context_lines=_research_requirement_context(requirement),
                requires_independent_verification=plan.requires_independent_verification,
                minimum_evidence_sources_for_must=(
                    run_contract.execution_policy.minimum_evidence_sources_for_must
                ),
                minimum_evidence_origins_for_must=(
                    run_contract.execution_policy.minimum_evidence_origins_for_must
                ),
            )
        )
        artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="research_coverage_contract",
            value=contract.model_dump(mode="json"),
            commit=commit,
        )
        created = await control.bind_coverage_contract(
            run_id=created.id,
            coverage_contract_ref=artifact.ref,
        )
        if existing is None or retry_failed:
            event_payload: dict[str, object] = {
                "execution_plan_id": str(plan.id),
                "basis_hash": basis_hash,
                "run_contract_ref": run_contract_artifact.ref,
                "coverage_contract_ref": artifact.ref,
                "research_strategy_task_count": len(strategy.tasks),
                "supersedes_agent_run_id": (
                    str(existing.id) if retry_failed and existing is not None else None
                ),
            }
            created = await control.record_queued_event(
                run_id=created.id,
                event_type=AgentRunEventType.RESEARCH_QUEUED,
                context=event_payload,
                artifact_refs=(run_contract_artifact.ref, artifact.ref),
            )
        if commit:
            await self._session.commit()
        return created
    async def enqueue_solution_run(
        self,
        *,
        project_id: UUID,
        decision_id: UUID,
        expected_project_revision: int,
        runtime_binding: RuntimeBinding,
        risk_class: SolutionRiskClass,
    ) -> AgentRun:
        """Create one Solution AgentRun from a current, evidence-backed decision.

        The user decision is the authorization boundary for this first solution
        vertical.  The graph receives immutable contract references only; it
        cannot reinterpret a different decision or write ``SolutionVersion``.
        """

        project = await self._store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_requirement_revision_id is None:
            raise DomainConflictError("requirements must be approved before solution")

        requirement = await self._store.get_requirement_revision(
            project.active_requirement_revision_id
        )
        if requirement is None:
            raise DomainConflictError("active requirement revision is unavailable")
        modules = await ProjectApplication(self._store)._active_modules(project)
        if not modules:
            raise DomainConflictError("solution requires at least one active module")
        decision = await self._store.get_decision_request(decision_id)
        if decision is None or decision.project_id != project_id:
            raise DomainNotFoundError("decision not found")
        if decision.status is not DecisionStatus.APPROVED or decision.selected_option_id is None:
            raise DomainConflictError("solution requires an approved decision")
        option = next(
            (item for item in decision.options if item.option_id == decision.selected_option_id),
            None,
        )
        if option is None:  # pragma: no cover - DecisionRequest validates this invariant
            raise DomainConflictError("approved decision has no selected option")
        if option.legacy_unbound or not option.candidate_ids or not option.evidence_binding_ids:
            raise DomainConflictError(
                "solution requires a decision bound to candidates and evidence"
            )

        candidates_by_id = {item.id: item for item in await self._store.list_candidates(project_id)}
        selected_candidates = tuple(
            candidates_by_id.get(item_id) for item_id in option.candidate_ids
        )
        if any(item is None for item in selected_candidates):
            raise DomainConflictError("solution decision references unavailable candidates")
        approved_candidates = tuple(item for item in selected_candidates if item is not None)
        active_module_ids = {item.id for item in modules}
        if {item.module_id for item in approved_candidates} != active_module_ids or len(
            approved_candidates
        ) != len(active_module_ids):
            raise DomainConflictError(
                "solution decision must select exactly one candidate for every active module"
            )

        evidence_by_id = {
            item.id: item for item in await self._store.list_evidence_bindings(project_id)
        }
        selected_evidence = tuple(
            evidence_by_id.get(item_id) for item_id in option.evidence_binding_ids
        )
        if any(item is None for item in selected_evidence):
            raise DomainConflictError("solution decision references unavailable supported evidence")
        supported_evidence = tuple(item for item in selected_evidence if item is not None)
        if any(item.status is not EvidenceStatus.SUPPORTED for item in supported_evidence):
            raise DomainConflictError("solution decision references unavailable supported evidence")

        basis_hash = canonical_hash(project.active_requirement_revision_id, modules)
        key = f"solution-decision:{decision.id}"
        run_id = uuid5(NAMESPACE_URL, f"aidison://agent-run/{key}")
        run = AgentRun(
            id=run_id,
            project_id=project_id,
            kind=AgentRunKind.SOLUTION,
            idempotency_key=key,
            basis_hash=basis_hash,
            basis_project_revision=project.revision,
            runtime_binding=runtime_binding,
            thread_id=f"agent-run:{run_id}",
        )
        control = AgentRunControl(self._session)
        existing = await control.get(run.id)
        created = await control.create(run)

        requirement_ref = f"requirement://{requirement.id}"
        module_refs = tuple(
            f"module://{item.id}/requirement:{item.requirement_revision_id}"
            for item in sorted(modules, key=lambda item: item.key)
        )
        artifacts = ContentAddressedArtifactStore(self._session, self._artifact_root)
        constraint_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="solution_constraint_set",
            value={
                "requirement_ref": requirement_ref,
                "hard_constraints": requirement.hard_constraints,
                "preferences": requirement.preferences,
                "available_resources": requirement.available_resources,
                "accepted_decision_ref": f"decision://{decision.id}/option/{option.option_id}",
            },
        )
        coverage = compile_solution_coverage_contract(
            basis_hash=basis_hash,
            requirement_ref=requirement_ref,
            modules=tuple(
                (
                    item.key,
                    f"module://{item.id}/requirement:{item.requirement_revision_id}",
                    item.responsibility,
                )
                for item in modules
            ),
        )
        coverage_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="solution_coverage_contract",
            value=coverage.model_dump(mode="json"),
        )
        contract = SolutionContract(
            solution_run_id=created.id,
            project_id=project_id,
            basis_hash=basis_hash,
            basis_project_revision=project.revision,
            requirement_refs=(requirement_ref,),
            module_revision_refs=module_refs,
            accepted_decision_refs=(f"decision://{decision.id}/option/{option.option_id}",),
            admitted_evidence_refs=tuple(
                f"evidence-binding://{item.id}" for item in supported_evidence
            ),
            constraint_set_ref=constraint_artifact.ref,
            interface_contract_refs=(),
            coverage_contract_ref=coverage_artifact.ref,
            risk_class=risk_class,
            allowed_tool_ids=(),
            budget_ref=f"budget://agent-run/{created.id}",
            verification_policy_ref=f"policy://solution-verification/{risk_class.value}",
            completion_policy_ref="policy://solution-completion/v1",
        )
        contract_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="solution_contract",
            value=contract.model_dump(mode="json"),
        )
        created = await control.bind_run_contract(
            run_id=created.id,
            run_contract_ref=contract_artifact.ref,
        )
        created = await control.bind_coverage_contract(
            run_id=created.id,
            coverage_contract_ref=coverage_artifact.ref,
        )
        if existing is None:
            event_payload: dict[str, object] = {
                "decision_id": str(decision.id),
                "basis_hash": basis_hash,
                "run_contract_ref": contract_artifact.ref,
                "coverage_contract_ref": coverage_artifact.ref,
            }
            created = await control.record_queued_event(
                run_id=created.id,
                event_type=AgentRunEventType.SOLUTION_QUEUED,
                context=event_payload,
                artifact_refs=(contract_artifact.ref, coverage_artifact.ref),
            )
        await self._session.commit()
        return created
    async def enqueue_impact_run(
        self,
        *,
        project_id: UUID,
        observation_id: UUID,
        expected_project_revision: int,
        runtime_binding: RuntimeBinding,
    ) -> AgentRun:
        """Create one deterministic Impact AgentRun from a current observation.

        The current vertical accepts only complete, frozen dependency
        projections.  Historical solutions remain readable through their
        legacy impact command but cannot be silently analysed by the new graph
        using a mutable module graph as a substitute for missing facts.
        """

        project = await self._store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        observation = await self._store.get_observation(observation_id)
        if observation is None or observation.project_id != project_id:
            raise DomainNotFoundError("observation not found")
        if project.active_solution_version_id != observation.solution_version_id:
            raise PreconditionFailedError("observation base is no longer the active solution")
        solution = await self._store.get_solution_version(observation.solution_version_id)
        if solution is None:
            raise DomainConflictError("observation base solution is unavailable")
        if not solution.dependency_projection_complete:
            raise DomainConflictError(
                "ImpactGraph requires a complete frozen dependency projection"
            )

        basis_hash = canonical_hash("impact", solution.basis_hash, observation.id)
        key = f"impact-observation:{observation.id}"
        run_id = uuid5(NAMESPACE_URL, f"aidison://agent-run/{key}")
        run = AgentRun(
            id=run_id,
            project_id=project_id,
            kind=AgentRunKind.IMPACT,
            idempotency_key=key,
            basis_hash=basis_hash,
            basis_project_revision=project.revision,
            runtime_binding=runtime_binding,
            thread_id=f"agent-run:{run_id}",
        )
        control = AgentRunControl(self._session)
        existing = await control.get(run.id)
        created = await control.create(run)
        artifacts = ContentAddressedArtifactStore(self._session, self._artifact_root)
        change_set = SolutionChangeSet(
            change_set_id=observation.id,
            project_id=project_id,
            base_solution_version_id=solution.id,
            base_solution_basis_hash=solution.basis_hash,
            changed_module_ids=observation.affected_module_hints,
            change_kinds=(SolutionChangeKind.OBSERVATION,),
            before_refs=tuple(f"artifact://{item}" for item in observation.artifact_ids),
            trigger_ref=f"observation://{observation.id}",
        )
        change_set_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="solution_change_set",
            value=change_set.model_dump(mode="json"),
        )
        coverage_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="impact_coverage_contract",
            value={
                "schema_version": "impact-coverage-contract-v1",
                "change_set_ref": change_set_artifact.ref,
                "coverage_keys": tuple(
                    f"impact.module.{module_id}" for module_id in change_set.changed_module_ids
                ),
            },
        )
        contract = ImpactContract(
            impact_run_id=created.id,
            project_id=project_id,
            basis_hash=basis_hash,
            basis_project_revision=project.revision,
            base_solution_version_id=solution.id,
            base_solution_basis_hash=solution.basis_hash,
            change_set_ref=change_set_artifact.ref,
            coverage_contract_ref=coverage_artifact.ref,
            impact_policy_ref="policy://impact/deterministic-v1",
        )
        contract_artifact = await artifacts.put_agent_run_json(
            project_id=project_id,
            agent_run_id=created.id,
            basis_hash=basis_hash,
            kind="impact_contract",
            value=contract.model_dump(mode="json"),
        )
        created = await control.bind_run_contract(
            run_id=created.id,
            run_contract_ref=contract_artifact.ref,
        )
        created = await control.bind_coverage_contract(
            run_id=created.id,
            coverage_contract_ref=coverage_artifact.ref,
        )
        if existing is None:
            event_payload: dict[str, object] = {
                "observation_id": str(observation.id),
                "basis_hash": basis_hash,
                "run_contract_ref": contract_artifact.ref,
                "coverage_contract_ref": coverage_artifact.ref,
            }
            created = await control.record_queued_event(
                run_id=created.id,
                event_type=AgentRunEventType.IMPACT_QUEUED,
                context=event_payload,
                artifact_refs=(contract_artifact.ref, coverage_artifact.ref),
            )
        await self._session.commit()
        return created


def _research_requirement_context(requirement: object) -> tuple[str, ...]:
    """Build bounded frozen context that constrains every research task."""

    values = (
        ("Project goal", getattr(requirement, "goal", "")),
        ("Usage context", getattr(requirement, "usage_context", "")),
        ("Budget context", getattr(requirement, "budget_context", "")),
        ("Skill context", getattr(requirement, "skill_context", "")),
        ("Hard constraints", "; ".join(getattr(requirement, "hard_constraints", ()))),
        ("Preferences", "; ".join(getattr(requirement, "preferences", ()))),
        ("Available resources", "; ".join(getattr(requirement, "available_resources", ()))),
    )
    return tuple(
        f"{label}: {text}"
        for label, text in values
        if isinstance(text, str) and text.strip()
    )
