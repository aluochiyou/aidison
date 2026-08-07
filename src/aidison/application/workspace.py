from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AttentionState(StrEnum):
    UP_TO_DATE = "up_to_date"
    WORKING = "working"
    NEEDS_INPUT = "needs_input"
    READY_TO_REVIEW = "ready_to_review"
    RECOVERABLE_FAILURE = "recoverable_failure"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class WorkspaceSourceRef(WorkspaceContract):
    type: str = Field(min_length=1, max_length=80)
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=300)


class WorkspaceAttention(WorkspaceContract):
    state: AttentionState
    title: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=2_000)
    since: datetime
    affected_module_ids: tuple[str, ...] = ()
    source_refs: tuple[WorkspaceSourceRef, ...] = Field(min_length=1)


class WorkspaceAction(WorkspaceContract):
    id: str = Field(min_length=1, max_length=240)
    kind: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    explanation: str = Field(min_length=1, max_length=2_000)
    safe_action: str = Field(min_length=1, max_length=300)
    destructive: bool = False
    source_refs: tuple[WorkspaceSourceRef, ...] = Field(min_length=1)


class WorkspaceModuleSummary(WorkspaceContract):
    id: str
    name: str
    responsibility: str
    domain_stage: str
    work_state: str | None = None
    open_question_count: int = Field(ge=0)
    dependency_count: int = Field(ge=0)


class WorkspaceWorkSummary(WorkspaceContract):
    run_id: str
    user_label: str
    kind: str
    state: str
    started_at: datetime
    updated_at: datetime
    total_units: int = Field(ge=0)
    completed_units: int = Field(ge=0)
    failed_units: int = Field(ge=0)
    latest_error: str | None = None
    source_refs: tuple[WorkspaceSourceRef, ...] = Field(min_length=1)


