"use client";

import { useCallback, useMemo, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import {
  Activity,
  ArrowLeft,
  Bot,
  Check,
  CircleAlert,
  Database,
  FlaskConical,
  GitBranch,
  RefreshCw,
  Search,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { useEventStream } from "@/app/hooks/useEventStream";
import { useViewState } from "@/app/hooks/useViewState";
import { ViewShell } from "@/app/components/ViewShell";
import { DraftWorkbench } from "@/app/components/DraftWorkbench";
import { RequirementsRevisionForm } from "@/app/components/RequirementsRevisionForm";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { getClient } from "@/lib/api";
import type {
  ConsoleView,
  DecisionOption,
  DecisionRequest,
  ImpactAnalysis,
  Module,
  Project,
  ProjectSnapshot,
  ProjectWorkspaceProjectionV1,
  SolutionProposal,
  SolutionVersion,
  WorkspaceAction,
} from "@/app/types/types";

const STAGES = [
  ["intake", "项目"],
  ["requirements", "需求"],
  ["research", "研究"],
  ["comparing", "比较"],
  ["deciding", "决策"],
  ["approved", "方案"],
  ["verifying", "验证"],
  ["revising", "修订"],
] as const;

interface ProjectConsoleProps {
  initialProject: Project;
  onBack: () => void;
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function normalizedDecisionOption(
  option: DecisionOption | string
): DecisionOption {
  if (typeof option !== "string") return option;
  return {
    option_id: option,
    label: option,
    summary: "历史选项未绑定候选与证据，仅供只读审计。",
    candidate_ids: [],
    evidence_binding_ids: [],
    risks: [],
    legacy_unbound: true,
  };
}

function moduleSnapshotHash(
  snapshot: SolutionVersion["module_snapshots"][number]
): string | null {
  return "snapshot_hash" in snapshot &&
    typeof snapshot.snapshot_hash === "string"
    ? snapshot.snapshot_hash
    : null;
}

function moduleSnapshotIdentity(
  snapshot: SolutionVersion["module_snapshots"][number]
): string {
  return moduleSnapshotHash(snapshot) ?? JSON.stringify(snapshot);
}

function bomLabel(item: SolutionVersion["bom"][number]): string {
  return "name" in item ? item.name : item.item;
}

function bomKey(item: SolutionVersion["bom"][number], index: number): string {
  return "line_id" in item ? item.line_id : `legacy-bom-${index}-${item.item}`;
}

function bomUnit(item: SolutionVersion["bom"][number]): string {
  return "unit" in item ? item.unit : "";
}

function stepLabel(
  step: SolutionVersion["implementation_steps"][number]
): string {
  return "title" in step ? step.title : step.step;
}

function stepInstruction(
  step: SolutionVersion["implementation_steps"][number]
): string {
  return "instruction" in step ? step.instruction : "";
}

function stepKey(
  step: SolutionVersion["implementation_steps"][number],
  index: number
): string {
  return "step_id" in step ? step.step_id : `legacy-step-${index}-${step.step}`;
}

function eventLabel(type: string): string {
  return type.replaceAll(".", " / ").replaceAll("_", " ");
}

const STATUS_LABELS: Record<string, string> = {
  approved: "已批准",
  blocked: "暂时受阻",
  cancelled: "已取消",
  complete: "已完成",
  failed: "未完成",
  needs_input: "需要补充",
  pending: "等待处理",
  queued: "等待开始",
  ready_to_review: "等待确认",
  recoverable_failure: "需要检查",
  running: "处理中",
  succeeded: "已完成",
  up_to_date: "已同步",
  working: "处理中",
};

function displayError(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message;
  if (error && typeof error === "object" && "error" in error) {
    const message = (error as { error?: { message?: unknown } }).error?.message;
    if (typeof message === "string" && message) return message;
  }
  return fallback;
}

function eventTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : date.toLocaleString("zh-CN");
}

function statusTone(status: string): string {
  if (
    [
      "succeeded",
      "approved",
      "supported",
      "compatible",
      "joined",
      "up_to_date",
      "complete",
    ].includes(status)
  ) {
    return "tone-good";
  }
  if (
    [
      "failed",
      "cancelled",
      "incompatible",
      "contradicted",
      "corrupt",
      "recoverable_failure",
      "blocked",
    ].includes(status)
  ) {
    return "tone-bad";
  }
  if (
    [
      "running",
      "queued",
      "pending",
      "needs_test",
      "conditional",
      "working",
      "needs_input",
      "ready_to_review",
    ].includes(status)
  ) {
    return "tone-live";
  }
  return "tone-muted";
}

function StatusTag({ status }: { status: string }) {
  return (
    <span className={`status-tag ${statusTone(status)}`}>
      {STATUS_LABELS[status] ?? status}
    </span>
  );
}

function StageRail({ project }: { project: Project }) {
  const activeIndex = STAGES.findIndex(([stage]) => stage === project.stage);
  return (
    <div
      className="stage-rail"
      aria-label={`当前阶段：${project.stage}`}
      role="list"
    >
      {STAGES.map(([stage, label], index) => (
        <div
          className={`stage-stop ${index < activeIndex ? "is-done" : ""} ${
            index === activeIndex ? "is-active" : ""
          }`}
          key={stage}
          role="listitem"
        >
          <span>
            {index < activeIndex ? <Check className="h-3 w-3" /> : index + 1}
          </span>
          <small>{label}</small>
        </div>
      ))}
    </div>
  );
}

const ATTENTION_LABELS: Record<
  ProjectWorkspaceProjectionV1["attention"]["state"],
  string
> = {
  up_to_date: "已同步",
  working: "处理中",
  needs_input: "需要补充",
  ready_to_review: "等你确认",
  recoverable_failure: "需要检查",
  blocked: "暂时受阻",
  complete: "已完成",
};

function ProjectPulse({
  workspace,
}: {
  workspace?: ProjectWorkspaceProjectionV1;
}) {
  if (!workspace) {
    return (
      <section
        className="project-pulse is-unavailable"
        aria-label="项目状态"
      >
        <div>
          <small>PROJECT PULSE</small>
          <h2>正在整理项目状态</h2>
        </div>
        <p>当前服务尚未返回用户态工作投影；项目事实仍可在下方查看。</p>
      </section>
    );
  }
  const activeWork = workspace.work.filter((item) =>
    ["queued", "running"].includes(item.state)
  );
  return (
    <section
      className={`project-pulse attention-${workspace.attention.state}`}
      aria-label="项目状态"
    >
      <header>
        <div>
          <small>PROJECT PULSE / 项目脉冲</small>
          <h2>{workspace.attention.title}</h2>
        </div>
        <StatusTag status={workspace.attention.state} />
      </header>
      <p>{workspace.attention.reason}</p>
      <div
        className="pulse-track"
        aria-label="项目状态摘要"
      >
        <div>
          <i />
          <span>项目事实</span>
          <strong>{workspace.modules.length} 个组成部分</strong>
        </div>
        <div>
          <i />
          <span>后台工作</span>
          <strong>
            {activeWork.length ? `${activeWork.length} 项进行中` : "当前空闲"}
          </strong>
        </div>
        <div>
          <i />
          <span>人工判断</span>
          <strong>{ATTENTION_LABELS[workspace.attention.state]}</strong>
        </div>
      </div>
    </section>
  );
}

function ModuleCard({
  module,
  onClick,
}: {
  module: Module;
  onClick?: () => void;
}) {
  return (
    <article
      className="module-card"
      tabIndex={0}
      role="button"
      aria-label={`查看模块 ${module.name} 详情`}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick?.();
        }
      }}
    >
      <div className="module-card-head">
        <code>{module.key}</code>
        <StatusTag status={module.stage} />
      </div>
      <h3>{module.name}</h3>
      <p>{module.responsibility}</p>
      {module.acceptance.length ? (
        <ul>
          {module.acceptance.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

function ResearchAction({
  snapshot,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const approvedPlan = (snapshot.execution_plans ?? [])
    .filter(
      (plan) =>
        plan.status === "approved" &&
        plan.allowed_coordination_modes.includes("decompose")
    )
    .at(-1);
  const start = async () => {
    if (!approvedPlan) {
      toast.error("请先在“执行边界”中批准一份允许分解研究的执行提案。");
      return;
    }
    setBusy(true);
    try {
      await getClient().startResearchRun(
        snapshot.project.id,
        snapshot.project.revision,
        approvedPlan.id
      );
      toast.success("有界研究已进入 durable Job 队列");
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "无法启动研究"));
    } finally {
      setBusy(false);
    }
  };
  const proposePlan = async () => {
    setBusy(true);
    try {
      await getClient().proposeDefaultResearchExecutionPlan(
        snapshot.project.id,
        snapshot.project.revision
      );
      toast.success("已生成研究执行范围草案，请在工作台确认后启动。");
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "无法生成研究执行范围"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="action-block research-action">
      <div className="action-title">
        <Search className="h-5 w-5" />
        <div>
          <small>查找资料</small>
          <h2>启动有界研究</h2>
        </div>
      </div>
      <p>
        PostgreSQL 持有 Job、Attempt、Delegation 与 JoinReceipt；Deep Agent
        只生成 typed Proposal。
      </p>
      <Button
        disabled={busy}
        onClick={() => void (approvedPlan ? start() : proposePlan())}
      >
        <Bot className="h-4 w-4" />
        {busy ? "正在准备…" : approvedPlan ? "开始查找资料" : "生成待确认执行范围"}
      </Button>
    </div>
  );
}

function DecisionAction({
  snapshot,
  decision,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  decision: DecisionRequest;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const approvedPlan = (snapshot.execution_plans ?? [])
    .filter(
      (plan) =>
        plan.status === "approved" &&
        plan.allowed_coordination_modes.includes("decompose")
    )
    .at(-1);
  const options = decision.options.map(normalizedDecisionOption);
  const choose = async (optionId: string) => {
    if (!approvedPlan) {
      toast.error("请先批准一份允许分解执行的执行提案。");
      return;
    }
    setBusy(optionId);
    try {
      await getClient().resolveDecision(
        decision.id,
        snapshot.project.revision,
        optionId,
        decision.basis_hash,
        approvedPlan.id
      );
      toast.success("决策已写入 canonical state");
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "无法确认这个选项"));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="action-block decision-action">
      <div className="action-title">
        <GitBranch className="h-5 w-5" />
        <div>
          <small>需要确认</small>
          <h2>{decision.question}</h2>
        </div>
      </div>
      <div className="decision-options">
        {options.map((option) => (
          <button
            disabled={busy !== null || option.legacy_unbound}
            key={option.option_id}
            onClick={() => void choose(option.option_id)}
          >
            <span>{option.label}</span>
            <small>{option.summary}</small>
            <code>
              {option.option_id} · candidates {option.candidate_ids.length} ·
              evidence {option.evidence_binding_ids.length}
            </code>
          </button>
        ))}
      </div>
    </div>
  );
}

function FreezeAction({
  snapshot,
  proposal,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  proposal: SolutionProposal;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const freeze = async () => {
    setBusy(true);
    try {
      await getClient().freezeSolution(
        snapshot.project.id,
        snapshot.project.revision,
        {
          solution_proposal_id: proposal.id,
          basis_hash: proposal.basis_hash,
        }
      );
      toast.success("不可变 SolutionVersion 已冻结");
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "无法确认方案"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="action-block">
      <div className="action-title">
        <ShieldCheck className="h-5 w-5" />
        <div>
          <small>确认方案</small>
          <h2>把已批准决策冻结为方案</h2>
        </div>
      </div>
      <p>
        方案由服务端接纳的{" "}
        <code>
          {proposal.profile_id}@{proposal.profile_revision}
        </code>{" "}
        Proposal 生成；浏览器只能批准 proposal ID 和精确 basis，不能上传 BOM
        或步骤。
      </p>
      <div className="decision-options">
        {proposal.module_selections.map((selection) => (
          <article key={selection.module_id}>
            <strong>{selection.candidate_name}</strong>
            <small>{selection.rationale}</small>
          </article>
        ))}
      </div>
      <p>
        BOM {proposal.bom.length} 项 · 实施步骤{" "}
        {proposal.implementation_steps.length} · 台架验证{" "}
        {proposal.verification_steps.length} · unknown{" "}
        {proposal.unknowns.length}
      </p>
      <Button
        disabled={busy}
        onClick={() => void freeze()}
      >
        {busy ? "正在冻结…" : "冻结 SolutionVersion v1"}
      </Button>
    </div>
  );
}

function ObservationAction({
  snapshot,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  onDone: () => Promise<unknown>;
}) {
  const [statement, setStatement] = useState("");
  const [selected, setSelected] = useState<string[]>(() =>
    snapshot.modules[0] ? [snapshot.modules[0].id] : []
  );
  const [busy, setBusy] = useState(false);
  const approvedPlan = (snapshot.execution_plans ?? [])
    .filter(
      (plan) =>
        plan.status === "approved" &&
        plan.allowed_coordination_modes.includes("decompose")
    )
    .at(-1);
  const submit = async () => {
    if (!approvedPlan) {
      toast.error("请先批准一份允许分解执行的执行提案。");
      return;
    }
    setBusy(true);
    try {
      await getClient().submitObservation(
        snapshot.project.id,
        snapshot.project.revision,
        statement,
        selected,
        approvedPlan.id
      );
      setStatement("");
      toast.success("现场观察已记录，durable impact-proposer 已入队");
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "无法记录这次观察"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="action-block">
      <div className="action-title">
        <FlaskConical className="h-5 w-5" />
        <div>
          <small>记录变化</small>
          <h2>记录一次制造或测试观察</h2>
        </div>
      </div>
      <Textarea
        rows={3}
        value={statement}
        onChange={(event) => setStatement(event.target.value)}
        placeholder="例如：试装后发现结构在满载时振动，电池固定带会位移。"
      />
      <div className="module-checks">
        {snapshot.modules.map((module) => (
          <label key={module.id}>
            <input
              type="checkbox"
              checked={selected.includes(module.id)}
              onChange={(event) =>
                setSelected((current) =>
                  event.target.checked
                    ? [...current, module.id]
                    : current.filter((id) => id !== module.id)
                )
              }
            />
            {module.name}
          </label>
        ))}
      </div>
      <Button
        disabled={busy || !statement.trim() || selected.length === 0}
        onClick={() => void submit()}
      >
        提交 Observation
      </Button>
    </div>
  );
}

function ImpactAction({
  snapshot,
  impact,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  impact: ImpactAnalysis;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const approve = async () => {
    setBusy(true);
    try {
      await getClient().approveImpact(
        impact.id,
        snapshot.project.revision,
        impact.basis_hash
      );
      toast.success("PatchSet 已批准，新 SolutionVersion 已创建");
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "无法确认影响分析"));
    } finally {
      setBusy(false);
    }
  };
  const moduleById = new Map(
    snapshot.modules.map((module) => [module.id, module])
  );
  const moduleNames = (ids: string[]) =>
    ids.map((id) => moduleById.get(id)?.name ?? shortId(id)).join("、") || "无";
  return (
    <div className="action-block impact-action">
      <div className="action-title">
        <Wrench className="h-5 w-5" />
        <div>
          <small>确认影响</small>
          <h2>批准影响分析和模块补丁</h2>
        </div>
      </div>
      <p>{impact.summary}</p>
      <div
        className="impact-partition"
        aria-label="影响闭包"
      >
        <article>
          <small>DIRECT / 用户事实</small>
          <strong>{moduleNames(impact.direct_affected_module_ids)}</strong>
        </article>
        <article>
          <small>TRANSITIVE / 依赖传播</small>
          <strong>{moduleNames(impact.transitive_affected_module_ids)}</strong>
        </article>
        <article className="is-reused">
          <small>REUSED / 原样复用</small>
          <strong>{moduleNames(impact.unaffected_module_ids)}</strong>
        </article>
      </div>
      <div className="impact-patches">
        {impact.module_patches.map((patch) => (
          <article key={patch.module_id}>
            <div>
              <StatusTag status="proposed" />
              <code>{patch.base_snapshot_hash.slice(0, 12)}…</code>
            </div>
            <strong>
              {moduleById.get(patch.module_id)?.name ??
                shortId(patch.module_id)}
            </strong>
            <span>→ {patch.replacement.candidate_name}</span>
            <small>{patch.replacement.rationale}</small>
          </article>
        ))}
      </div>
      <p className="impact-meta">
        replacement BOM {impact.replacement_bom_items.length} · 实施步骤{" "}
        {impact.replacement_implementation_steps.length} · 验证步骤{" "}
        {impact.replacement_verification_steps.length} · stale evidence{" "}
        {impact.stale_evidence_binding_ids.length}
      </p>
      {impact.risks.length ? <p>风险：{impact.risks.join("；")}</p> : null}
      <p>
        Proposal：
        <code>
          {impact.profile_id}@{impact.profile_revision}
        </code>
        。浏览器只批准该 basis， 不能编辑 PatchSet。
      </p>
      <Button
        disabled={busy || impact.status !== "proposed" || !impact.artifact_ref}
        onClick={() => void approve()}
      >
        {busy ? "正在创建新版本…" : "批准 PatchSet"}
      </Button>
    </div>
  );
}

function actionSourceId(
  action: WorkspaceAction,
  type: string
): string | undefined {
  return action.source_refs.find((item) => item.type === type)?.id;
}

function ActionNotice({
  action,
  onOpenAudit,
}: {
  action?: WorkspaceAction;
  onOpenAudit?: () => void;
}) {
  return (
    <div className="action-block">
      <div className="action-title">
        <Bot className="h-5 w-5" />
        <div>
          <small>NEXT / 服务端建议</small>
          <h2>{action?.title ?? "等待项目状态同步"}</h2>
        </div>
      </div>
      <p>
        {action?.explanation ?? "当前没有可安全执行的下一步，请刷新项目状态。"}
      </p>
      {action ? (
        ["follow_work", "inspect_failure"].includes(action.kind) &&
        onOpenAudit ? (
          <Button
            variant="outline"
            onClick={onOpenAudit}
          >
            {action.safe_action}
          </Button>
        ) : (
          <div className="run-callout">{action.safe_action}</div>
        )
      ) : null}
    </div>
  );
}

function NextAction({
  snapshot,
  onDone,
  onOpenAudit,
}: {
  snapshot: ProjectSnapshot;
  onDone: () => Promise<unknown>;
  onOpenAudit: () => void;
}) {
  const action = snapshot.workspace?.next_actions[0];
  if (!action) return <ActionNotice onOpenAudit={onOpenAudit} />;

  switch (action.kind) {
    case "clarify_requirements":
      return (
        <RequirementsRevisionForm
          snapshot={snapshot}
          onDone={onDone}
        />
      );
    case "start_research":
      return (
        <ResearchAction
          snapshot={snapshot}
          onDone={onDone}
        />
      );
    case "review_decision": {
      const decisionId = actionSourceId(action, "decision");
      const decision = snapshot.decisions.find(
        (item) => item.id === decisionId
      );
      return decision ? (
        <DecisionAction
          snapshot={snapshot}
          decision={decision}
          onDone={onDone}
        />
      ) : (
        <ActionNotice
          action={action}
          onOpenAudit={onOpenAudit}
        />
      );
    }
    case "review_solution": {
      const proposalId = actionSourceId(action, "solution_proposal");
      const proposal = snapshot.solution_proposals.find(
        (item) => item.id === proposalId
      );
      return proposal ? (
        <FreezeAction
          snapshot={snapshot}
          proposal={proposal}
          onDone={onDone}
        />
      ) : (
        <ActionNotice
          action={action}
          onOpenAudit={onOpenAudit}
        />
      );
    }
    case "review_impact": {
      const impactId = actionSourceId(action, "impact");
      const impact = snapshot.impacts.find((item) => item.id === impactId);
      return impact ? (
        <ImpactAction
          snapshot={snapshot}
          impact={impact}
          onDone={onDone}
        />
      ) : (
        <ActionNotice
          action={action}
          onOpenAudit={onOpenAudit}
        />
      );
    }
    case "record_observation":
      return (
        <ObservationAction
          snapshot={snapshot}
          onDone={onDone}
        />
      );
    default:
      return (
        <ActionNotice
          action={action}
          onOpenAudit={onOpenAudit}
        />
      );
  }
}

function SolutionVersionCard({
  solution,
  previous,
  impact,
  moduleById,
  hasPatchSet,
}: {
  solution: SolutionVersion;
  previous?: SolutionVersion;
  impact?: ImpactAnalysis;
  moduleById: Map<string, Module>;
  hasPatchSet: boolean;
}) {
  const previousSnapshots = new Map(
    (previous?.module_snapshots ?? []).map((snapshot) => [
      snapshot.module_id,
      snapshot,
    ])
  );
  const affected = new Set(impact?.affected_module_ids ?? []);
  const changedCount = solution.module_snapshots.reduce(
    (count, snapshot) =>
      count +
      (previousSnapshots.has(snapshot.module_id) &&
      moduleSnapshotIdentity(previousSnapshots.get(snapshot.module_id)!) !==
        moduleSnapshotIdentity(snapshot)
        ? 1
        : 0),
    0
  );
  return (
    <article className="solution-version-card">
      <header>
        <div>
          <strong>V{solution.version}</strong>
          <span>{new Date(solution.created_at).toLocaleString("zh-CN")}</span>
        </div>
        <div className="version-badges">
          <StatusTag status={previous ? "revised" : "approved"} />
          {hasPatchSet ? <small>SERVER PATCH</small> : null}
        </div>
      </header>
      <code>{solution.basis_hash.slice(0, 16)}…</code>
      {previous ? (
        <div
          className="reuse-rail"
          aria-label={`V${previous.version} 到 V${solution.version} 语义差异`}
        >
          {solution.module_snapshots.map((snapshot) => {
            const prior = previousSnapshots.get(snapshot.module_id);
            const changed = prior
              ? moduleSnapshotIdentity(prior) !==
                moduleSnapshotIdentity(snapshot)
              : true;
            const wasAffected = affected.has(snapshot.module_id);
            return (
              <div
                className={changed ? "is-changed" : "is-reused"}
                key={snapshot.module_id}
              >
                <i />
                <span>
                  {moduleById.get(snapshot.module_id)?.name ??
                    shortId(snapshot.module_id)}
                </span>
                <small>
                  {changed
                    ? "changed"
                    : wasAffected
                    ? "affected · reused"
                    : "reused"}
                </small>
                <code>
                  {moduleSnapshotHash(snapshot)?.slice(0, 10) ?? "legacy"}
                </code>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="reuse-rail is-origin">
          {solution.module_snapshots.map((snapshot) => (
            <div key={snapshot.module_id}>
              <i />
              <span>
                {moduleById.get(snapshot.module_id)?.name ??
                  shortId(snapshot.module_id)}
              </span>
              <small>origin</small>
              <code>
                {moduleSnapshotHash(snapshot)?.slice(0, 10) ?? "legacy"}
              </code>
            </div>
          ))}
        </div>
      )}
      <div className="version-metrics">
        <span>
          BOM {solution.bom.length}
          {previous
            ? ` (${solution.bom.length - previous.bom.length >= 0 ? "+" : ""}${
                solution.bom.length - previous.bom.length
              })`
            : ""}
        </span>
        <span>实施 {solution.implementation_steps.length}</span>
        <span>
          验证 {solution.verification_steps.length}
          {previous
            ? ` (${
                solution.verification_steps.length -
                  previous.verification_steps.length >=
                0
                  ? "+"
                  : ""
              }${
                solution.verification_steps.length -
                previous.verification_steps.length
              })`
            : ""}
        </span>
        {previous ? <span>变更模块 {changedCount}</span> : null}
      </div>
      <details>
        <summary>查看 BOM 与工程步骤</summary>
        <div className="version-detail-grid">
          <section>
            <small>BOM</small>
            {solution.bom.map((item, index) => (
              <p key={bomKey(item, index)}>
                {bomLabel(item)} × {item.quantity} {bomUnit(item)}
              </p>
            ))}
          </section>
          <section>
            <small>IMPLEMENT</small>
            {solution.implementation_steps.map((step, index) => (
              <p key={stepKey(step, index)}>
                <strong>{stepLabel(step)}</strong>
                {stepInstruction(step) ? ` — ${stepInstruction(step)}` : ""}
              </p>
            ))}
          </section>
          <section>
            <small>VERIFY</small>
            {solution.verification_steps.map((step, index) => (
              <p key={stepKey(step, index)}>
                <strong>{stepLabel(step)}</strong>
                {stepInstruction(step) ? ` — ${stepInstruction(step)}` : ""}
              </p>
            ))}
          </section>
        </div>
      </details>
    </article>
  );
}

export function ProjectConsole({
  initialProject,
  onBack,
}: ProjectConsoleProps) {
  const [auditOpen, setAuditOpen] = useState(false);
  const { data, error, isLoading, mutate } = useSWR(
    ["project-snapshot", initialProject.id],
    () => getClient().getSnapshot(initialProject.id),
    { keepPreviousData: true }
  );
  const refresh = useCallback(async () => mutate(), [mutate]);
  const onRuntimeEvent = useCallback(() => {
    void mutate();
  }, [mutate]);
  const { connected, events } = useEventStream(
    initialProject.id,
    onRuntimeEvent
  );

  // URL-recoverable view state
  const {
    view,
    moduleId,
    artifactId,
    decisionId,
    solutionVersionId,
    setView,
    selectModule,
    selectArtifact,
    selectDecision,
    selectSolutionVersion,
    clearOverlay,
  } = useViewState();
  const snapshot = data;
  const project = snapshot?.project || initialProject;
  const latestEvents = useMemo(() => events.slice(-20).toReversed(), [events]);

  if (isLoading && !snapshot) {
    return (
      <div className="console-loading">
        正在读取 canonical project snapshot…
      </div>
    );
  }
  if (error || !snapshot) {
    return (
      <div className="console-loading error-state">
        <CircleAlert className="h-6 w-6" />
        <p>
          无法读取项目：
          {error instanceof Error ? error.message : "unknown error"}
        </p>
        <Button onClick={() => void mutate()}>重试</Button>
      </div>
    );
  }

  return (
    <main className="console-shell">
      <header className="console-header">
        <div className="console-title-row">
          <Button
            variant="ghost"
            size="sm"
            onClick={onBack}
          >
            <ArrowLeft className="h-4 w-4" />
            项目列表
          </Button>
          <div className="project-title">
            <small>PROJECT / {shortId(project.id)}</small>
            <h1>{project.name}</h1>
          </div>
          <div className="truth-badge">
            <Database className="h-4 w-4" />
            <span>REV {String(project.revision).padStart(2, "0")}</span>
            <i className={connected ? "is-online" : ""} />
            {connected ? "LIVE" : "RECONNECTING"}
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void refresh()}
          >
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
        </div>
        <StageRail project={project} />
      </header>

      <div className="console-body">
        {/* Left module board — always visible */}
        <aside className="module-board">
          <div className="panel-heading">
            <div>
              <small>MODULE GRAPH</small>
              <h2>工程模块</h2>
            </div>
            <span>{snapshot.modules.length}</span>
          </div>
          <div className="module-list">
            {snapshot.modules.length ? (
              snapshot.modules.map((module) => (
                <ModuleCard
                  key={module.id}
                  module={module}
                  onClick={() => selectModule(module.id)}
                />
              ))
            ) : (
              <p className="empty-copy">
                批准需求后，这里会显示通用 DIY 模块。
              </p>
            )}
          </div>
        </aside>

        {/* Center: V0 主线动作 + SolutionVersions + ViewShell with View Navigation */}
        <section className="workbench">
          <ProjectPulse workspace={snapshot.workspace} />
          <div className="goal-strip">
            <span>GOAL</span>
            <p>{project.goal}</p>
          </div>
          <DraftWorkbench snapshot={snapshot} onRefresh={refresh} />
          <NextAction
            snapshot={snapshot}
            onDone={refresh}
            onOpenAudit={() => setAuditOpen(true)}
          />

          <section className="solution-board">
            <div className="panel-heading">
              <div>
                <small>IMMUTABLE VERSIONS</small>
                <h2>方案版本</h2>
              </div>
              <span>{snapshot.solutions.length}</span>
            </div>
            <div className="version-line">
              {snapshot.solutions.map((solution) => {
                const previous = solution.previous_version_id
                  ? (() => {
                      const sid = new Map(
                        snapshot.solutions.map((s) => [s.id, s])
                      );
                      return sid.get(solution.previous_version_id);
                    })()
                  : undefined;
                const moduleById = new Map(
                  snapshot.modules.map((m) => [m.id, m])
                );
                const impactByBase = new Map(
                  snapshot.impacts.map((i) => [i.base_solution_version_id, i])
                );
                return (
                  <SolutionVersionCard
                    key={solution.id}
                    solution={solution}
                    previous={previous}
                    impact={
                      previous ? impactByBase.get(previous.id) : undefined
                    }
                    moduleById={moduleById}
                    hasPatchSet={snapshot.patch_sets.some(
                      (patch) =>
                        patch.base_solution_version_id ===
                        solution.previous_version_id
                    )}
                  />
                );
              })}
              {!snapshot.solutions.length ? (
                <p className="empty-copy">用户批准决策后才能冻结第一版方案。</p>
              ) : null}
            </div>
          </section>

          <ViewShell
            snapshot={snapshot}
            view={view}
            moduleId={moduleId}
            artifactId={artifactId}
            decisionId={decisionId}
            solutionVersionId={solutionVersionId}
            onSetView={(v: ConsoleView) => setView(v)}
            onSelectModule={selectModule}
            onSelectArtifact={selectArtifact}
            onSelectDecision={selectDecision}
            onSelectSolutionVersion={selectSolutionVersion}
            onClearOverlay={clearOverlay}
            onRefresh={refresh}
          />
        </section>

        {/* Right board: readable work first, runtime audit on demand */}
        <aside className="audit-board">
          <div className="panel-heading">
            <div>
              <small>WORK ACTIVITY</small>
              <h2>工作动态</h2>
            </div>
            <Activity className="h-4 w-4" />
          </div>
          <div className="work-summary-stack">
            {snapshot.workspace?.work.toReversed().map((item) => (
              <article key={item.run_id}>
                <div>
                  <strong>{item.user_label}</strong>
                  <StatusTag status={item.state} />
                </div>
                <p>
                  {item.total_units
                    ? `${item.completed_units} / ${item.total_units} 个子任务已结束`
                    : "正在准备工作范围"}
                </p>
                {item.failed_units ? (
                  <small>{item.failed_units} 个子任务需要检查</small>
                ) : null}
              </article>
            ))}
            {!snapshot.workspace?.work.length ? (
              <p className="empty-copy">
                当前没有后台工作；需要启动或确认时会在这里说明。
              </p>
            ) : null}
          </div>

          <details
            className="advanced-audit"
            open={auditOpen}
            onToggle={(event) => setAuditOpen(event.currentTarget.open)}
          >
            <summary>
              <Database className="h-4 w-4" />
              查看 Agent 与事件审计
            </summary>
            <div className="advanced-audit-body">
              {snapshot.runtime.budget_accounts.toReversed().map((budget) => (
                <div
                  className="runtime-stack"
                  key={budget.id}
                >
                  <article>
                    <div>
                      <strong>Run budget</strong>
                      <StatusTag status={budget.status} />
                    </div>
                    <small>
                      tokens {budget.token_committed.toLocaleString()} /{" "}
                      {budget.token_cap.toLocaleString()}
                      {" · "}tools {budget.tool_calls_committed} /{" "}
                      {budget.tool_call_cap}
                    </small>
                  </article>
                </div>
              ))}
              <div className="runtime-stack">
                {snapshot.runtime.jobs.toReversed().map((job) => {
                  const error = snapshot.runtime.attempts.findLast(
                    (attempt) =>
                      attempt.job_id === job.id && attempt.normalized_error
                  )?.normalized_error;
                  return (
                    <article key={job.id}>
                      <div>
                        <Bot className="h-4 w-4" />
                        <strong>
                          {job.parent_job_id ? job.profile_id : "Coordinator"}
                        </strong>
                        <StatusTag status={job.status} />
                      </div>
                      <small>
                        gen {job.generation} · profile r{job.profile_revision} ·{" "}
                        {shortId(job.id)}
                      </small>
                      {error ? (
                        <code className="runtime-error">{error}</code>
                      ) : null}
                    </article>
                  );
                })}
              </div>

              <div className="panel-heading timeline-heading">
                <div>
                  <small>PROJECT CURSOR</small>
                  <h2>因果事件</h2>
                </div>
                <span>{events.length}</span>
              </div>
              <div className="event-timeline">
                {latestEvents.map((event) => (
                  <article key={event.id}>
                    <span>{String(event.sequence).padStart(3, "0")}</span>
                    <div>
                      <strong>{eventLabel(event.type)}</strong>
                      <code>{event.id}</code>
                      <time dateTime={event.created_at}>
                        {eventTime(event.created_at)}
                      </time>
                    </div>
                  </article>
                ))}
                {!latestEvents.length ? (
                  <p className="empty-copy">等待事件流连接…</p>
                ) : null}
              </div>
            </div>
          </details>
        </aside>
      </div>
    </main>
  );
}
