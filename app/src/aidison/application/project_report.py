"""Deterministic, evidence-linked export for one approved project solution."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from aidison.domain.models import (
    Candidate,
    CompatibilityFinding,
    DecisionRequest,
    EvidenceBinding,
    Module,
    Project,
    RequirementRevision,
    SolutionVersion,
)


def _text(value: object) -> str:
    """Keep user/model text readable without allowing it to reshape Markdown."""

    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _bullet_lines(values: Sequence[object], *, empty: str = "无") -> list[str]:
    return [f"- {_text(value)}" for value in values] or [f"- {empty}"]


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _step_line(step: dict[str, object], index: int) -> str:
    title = _text(step.get("title") or step.get("step") or f"步骤 {index}")
    instruction = _text(step.get("instruction") or "")
    acceptance = _text(step.get("acceptance") or step.get("expected_result") or "")
    detail = "；".join(item for item in (instruction, acceptance) if item)
    return f"{index}. {title}{f' — {detail}' if detail else ''}"


def render_project_report(
    *,
    project: Project,
    requirement: RequirementRevision,
    modules: Sequence[Module],
    solution: SolutionVersion,
    decisions: Sequence[DecisionRequest],
    candidates: Sequence[Candidate],
    evidence: Sequence[EvidenceBinding],
    findings: Sequence[CompatibilityFinding],
    generated_at: datetime | None = None,
) -> str:
    """Render the approved relational truth; never ask a model to rewrite it."""

    generated = generated_at or datetime.now(UTC)
    module_by_id = {item.id: item for item in modules}
    candidate_by_id = {item.id: item for item in candidates}
    selected_evidence = [item for item in evidence if item.id in set(solution.evidence_binding_ids)]
    evidence_numbers = {item.id: index for index, item in enumerate(selected_evidence, 1)}
    selected_findings = [
        item for item in findings if item.id in set(solution.compatibility_finding_ids)
    ]
    approved_decisions = [item for item in decisions if item.id == solution.approved_decision_id]

    lines = [
        f"# {_text(project.name)} 工程决策报告",
        "",
        "> 本报告由 Aidison 从已批准的项目事实、候选、证据和方案版本确定性生成；"
        "未让模型重新概括或补写事实。",
        "",
        "## 报告元数据",
        "",
        f"- 项目 ID：`{project.id}`",
        f"- 项目修订：`r{project.revision}`",
        f"- 方案版本：`V{solution.version}`",
        f"- 方案 basis：`{solution.basis_hash}`",
        f"- 生成时间：`{generated.isoformat()}`",
        "",
        "## 目标与约束",
        "",
        _text(requirement.goal),
        "",
        "### 硬约束",
        "",
        *_bullet_lines(requirement.hard_constraints),
        "",
        "### 偏好",
        "",
        *_bullet_lines(requirement.preferences),
        "",
        "### 可用资源",
        "",
        *_bullet_lines(requirement.available_resources),
        "",
        "### 尚未消除的不确定项",
        "",
        *_bullet_lines(requirement.unknowns),
        "",
        "## 模块与接口边界",
        "",
    ]
    for module in modules:
        dependencies = [
            module_by_id[item].name for item in module.dependency_ids if item in module_by_id
        ]
        lines.extend(
            [
                f"### {_text(module.name)} (`{module.key}`)",
                "",
                _text(module.responsibility),
                "",
                f"- 依赖：{_text('、'.join(dependencies) or '无')}",
                f"- 验收：{_text('；'.join(module.acceptance) or '未登记')}",
                f"- 待确认：{_text('；'.join(module.open_questions) or '无')}",
                "",
            ]
        )

    lines.extend(["## 已批准决策", ""])
    for decision in approved_decisions:
        selected = next(
            (item for item in decision.options if item.option_id == decision.selected_option_id),
            None,
        )
        lines.extend(
            [
                f"### {_text(decision.question)}",
                "",
                f"- 选择：{_text(selected.label if selected else decision.selected_option_id)}",
                f"- 理由：{_text(selected.summary if selected else '历史决策详情不可用')}",
                f"- 风险：{_text('；'.join(selected.risks) if selected else '未登记')}",
                "",
            ]
        )
    if not approved_decisions:
        lines.extend(["- 当前方案关联的历史决策不可用。", ""])

    lines.extend(["## 模块选型", ""])
    for snapshot in solution.module_snapshots:
        module_id = snapshot.get("module_id")
        candidate_id = snapshot.get("candidate_id")
        module = module_by_id.get(_uuid_or_none(module_id))
        candidate = candidate_by_id.get(_uuid_or_none(candidate_id))
        evidence_ids = snapshot.get("evidence_binding_ids") or ()
        refs = [f"[{evidence_numbers[item]}]" for item in evidence_ids if item in evidence_numbers]
        candidate_name = (
            candidate.name if candidate else snapshot.get("candidate_name", candidate_id)
        )
        rationale = snapshot.get("rationale", candidate.description if candidate else "")
        lines.extend(
            [
                f"### {_text(module.name if module else module_id)}",
                "",
                f"- 选型：{_text(candidate_name)}",
                f"- 说明：{_text(rationale)}",
                f"- 证据：{' '.join(refs) or '未绑定'}",
                "",
            ]
        )

    lines.extend(["## BOM", "", "| 物料 | 数量 | 单位 |", "|---|---:|---|"])
    for item in solution.bom:
        lines.append(
            f"| {_text(item.get('name') or item.get('item') or '未命名')} | "
            f"{_text(item.get('quantity', ''))} | {_text(item.get('unit', ''))} |"
        )
    if not solution.bom:
        lines.append("| 暂无 |  |  |")

    lines.extend(["", "## 实施步骤", ""])
    lines.extend(
        _step_line(step, index) for index, step in enumerate(solution.implementation_steps, 1)
    )
    if not solution.implementation_steps:
        lines.append("1. 未登记实施步骤。")

    lines.extend(["", "## 验证步骤", ""])
    lines.extend(
        _step_line(step, index) for index, step in enumerate(solution.verification_steps, 1)
    )
    if not solution.verification_steps:
        lines.append("1. 未登记验证步骤。")

    lines.extend(["", "## 兼容性结论", ""])
    for finding in selected_findings:
        lines.append(
            f"- **{_text(finding.status.value)}** `{finding.rule_id}`：{_text(finding.summary)}"
        )
    if not selected_findings:
        lines.append("- 当前方案未绑定兼容性结论。")

    lines.extend(["", "## 证据附录", ""])
    for item in selected_evidence:
        module = module_by_id.get(item.module_id)
        module_name = module.name if module else item.module_id
        lines.extend(
            [
                f"### [{evidence_numbers[item.id]}] {_text(module_name)}",
                "",
                f"- 结论：{_text(item.claim)}",
                f"- 状态：`{item.status.value}`",
                f"- 来源：{item.source_url}",
                f"- 快照：`{item.snapshot_hash}`",
                f"- 观察时间：`{item.observed_at.isoformat()}`",
                "",
                f"> {_text(item.span_text)}",
                "",
            ]
        )
    if not selected_evidence:
        lines.append("- 当前方案未绑定可导出的证据。")

    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_project_report"]