class ProjectWorkspaceProjectionV1(WorkspaceContract):
    schema_version: str = Field(default="project-workspace.v1", frozen=True)
    generated_at: datetime
    project_revision: int = Field(ge=1)
    event_cursor: int = Field(ge=0)
    attention: WorkspaceAttention
    next_actions: tuple[WorkspaceAction, ...]
    modules: tuple[WorkspaceModuleSummary, ...]
    work: tuple[WorkspaceWorkSummary, ...]
    warnings: tuple[str, ...] = ()


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _as_datetime(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback


def _source(type_: str, id_: Any, label: str) -> WorkspaceSourceRef:
    return WorkspaceSourceRef(type=type_, id=str(id_), label=label)


def _root_work_summaries(
    runtime: Mapping[str, Any],
    *,
    generated_at: datetime,
) -> tuple[WorkspaceWorkSummary, ...]:
    jobs = list(runtime.get("jobs", ()))
    attempts = list(runtime.get("attempts", ()))
    roots = sorted(
        (item for item in jobs if _value(item, "parent_job_id") is None),
        key=lambda item: (
            _as_datetime(_value(item, "created_at"), fallback=generated_at),
            str(_value(item, "id")),
        ),
    )
    children_by_parent: dict[str, list[Any]] = {}
    for job in jobs:
        parent_id = _value(job, "parent_job_id")
        if parent_id is not None:
            children_by_parent.setdefault(str(parent_id), []).append(job)

    labels = {
        "research_wave": "查找并核对资料",
        "solution_wave": "整理可执行方案",
        "impact_wave": "分析现场变化",
    }
    summaries: list[WorkspaceWorkSummary] = []
    for root in roots:
        root_id = str(_value(root, "id"))
        children = children_by_parent.get(root_id, [])
        terminal = {"succeeded", "failed", "cancelled"}
        failed = [item for item in children if _value(item, "status") == "failed"]
        root_attempts = sorted(
            (item for item in attempts if str(_value(item, "job_id")) == root_id),
            key=lambda item: (
                int(_value(item, "number", 0)),
                str(_value(item, "id", "")),
            ),
        )
        errors = [
            str(_value(item, "normalized_error"))
            for item in root_attempts
            if _value(item, "normalized_error")
        ]
        started_at = _as_datetime(_value(root, "created_at"), fallback=generated_at)
        updated_at = _as_datetime(
            _value(root, "completed_at") or _value(root, "created_at"),
            fallback=started_at,
        )
        kind = str(_value(root, "kind", "work"))
        summaries.append(
            WorkspaceWorkSummary(
                run_id=root_id,
                user_label=labels.get(kind, "处理项目工作"),
                kind=kind,
                state=str(_value(root, "status", "queued")),
                started_at=started_at,
                updated_at=updated_at,
                total_units=len(children),
                completed_units=sum(
                    1 for item in children if str(_value(item, "status")) in terminal
                ),
                failed_units=len(failed),
                latest_error=errors[-1] if errors else None,
                source_refs=(_source("job", root_id, labels.get(kind, "项目工作")),),
            )
        )
    return tuple(summaries)


def _action(
    *,
    kind: str,
    subject_id: Any,
    title: str,
    explanation: str,
    safe_action: str,
    source_refs: Sequence[WorkspaceSourceRef],
) -> WorkspaceAction:
    return WorkspaceAction(
        id=f"{kind}:{subject_id}",
        kind=kind,
        title=title,
        explanation=explanation,
        safe_action=safe_action,
        source_refs=tuple(source_refs),
    )


def _first_matching(items: Sequence[Any], *, status: str) -> Any | None:
    matches = [item for item in items if _value(item, "status") == status]
    if not matches:
        return None
    return min(matches, key=lambda item: str(_value(item, "id")))


def build_workspace_projection(
    snapshot: Mapping[str, Any],
    *,
    event_cursor: int,
    generated_at: datetime | None = None,
) -> ProjectWorkspaceProjectionV1:
    """Build a user-facing read model without mutating canonical domain state."""

    now = generated_at or datetime.now(UTC)
    project = snapshot["project"]
    project_id = str(_value(project, "id"))
    project_ref = _source("project", project_id, str(_value(project, "name", "项目")))
    project_updated_at = _as_datetime(_value(project, "updated_at"), fallback=now)
    runtime = snapshot.get("runtime", {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    work = _root_work_summaries(runtime, generated_at=now)
    latest_work = work[-1] if work else None
    pending_decision = _first_matching(snapshot.get("decisions", ()), status="pending")
    proposed_impact = _first_matching(snapshot.get("impacts", ()), status="proposed")
    proposed_solution = _first_matching(
        snapshot.get("solution_proposals", ()), status="proposed"
    )

    attention: WorkspaceAttention
    actions: tuple[WorkspaceAction, ...]
    refs: tuple[WorkspaceSourceRef, ...]
    if pending_decision is not None:
        decision_id = str(_value(pending_decision, "id"))
        affected = tuple(str(item) for item in _value(pending_decision, "affected_module_ids", ()))
        refs = (project_ref, _source("decision", decision_id, "待确认的选择"))
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title="有一项选择等你确认",
            reason="资料已经整理成可比较的选项；只有你的确认才会进入方案。",
            since=_as_datetime(_value(pending_decision, "created_at"), fallback=project_updated_at),
            affected_module_ids=affected,
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_decision",
                subject_id=decision_id,
                title="查看证据与选项",
                explanation="比较依据、风险和适用范围后再作决定。",
                safe_action="打开待确认选项",
                source_refs=refs,
            ),
        )
    elif proposed_impact is not None:
        impact_id = str(_value(proposed_impact, "id"))
        affected = tuple(str(item) for item in _value(proposed_impact, "affected_module_ids", ()))
        refs = (project_ref, _source("impact", impact_id, "现场变化影响"))
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title="现场变化的影响已整理",
            reason="系统只提出修改建议，不会在你确认前改写当前方案。",
            since=_as_datetime(_value(proposed_impact, "created_at"), fallback=project_updated_at),
            affected_module_ids=affected,
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_impact",
                subject_id=impact_id,
                title="检查影响范围",
                explanation="确认哪些组成部分会变化、哪些保持不变。",
                safe_action="查看修改建议",
                source_refs=refs,
            ),
        )
    elif proposed_solution is not None and not _value(project, "active_solution_version_id"):
        proposal_id = str(_value(proposed_solution, "id"))
        refs = (project_ref, _source("solution_proposal", proposal_id, "待确认方案"))
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title="第一版方案已准备好",
            reason="方案包含材料、步骤、验证方法和风险；确认后才会冻结为项目版本。",
            since=_as_datetime(
                _value(proposed_solution, "created_at"), fallback=project_updated_at
            ),
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_solution",
                subject_id=proposal_id,
                title="检查并确认方案",
                explanation="先核对组成部分、材料和验收方法。",
                safe_action="打开方案提案",
                source_refs=refs,
            ),
        )
    elif not _value(project, "active_requirement_revision_id"):
        attention = WorkspaceAttention(
            state=AttentionState.NEEDS_INPUT,
            title="先把想法变成可执行需求",
            reason="目标、硬约束、已有资源和未知问题尚未确认，研究现在不会自动开始。",
            since=project_updated_at,
            source_refs=(project_ref,),
        )
        actions = (
            _action(
                kind="clarify_requirements",
                subject_id=project_id,
                title="补充并确认需求",
                explanation="说明必须满足的条件、偏好、已有材料和仍不确定的地方。",
                safe_action="编辑需求草稿",
                source_refs=(project_ref,),
            ),
        )
    elif latest_work is not None and latest_work.state == "failed":
        refs = latest_work.source_refs
        attention = WorkspaceAttention(
            state=AttentionState.RECOVERABLE_FAILURE,
            title="最近一次工作没有完成",
            reason=(
                f"失败原因：{latest_work.latest_error}。项目和组成部分事实没有被改写。"
                if latest_work.latest_error
                else "本次工作已停止，项目和组成部分事实没有被改写。"
            ),
            since=latest_work.updated_at,
            source_refs=refs,
        )
        actions = (
            _action(
                kind="inspect_failure",
                subject_id=latest_work.run_id,
                title="查看失败原因",
                explanation=(
                    "当前版本只提供安全检查；自动重试将在运行时支持 generation 前置条件后开放。"
                ),
                safe_action="查看工作记录",
                source_refs=refs,
            ),
        )
    elif latest_work is not None and latest_work.state in {"queued", "running"}:
        attention = WorkspaceAttention(
            state=AttentionState.WORKING,
            title=latest_work.user_label,
            reason="后台工作正在推进；只有出现选择、缺少输入或安全边界时才需要你介入。",
            since=latest_work.started_at,
            source_refs=latest_work.source_refs,
        )
        actions = (
            _action(
                kind="follow_work",
                subject_id=latest_work.run_id,
                title="查看工作动态",
                explanation="可以离开本页，已完成的证据和结果会保存在项目中。",
                safe_action="展开工作记录",
                source_refs=latest_work.source_refs,
            ),
        )
    elif not snapshot.get("decisions"):
        attention = WorkspaceAttention(
            state=AttentionState.UP_TO_DATE,
            title="需求已确认，可以开始查资料",
            reason="研究会围绕已确认的组成部分收集证据，并在需要判断时停下来。",
            since=project_updated_at,
            source_refs=(project_ref,),
        )
        actions = (
            _action(
                kind="start_research",
                subject_id=project_id,
                title="开始研究",
                explanation="系统会先查找和核对资料，不会自动替你作最终选择。",
                safe_action="启动资料研究",
                source_refs=(project_ref,),
            ),
        )
    else:
        attention = WorkspaceAttention(
            state=AttentionState.UP_TO_DATE,
            title="项目已同步到最新记录",
            reason="目前没有等待确认或正在运行的工作；可以继续搭建、验证或记录现场变化。",
            since=project_updated_at,
            source_refs=(project_ref,),
        )
        actions = (
            _action(
                kind="record_observation",
                subject_id=project_id,
                title="记录现场变化",
                explanation="把实物、预算或环境中的变化写入项目，再评估影响。",
                safe_action="添加现场记录",
                source_refs=(project_ref,),
            ),
        )

    modules = tuple(
        WorkspaceModuleSummary(
            id=str(_value(item, "id")),
            name=str(_value(item, "name")),
            responsibility=str(_value(item, "responsibility")),
            domain_stage=str(_value(item, "stage")),
            work_state=None,
            open_question_count=len(_value(item, "open_questions", ())),
            dependency_count=len(_value(item, "dependency_ids", ())),
        )
        for item in snapshot.get("modules", ())
    )
    warnings = (
        ("runtime_module_relation_unavailable",)
        if modules and work
        else ()
    )
    return ProjectWorkspaceProjectionV1(
        generated_at=now,
        project_revision=int(_value(project, "revision", 1) or 1),
        event_cursor=event_cursor,
        attention=attention,
        next_actions=actions,
        modules=modules,
        work=work,
        warnings=warnings,
    )
