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
    partial_units: int = Field(default=0, ge=0)
    failed_units: int = Field(ge=0)
    latest_error: str | None = None
    failure_coverage_keys: tuple[str, ...] = ()
    affected_module_ids: tuple[str, ...] = ()
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


def _research_failure_scope(run: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return bounded, read-only research gaps for a workspace failure.

    The quality projection is diagnostic only.  It does not admit evidence,
    change a run, or infer missing coverage from raw model output.
    """

    quality = _value(run, "research_quality")
    if not isinstance(quality, Mapping):
        return (), ()
    raw_gaps = quality.get("gap_coverage_keys")
    if not isinstance(raw_gaps, Sequence) or isinstance(raw_gaps, (str, bytes)):
        return (), ()
    gap_keys = tuple(sorted({item for item in raw_gaps if isinstance(item, str) and item}))[:8]
    if not gap_keys:
        return (), ()

    raw_coverage = quality.get("coverage")
    if not isinstance(raw_coverage, Sequence) or isinstance(raw_coverage, (str, bytes)):
        return gap_keys, ()
    gap_set = set(gap_keys)
    module_ids: set[str] = set()
    for item in raw_coverage:
        if not isinstance(item, Mapping) or item.get("coverage_key") not in gap_set:
            continue
        raw_module_ids = item.get("module_ids")
        if not isinstance(raw_module_ids, Sequence) or isinstance(raw_module_ids, (str, bytes)):
            continue
        module_ids.update(
            module_id for module_id in raw_module_ids if isinstance(module_id, str) and module_id
        )
    return gap_keys, tuple(sorted(module_ids))[:16]


def _agent_run_work_summaries(
    agent_runs: Sequence[Any],
    *,
    generated_at: datetime,
) -> tuple[WorkspaceWorkSummary, ...]:
    """Derive the UI work list from the LangGraph control-plane run records.

    A LangGraph run owns its internal node/task progress in its checkpoint,
    rather than duplicating that progress in the retired Job/Attempt tables.
    The workspace deliberately exposes only the durable, user-facing run
    lifecycle here.
    """

    ordered_runs = sorted(
        agent_runs,
        key=lambda item: (
            _as_datetime(_value(item, "created_at"), fallback=generated_at),
            str(_value(item, "id")),
        ),
    )
    labels = {
        "research": "查找并核对资料",
        "solution": "整理可执行方案",
        "impact": "分析现场变化",
    }
    summaries: list[WorkspaceWorkSummary] = []
    for run in ordered_runs:
        run_id = str(_value(run, "id"))
        state = str(_value(run, "status", "queued"))
        started_at = _as_datetime(
            _value(run, "started_at") or _value(run, "created_at"),
            fallback=generated_at,
        )
        updated_at = _as_datetime(
            _value(run, "completed_at") or _value(run, "updated_at") or _value(run, "created_at"),
            fallback=started_at,
        )
        kind = str(_value(run, "kind", "work"))
        failure_coverage_keys, affected_module_ids = (
            _research_failure_scope(run) if kind == "research" else ((), ())
        )
        progress = _value(run, "research_progress")
        initial_task_count = (
            int(progress["initial_task_count"])
            if isinstance(progress, Mapping) and isinstance(progress.get("initial_task_count"), int)
            else 1
        )
        admitted_task_count = (
            int(progress["admitted_task_count"])
            if isinstance(progress, Mapping)
            and isinstance(progress.get("admitted_task_count"), int)
            else 0
        )
        succeeded_task_count = (
            int(progress["succeeded_task_count"])
            if isinstance(progress, Mapping)
            and isinstance(progress.get("succeeded_task_count"), int)
            else admitted_task_count
        )
        partial_task_count = (
            int(progress["partial_task_count"])
            if isinstance(progress, Mapping)
            and isinstance(progress.get("partial_task_count"), int)
            else 0
        )
        total_units = max(initial_task_count, admitted_task_count)
        summaries.append(
            WorkspaceWorkSummary(
                run_id=run_id,
                user_label=labels.get(kind, "处理项目工作"),
                kind=kind,
                state=state,
                started_at=started_at,
                updated_at=updated_at,
                total_units=total_units,
                completed_units=(
                    total_units if state == "succeeded" else succeeded_task_count
                ),
                partial_units=partial_task_count,
                failed_units=1 if state == "failed" else 0,
                latest_error=(
                    str(_value(run, "latest_error")).strip()[:500]
                    if _value(run, "latest_error")
                    else None
                ),
                failure_coverage_keys=failure_coverage_keys,
                affected_module_ids=affected_module_ids,
                source_refs=(
                    _source("agent_run", run_id, labels.get(kind, "项目工作")),
                ),
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
    work = _agent_run_work_summaries(snapshot.get("agent_runs", ()), generated_at=now)
    latest_work = work[-1] if work else None
    pending_decision = _first_matching(snapshot.get("decisions", ()), status="pending")
    proposed_impact = _first_matching(snapshot.get("impacts", ()), status="proposed")
    proposed_solution = _first_matching(snapshot.get("solution_proposals", ()), status="proposed")
    pending_requirements_change = _first_matching(
        snapshot.get("requirements_change_proposals", ()), status="proposed"
    )
    pending_reshape = _first_matching(snapshot.get("reshape_proposals", ()), status="proposed")
    pending_spend_budget = _first_matching(
        snapshot.get("spend_budget_proposals", ()), status="proposed"
    )
    pending_change_impact = _first_matching(
        snapshot.get("change_impact_previews", ()), status="proposed"
    )
    pending_execution_plan = _first_matching(
        snapshot.get("execution_plans", ()), status="proposed"
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
    elif pending_requirements_change is not None:
        change_id = str(_value(pending_requirements_change, "id"))
        refs = (
            project_ref,
            _source("requirements_change", change_id, "待审核的需求/结构变更"),
        )
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title="有一份需求与结构变更待你审核",
            reason="对话中的重写建议已整理成一份可审阅、可拒绝或可应用的提案；应用前不会改写需求、模块或蓝图。",
            since=_as_datetime(
                _value(pending_requirements_change, "created_at"),
                fallback=project_updated_at,
            ),
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_requirements_change",
                subject_id=change_id,
                title="查看需求与结构变更",
                explanation="核对新目标、约束与模块边界后再决定应用或拒绝。",
                safe_action="打开变更提案",
                source_refs=refs,
            ),
        )
    elif pending_reshape is not None:
        reshape_id = str(_value(pending_reshape, "id"))
        affected = tuple(str(item) for item in _value(pending_reshape, "affected_module_ids", ()))
        refs = (project_ref, _source("reshape_proposal", reshape_id, "待审核的结构草案"))
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title="有一份结构草案等你确认",
            reason="对话中的结构调整已整理成提案；应用前不会改动现有模块与蓝图。",
            since=_as_datetime(_value(pending_reshape, "created_at"), fallback=project_updated_at),
            affected_module_ids=affected,
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_reshape",
                subject_id=reshape_id,
                title="查看结构草案",
                explanation="确认哪些组成部分会变化、哪些保持不变。",
                safe_action="打开结构草案",
                source_refs=refs,
            ),
        )
    elif pending_spend_budget is not None:
        budget_id = str(_value(pending_spend_budget, "id"))
        budget_amount = str(_value(pending_spend_budget, "amount", "?"))
        budget_currency = str(_value(pending_spend_budget, "currency", "?"))
        refs = (
            project_ref,
            _source("spend_budget_proposal", budget_id, "待批准的预算提案"),
        )
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title=f"有一份预算提案等你批准（{budget_currency} {budget_amount}）",
            reason="对话中的预算调整已成为一份可审阅、可批准或拒绝的提案；批准前不会改写项目预算。",
            since=_as_datetime(
                _value(pending_spend_budget, "created_at"),
                fallback=project_updated_at,
            ),
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_spend_budget",
                subject_id=budget_id,
                title="查看预算提案与影响预览",
                explanation="核对金额、币种和对候选成本的影响后再决定批准或拒绝。",
                safe_action="打开预算提案",
                source_refs=refs,
            ),
        )
    elif pending_change_impact is not None:
        preview_id = str(_value(pending_change_impact, "id"))
        affected = tuple(
            str(item)
            for item in _value(pending_change_impact, "affected_module_ids", ())
        )
        invalidated_count = len(
            _value(pending_change_impact, "invalidated_refs", ())
        )
        refs = (
            project_ref,
            _source(
                "change_impact_preview", preview_id,
                "用户调整的影响预览",
            ),
        )
        reason_parts = ["你的调整已触发全局影响分析。"]
        if affected:
            reason_parts.append(
                f"直接影响 {len(affected)} 个模块，"
                f"不会在你确认前自动改写任何事实。"
            )
        else:
            reason_parts.append("系统只提供分析结果，不会自动改写任何事实。")
        if invalidated_count:
            reason_parts.append(
                f" 另有 {invalidated_count} 项待处理的推荐或研究因此失效。"
            )
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title="你的调整已触发全局影响分析",
            reason="".join(reason_parts),
            since=_as_datetime(
                _value(pending_change_impact, "created_at"),
                fallback=project_updated_at,
            ),
            affected_module_ids=affected,
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_change_impact",
                subject_id=preview_id,
                title="查看调整的影响范围",
                explanation="确认哪些模块会变化、哪些待处理建议已失效。",
                safe_action="打开影响预览",
                source_refs=refs,
            ),
        )
    elif pending_execution_plan is not None:
        plan_id = str(_value(pending_execution_plan, "id"))
        refs = (project_ref, _source("execution_plan", plan_id, "待批准的执行计划"))
        attention = WorkspaceAttention(
            state=AttentionState.READY_TO_REVIEW,
            title="有一份执行计划等你批准",
            reason="系统已把研究目标整理成一份有边界的执行计划；批准后才会按它启动受限工作，批准前不会触发任何后台任务。",
            since=_as_datetime(
                _value(pending_execution_plan, "created_at"),
                fallback=project_updated_at,
            ),
            source_refs=refs,
        )
        actions = (
            _action(
                kind="review_execution_plan",
                subject_id=plan_id,
                title="查看并批准执行计划",
                explanation="核对目标、并发上限、预算与允许的工具边界后再决定批准或拒绝。",
                safe_action="打开执行计划",
                source_refs=refs,
            ),
        )
    elif not _value(project, "active_requirement_revision_id"):
        attention = WorkspaceAttention(
            state=AttentionState.NEEDS_INPUT,
            title="先把想法变成可执行需求",
            reason="先确认使用场景与硬约束；预算、已有资源和技能可以暂记为未知，并在后续研究中按需补充。研究现在不会自动开始。",
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
    elif not _value(project, "active_blueprint_id"):
        attention = WorkspaceAttention(
            state=AttentionState.NEEDS_INPUT,
            title="请先生成并确认模块结构",
            reason="需求已确认。请主动让 AI 提出模块结构，确认后才会拟定深度研究计划。",
            since=project_updated_at,
            source_refs=(project_ref,),
        )
        actions = (
            _action(
                kind="discover_initial_modules",
                subject_id=project_id,
                title="让 AI 生成模块结构",
                explanation="这会发起一次受限模型调用，只生成待你确认的结构草案。",
                safe_action="生成结构草案",
                source_refs=(project_ref,),
            ),
        )
    elif latest_work is not None and latest_work.state == "failed":
        refs = latest_work.source_refs
        failure_reason = (
            f"失败原因：{latest_work.latest_error}。"
            if latest_work.latest_error
            else "本次工作已停止。"
        )
        if latest_work.failure_coverage_keys:
            displayed_gaps = latest_work.failure_coverage_keys[:3]
            hidden_gap_count = len(latest_work.failure_coverage_keys) - len(displayed_gaps)
            gaps_text = "、".join(displayed_gaps)
            if hidden_gap_count:
                gaps_text = f"{gaps_text} 等 {len(latest_work.failure_coverage_keys)} 项"
            failure_reason = (
                f"{failure_reason}关键覆盖缺口：{gaps_text}；"
                "请在工作记录中核对缺失证据或未准入的论断。"
            )
        attention = WorkspaceAttention(
            state=AttentionState.RECOVERABLE_FAILURE,
            title="最近一次工作没有完成",
            reason=f"{failure_reason}项目和组成部分事实没有被改写。",
            since=latest_work.updated_at,
            affected_module_ids=latest_work.affected_module_ids,
            source_refs=refs,
        )
        actions = (
            _action(
                kind="inspect_failure",
                subject_id=latest_work.run_id,
                title="查看失败原因",
                explanation=(
                    "研究运行失败，项目事实没有被改写。工作记录会展示覆盖缺口和未准入论断；"
                    "你可以核对后重新发起研究。"
                ),
                safe_action="查看工作记录或重新发起研究",
                source_refs=refs,
            ),
        )
    elif latest_work is not None and latest_work.state in {"queued", "running", "waiting"}:
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
    return ProjectWorkspaceProjectionV1(
        generated_at=now,
        project_revision=int(_value(project, "revision", 1) or 1),
        event_cursor=event_cursor,
        attention=attention,
        next_actions=actions,
        modules=modules,
        work=work,
        warnings=(),
    )
