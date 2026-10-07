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
  Code2,
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
import { ActivityFeed } from "@/app/components/ActivityFeed";
import { RequirementsRevisionForm } from "@/app/components/RequirementsRevisionForm";
import { ConversationPanel } from "@/app/components/ConversationPanel";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { getClient } from "@/lib/api";
import type {
  ChangeImpactPreview,
  ConsoleView,
  DecisionOption,
  DecisionRequest,
  ExecutionPlanProposal,
  ImpactAnalysis,
  Module,
  Project,
  ProjectReshapeProposal,
  ProjectSnapshot,
  ProjectWorkspaceProjectionV1,
  RequirementsChangeProposal,
  SolutionProposal,
  SolutionVersion,
  SpendBudgetProposal,
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

/** 把后端返回的动作标题/说明翻译成玩家能懂的语言。 */
function humanizeActionTitle(kind: string, title: string): string {
  if (title) return title;
  const labels: Record<string, string> = {
    clarify_requirements: "先确认需求边界",
    discover_initial_modules: "让 AI 生成模块结构",
    start_research: "让 AI 查找资料",
    review_decision: "确认一项研究决策",
    review_solution: "确认方案",
    review_impact: "确认影响分析",
    record_observation: "记录现场观察",
    follow_work: "查看 AI 工作进展",
    inspect_failure: "AI 工作遇到了问题",
    review_requirements_change: "审核需求与结构变更",
    review_reshape: "确认结构草案",
    review_spend_budget: "审核预算提案",
    review_change_impact: "查看调整影响预览",
  };
  return labels[kind] ?? title;
}

/** 把后端的开发黑话错误说明，替换成用户可理解的提示。 */
function humanizeActionExplanation(
  kind: string,
  explanation: string
): string {
  if (kind === "inspect_failure") {
    return "AI 在查找资料时遇到问题，这次没有写入任何修改。你可以查看工作记录了解详情，或重新发起。";
  }
  return explanation;
}

const TOOL_CLASS_LABELS: Record<string, string> = {
  web_search: "网页搜索",
  github_read: "GitHub 读取",
  repository_read: "代码仓库读取",
  shopping: "购物比价",
  file_write: "文件写入",
};

const EFFECT_LABELS: Record<string, string> = {
  discovery: "只读发现",
  read: "只读",
  write: "写入",
  create: "创建",
  edit: "修改",
  delete: "删除",
};

const COORDINATION_MODE_LABELS: Record<string, string> = {
  decompose: "可分解并行",
  verify: "执行验证",
  replicate: "可复制",
  escalate: "可上报",
};

function humanizeList(
  items: string[],
  labels: Record<string, string>
): string {
  if (!items.length) return "无";
  return items.map((item) => labels[item] ?? item).join("、");
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
  stale: "已失效",
  up_to_date: "已同步",
  within: "预算内",
  over: "超预算",
  unknown: "未知",
  working: "处理中",
};

function displayError(error: unknown, fallback: string): string {
  const code =
    error && typeof error === "object" && "error" in error
      ? (error as { error?: { code?: unknown } }).error?.code
      : undefined;
  if (code === "MODEL_REQUEST_TIMED_OUT") {
    return "AI 规划请求超时，未写入任何项目修改。请重试，或缩小本次范围后再试。";
  }
  if (code === "MODEL_OUTPUT_INVALID") {
    return "AI 已返回，但结构草案未通过字段与依赖校验，未写入任何项目修改。请重试，或把模块边界重点写得更具体。";
  }
  if (code === "MODEL_CREDIT_EXHAUSTED") {
    return "模型服务额度不足，本次未写入任何项目修改。请充值，或切换到有可用额度的模型后重试。";
  }
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
  const affectedModuleNames = workspace.attention.affected_module_ids
    .map((moduleId) => workspace.modules.find((module) => module.id === moduleId)?.name)
    .filter((name): name is string => Boolean(name));
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
      {affectedModuleNames.length ? (
        <small>涉及模块：{affectedModuleNames.join("、")}</small>
      ) : null}
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
  const [maxConcurrency, setMaxConcurrency] = useState(3);
  // Leave ceilings undefined until the user sets them: the API freezes the
  // permissive defaults into the reviewable plan.
  const [maxTokenBudget, setMaxTokenBudget] = useState<number | undefined>(undefined);
  const [maxDurationSeconds, setMaxDurationSeconds] = useState<number | undefined>(undefined);
  const [researchDepth, setResearchDepth] = useState<"focused" | "standard" | "deep">("deep");
  const [sourceStrategy, setSourceStrategy] = useState<
    "auto" | "primary" | "independent" | "official" | "mixed"
  >("auto");
  const [requiresIndependentVerification, setRequiresIndependentVerification] = useState(true);
  const [objective, setObjective] = useState("");
  const [selectedModuleIds, setSelectedModuleIds] = useState<string[]>(() =>
    snapshot.modules.map((module) => module.id)
  );
  // A UUID is not a timeline.  A newly generated plan must become the plan
  // the user reviews next, even when an older approved plan remains in the
  // audit history.  The backend returns the same stable ordering; sorting
  // here makes the UI robust to a stale/cache-reordered Snapshot too.
  const latestResearchPlan = [...(snapshot.execution_plans ?? [])]
    .filter((plan) => plan.allowed_coordination_modes.includes("decompose"))
    .sort(
      (left, right) =>
        left.created_at.localeCompare(right.created_at) || left.id.localeCompare(right.id)
    )
    .at(-1);
  const approvedPlan =
    latestResearchPlan?.status === "approved" ? latestResearchPlan : undefined;
  const latestPlanNeedsReview = latestResearchPlan?.status === "proposed";
  const failedResearchRun = (snapshot.agent_runs ?? [])
    .filter((run) => run.kind === "research" && run.status === "failed")
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
        approvedPlan.id,
        failedResearchRun !== undefined
      );
      toast.success(
        failedResearchRun
          ? "已创建新的研究 Run，AI 将重新查找资料。"
          : "AI 已开始查找资料；完成后会把可选方案带回来给你看。"
      );
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "无法启动研究"));
    } finally {
      setBusy(false);
    }
  };
  const proposePlan = async () => {
    if (!selectedModuleIds.length) {
      toast.error("至少选择一个需要研究的模块。");
      return;
    }
    setBusy(true);
    try {
      await getClient().proposeResearchStrategyExecutionPlan(
        snapshot.project.id,
        snapshot.project.revision,
        {
          objective,
          moduleIds: selectedModuleIds,
          maxConcurrency,
          maxTokenBudget,
          maxDurationSeconds,
          requiresIndependentVerification,
          researchDepth,
          sourceStrategy: sourceStrategy === "auto" ? undefined : sourceStrategy,
        }
      );
      toast.success("AI 已生成可审查的研究策略，请确认范围、任务依赖和停止条件后再开始。");
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
          <h2>让 AI 帮你查找资料</h2>
        </div>
      </div>
      <p>
        AI
        会先告诉你它准备查什么、怎么查；你确认后才开始。找到的资料和推荐理由会留在项目里，方便以后回看。
      </p>
      {failedResearchRun?.latest_error ? (
        <p className="impact-meta">
          上一次研究停止原因：{failedResearchRun.latest_error}。你可以据此调整研究重点、模块范围、
          深度或 Token 预算，再生成新的待审核策略；不会复用旧 Run 的私有上下文。
        </p>
      ) : null}
      <Button
        disabled={busy || latestPlanNeedsReview}
        onClick={() => void (approvedPlan && !failedResearchRun ? start() : proposePlan())}
      >
        <Bot className="h-4 w-4" />
        {busy
          ? "正在准备…"
          : latestPlanNeedsReview
            ? "请先审核最新策略"
            : approvedPlan
            ? failedResearchRun
              ? "生成新的查找计划"
              : "开始查找资料"
            : "先查看查找计划"}
      </Button>
      <div className="research-plan-settings" aria-label="研究计划预算设置">
        <p>
          修改以下边界会生成一份新的待审核策略，不会自动启动研究，也不会改写当前项目事实。
          已批准的旧计划仍保留在审计记录中。
        </p>
          <label>
            研究重点（可选）
            <textarea
              value={objective}
              maxLength={2000}
              placeholder="例如：优先核对室内定位的可靠性与预算兼容性"
              onChange={(event) => setObjective(event.target.value)}
            />
          </label>
          <fieldset>
            <legend>本次研究范围</legend>
            {snapshot.modules.map((module) => (
              <label key={module.id}>
                <input
                  type="checkbox"
                  checked={selectedModuleIds.includes(module.id)}
                  onChange={(event) =>
                    setSelectedModuleIds((current) =>
                      event.target.checked
                        ? [...new Set([...current, module.id])]
                        : current.filter((item) => item !== module.id)
                    )
                  }
                />
                {module.name}
              </label>
            ))}
          </fieldset>
          <label>
            最大并发
            <input
              type="number"
              min={1}
              max={16}
              value={maxConcurrency}
              onChange={(event) => setMaxConcurrency(Number(event.target.value))}
            />
          </label>
          <label>
            Token 预算
            <input
              type="number"
              min={1}
              max={1_000_000_000}
              step={1000}
              value={maxTokenBudget ?? ""}
              onChange={(event) => {
                const next = event.target.value;
                setMaxTokenBudget(next === "" ? undefined : Number(next));
              }}
            />
            <small>留空时默认 200,000,000；实际用量按调用结算。</small>
          </label>
          <label>
            最长研究时长（秒）
            <input
              type="number"
              min={60}
              max={604_800}
              step={60}
              value={maxDurationSeconds ?? ""}
              onChange={(event) => {
                const next = event.target.value;
                setMaxDurationSeconds(next === "" ? undefined : Number(next));
              }}
            />
            <small>留空时默认 36,000 秒（10 小时）；从 Worker 首次开始执行后计时。</small>
          </label>
          <label>
            研究深度
            <select
              value={researchDepth}
              onChange={(event) => setResearchDepth(event.target.value as typeof researchDepth)}
            >
              <option value="focused">聚焦：2 个查询 / 2 份来源 / 最多 1 轮补题</option>
              <option value="standard">标准：3 个查询 / 3 份来源 / 最多 2 轮补题</option>
              <option value="deep">
                深度：按 Coverage 与研究视角持续扩展 / 关键结论至少 2 份不同来源文档 / 最长 10 小时
              </option>
            </select>
          </label>
          <label>
            来源策略
            <select
              value={sourceStrategy}
              onChange={(event) => setSourceStrategy(event.target.value as typeof sourceStrategy)}
            >
              <option value="auto">由 AI 根据任务选择（审核时展示）</option>
              <option value="official">优先官方资料</option>
              <option value="independent">优先独立资料</option>
              <option value="mixed">官方与独立来源混合</option>
              <option value="primary">优先一手资料</option>
            </select>
            <small>明确选择后会被冻结进策略，AI 不能在执行时自行替换。</small>
          </label>
          <label>
            <input
              type="checkbox"
              checked={requiresIndependentVerification}
              onChange={(event) => setRequiresIndependentVerification(event.target.checked)}
            />
            对关键结论要求独立核验（深度档默认开启；会增加研究时间与模型成本）
          </label>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => void proposePlan()}
        >
          {latestResearchPlan ? "按新边界生成策略草案" : "生成策略草案"}
        </Button>
      </div>
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

function ReviewExecutionPlanAction({
  snapshot,
  plan,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  plan: ExecutionPlanProposal;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState<"approved" | "rejected" | null>(null);
  const resolve = async (decision: "approved" | "rejected") => {
    setBusy(decision);
    try {
      const result = await getClient().resolveExecutionPlan(
        plan.id,
        snapshot.project.revision,
        decision,
        plan.scope_hash,
        decision === "approved" && plan.allowed_coordination_modes.includes("decompose")
      );
      toast.success(
        decision === "approved"
          ? result.data.agent_run
            ? "执行计划已批准，AI 已自动进入受限研究队列"
            : "执行计划已批准，AI 只能在这份边界内开始受限工作"
          : "执行计划已拒绝，不会触发任何后台任务"
      );
      await onDone();
    } catch (error) {
      toast.error(
        displayError(
          error,
          decision === "approved" ? "无法批准这份执行计划" : "无法拒绝这份执行计划"
        )
      );
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="action-block execution-plan-action">
      <div className="action-title">
        <ShieldCheck className="h-5 w-5" />
        <div>
          <small>批准执行计划</small>
          <h2>{plan.objective}</h2>
        </div>
      </div>
      <p>{plan.work_summary.length ? plan.work_summary.join("；") : "没有额外说明。"}</p>
      <p>
        研究深度：{plan.research_depth === "focused" ? "聚焦" : plan.research_depth === "deep" ? "深度" : "标准"}
        ；并发上限：{plan.max_concurrency}；Token 预算：{plan.max_token_budget}；最长时长：
        {plan.max_duration_seconds.toLocaleString()} 秒
        {plan.research_depth === "deep" ? "；关键结论至少需要两份不同来源文档" : ""}
        {plan.requires_independent_verification ? "；关键结论独立核验默认开启" : ""}
      </p>
      {plan.research_strategy ? (
        <section className="execution-plan-strategy" aria-label="AI 研究策略">
          <h3>AI 研究策略（批准后冻结）</h3>
          <p>{plan.research_strategy.summary}</p>
          {plan.research_strategy.decision_notes.length ? (
            <p>决策取舍：{plan.research_strategy.decision_notes.join("；")}</p>
          ) : null}
          <p>
            检索偏好（不替代证据准入）：{plan.research_strategy.source_strategy}；研究任务：
            {plan.research_strategy.tasks.length} 个
          </p>
          <ol>
            {plan.research_strategy.tasks.map((task) => (
              <li key={task.task_key}>
                <strong>{task.title}</strong>：{task.objective}
                <small>
                  依赖：{task.depends_on_task_keys.length ? task.depends_on_task_keys.join("、") : "无"}
                  ；预期输出：{task.expected_outputs.join("、")}
                  ；停止条件：{task.stop_conditions.join("；")}
                </small>
                {task.research_lenses?.length ? (
                  <small>研究视角：{task.research_lenses.join("；")}</small>
                ) : null}
              </li>
            ))}
          </ol>
          {plan.research_strategy.deferred_questions.length ? (
            <p>暂缓问题：{plan.research_strategy.deferred_questions.join("；")}</p>
          ) : null}
          {plan.research_strategy.risk_notes.length ? (
            <p>风险提示：{plan.research_strategy.risk_notes.join("；")}</p>
          ) : null}
        </section>
      ) : null}
      <div
        className="plan-boundary-grid"
        aria-label="执行计划边界"
      >
        <article>
          <small>协调模式</small>
          <strong>
            {humanizeList(plan.allowed_coordination_modes, COORDINATION_MODE_LABELS)}
          </strong>
        </article>
        <article>
          <small>最大并发</small>
          <strong>≤ {plan.max_concurrency}</strong>
        </article>
        <article>
          <small>Token 预算</small>
          <strong>≤ {plan.max_token_budget.toLocaleString()}</strong>
        </article>
        <article>
          <small>最长研究时长</small>
          <strong>≤ {plan.max_duration_seconds.toLocaleString()} 秒</strong>
        </article>
        <article>
          <small>允许工具</small>
          <strong>{humanizeList(plan.allowed_tool_classes, TOOL_CLASS_LABELS)}</strong>
        </article>
        <article>
          <small>允许副作用</small>
          <strong>{humanizeList(plan.allowed_effects, EFFECT_LABELS)}</strong>
        </article>
        <article>
          <small>结果需审批</small>
          <strong>{plan.requires_result_approval ? "需要" : "不需要"}</strong>
        </article>
      </div>
      <p className="impact-meta">
        批准后 AI 只会在该边界内分解与执行，任何结果仍由你决定是否采用；
        拒绝后不会触发任何后台任务。scope{" "}
        <code>{plan.scope_hash.slice(0, 16)}…</code>
      </p>
      {plan.status === "proposed" ? (
        <div className="proposal-actions">
          <Button
            disabled={busy !== null}
            onClick={() => void resolve("approved")}
          >
            {busy === "approved" ? "正在批准…" : "批准计划"}
          </Button>
          <Button
            disabled={busy !== null}
            variant="outline"
            onClick={() => void resolve("rejected")}
          >
            {busy === "rejected" ? "正在拒绝…" : "拒绝计划"}
          </Button>
        </div>
      ) : (
        <div className="run-callout">
          {plan.status === "approved"
            ? "已批准：AI 只能在该边界内执行"
            : "这份执行计划已处理，无需再次确认"}
        </div>
      )}
    </div>
  );
}

function actionSourceId(
  action: WorkspaceAction,
  type: string
): string | undefined {
  return action.source_refs.find((item) => item.type === type)?.id;
}

function ReviewRequirementsChangeAction({
  snapshot,
  proposal,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  proposal: RequirementsChangeProposal;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState<"applied" | "rejected" | null>(null);
  const resolve = async (decision: "applied" | "rejected") => {
    setBusy(decision);
    try {
      await getClient().resolveRequirementsChange(
        proposal.id,
        snapshot.project.revision,
        decision
      );
      toast.success(
        decision === "applied"
          ? "需求与模块结构已按提案重写，新的快照已生成"
          : "这份需求变更提案已拒绝，未改写任何项目事实"
      );
      await onDone();
    } catch (error) {
      toast.error(
        displayError(
          error,
          decision === "applied"
            ? "无法应用这份需求变更"
            : "无法拒绝这份需求变更"
        )
      );
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="action-block requirements-change-action">
      <div className="action-title">
        <ShieldCheck className="h-5 w-5" />
        <div>
          <small>审核需求变更</small>
          <h2>{proposal.summary}</h2>
        </div>
      </div>
      <p>{proposal.target_goal}</p>
      <div
        className="plan-boundary-grid"
        aria-label="需求变更要点"
      >
        <article>
          <small>硬约束</small>
          <strong>
            {proposal.hard_constraints.length
              ? proposal.hard_constraints.join("、")
              : "无"}
          </strong>
        </article>
        <article>
          <small>偏好</small>
          <strong>
            {proposal.preferences.length ? proposal.preferences.join("、") : "无"}
          </strong>
        </article>
        <article>
          <small>可用资源</small>
          <strong>
            {proposal.available_resources.length
              ? proposal.available_resources.join("、")
              : "无"}
          </strong>
        </article>
        <article>
          <small>未知项</small>
          <strong>
            {proposal.unknowns.length ? proposal.unknowns.join("、") : "无"}
          </strong>
        </article>
        <article>
          <small>新模块</small>
          <strong>
            {proposal.modules.map((item) => item.name).join("、") || "无"}
          </strong>
        </article>
      </div>
      <p className="impact-meta">
        应用会生成新的需求、模块与蓝图快照；拒绝不改写任何事实。
      </p>
      {proposal.status === "proposed" ? (
        <div className="proposal-actions">
          <Button
            disabled={busy !== null}
            onClick={() => void resolve("applied")}
          >
            {busy === "applied" ? "正在应用…" : "应用变更"}
          </Button>
          <Button
            disabled={busy !== null}
            variant="outline"
            onClick={() => void resolve("rejected")}
          >
            {busy === "rejected" ? "正在拒绝…" : "拒绝变更"}
          </Button>
        </div>
      ) : (
        <div className="run-callout">
          {proposal.status === "applied"
            ? "已应用：需求与模块结构已重写"
            : "这份需求变更提案已处理，无需再次确认"}
        </div>
      )}
    </div>
  );
}

function ReviewReshapeAction({
  snapshot,
  proposal,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  proposal: ProjectReshapeProposal;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState<"applied" | "rejected" | null>(null);
  const moduleById = new Map(
    snapshot.modules.map((module) => [module.id, module])
  );
  const moduleNames = (ids: string[]) =>
    ids.map((id) => moduleById.get(id)?.name ?? shortId(id)).join("、") || "无";
  const resolve = async (decision: "applied" | "rejected") => {
    setBusy(decision);
    try {
      await getClient().resolveProjectReshape(
        proposal.id,
        snapshot.project.revision,
        decision
      );
      toast.success(
        decision === "applied"
          ? "结构草案已应用，新模块骨架已生成"
          : "结构草案已拒绝，现有模块与蓝图保持不变"
      );
      await onDone();
    } catch (error) {
      toast.error(
        displayError(
          error,
          decision === "applied" ? "无法应用这份结构草案" : "无法拒绝这份结构草案"
        )
      );
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="action-block reshape-action">
      <div className="action-title">
        <GitBranch className="h-5 w-5" />
        <div>
          <small>确认结构草案</small>
          <h2>{proposal.summary}</h2>
        </div>
      </div>
      <p>{proposal.target_goal}</p>
      <div
        className="impact-partition"
        aria-label="结构草案分区"
      >
        <article>
          <small>调整</small>
          <strong>{moduleNames(proposal.affected_module_ids)}</strong>
        </article>
        <article className="is-reused">
          <small>保持不变</small>
          <strong>{moduleNames(proposal.unchanged_module_ids)}</strong>
        </article>
        <article>
          <small>新增</small>
          <strong>
            {proposal.new_modules.map((item) => item.name).join("、") || "无"}
          </strong>
        </article>
      </div>
      {proposal.status === "proposed" ? (
        <div className="proposal-actions">
          <Button
            disabled={busy !== null}
            onClick={() => void resolve("applied")}
          >
            {busy === "applied" ? "正在应用…" : "应用骨架"}
          </Button>
          <Button
            disabled={busy !== null}
            variant="outline"
            onClick={() => void resolve("rejected")}
          >
            {busy === "rejected" ? "正在拒绝…" : "拒绝草案"}
          </Button>
        </div>
      ) : (
        <div className="run-callout">
          {proposal.status === "applied"
            ? "已应用：新的模块骨架已生效"
            : "这份结构草案已处理，无需再次确认"}
        </div>
      )}
    </div>
  );
}

function DiscoverInitialModulesAction({
  snapshot,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false);
  const [planningBrief, setPlanningBrief] = useState("");
  const [structureDepth, setStructureDepth] = useState<"focused" | "standard" | "deep">(
    "deep"
  );
  const discover = async () => {
    setBusy(true);
    try {
      await getClient().discoverInitialModules(
        snapshot.project.id,
        snapshot.project.revision,
        {
          planning_brief: planningBrief || undefined,
          structure_depth: structureDepth,
        }
      );
      toast.success("模块结构草案已生成，请先审核并确认。");
      await onDone();
    } catch (error) {
      toast.error(displayError(error, "暂时无法生成模块结构草案"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="action-block reshape-action">
      <div className="action-title">
        <GitBranch className="h-5 w-5" />
        <div>
          <small>下一步：生成结构草案</small>
          <h2>让 AI 提出模块边界</h2>
        </div>
      </div>
      <p>
        这会进行一次受限模型调用，输出只是一份待你确认的模块结构草案；不会创建研究任务或修改项目模块。
      </p>
      <label>
        结构规划重点（可选）
        <textarea
          value={planningBrief}
          maxLength={2000}
          placeholder="例如：优先区分飞行安全、室内定位与通信接口的责任边界"
          onChange={(event) => setPlanningBrief(event.target.value)}
        />
      </label>
      <label>
        结构思考深度
        <select
          value={structureDepth}
          onChange={(event) => setStructureDepth(event.target.value as typeof structureDepth)}
        >
          <option value="focused">聚焦：最少必要边界</option>
          <option value="standard">标准：按接口和验收条件分层</option>
          <option value="deep">深度：额外审视耦合、失效隔离与依赖方向</option>
        </select>
      </label>
      <Button disabled={busy} onClick={() => void discover()}>
        {busy ? "正在生成结构草案…" : "让 AI 生成模块结构"}
      </Button>
    </div>
  );
}

function ReviewSpendBudgetAction({
  snapshot,
  proposal,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  proposal: SpendBudgetProposal;
  onDone: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState<"applied" | "rejected" | null>(null);
  const impactPreview = (snapshot.spend_budget_impact_previews ?? []).find(
    (item) => item.proposal_id === proposal.id
  );
  const activeBudget = snapshot.spend_budget;
  const resolve = async (decision: "applied" | "rejected") => {
    setBusy(decision);
    try {
      await getClient().resolveSpendBudget(
        proposal.id,
        snapshot.project.revision,
        decision
      );
      toast.success(
        decision === "applied"
          ? "预算已生效，成为项目新的支出上限"
          : "预算提案已拒绝，未改写项目预算"
      );
      await onDone();
    } catch (error) {
      toast.error(
        displayError(
          error,
          decision === "applied" ? "无法批准这份预算提案" : "无法拒绝这份预算提案"
        )
      );
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="action-block spend-budget-action">
      <div className="action-title">
        <Database className="h-5 w-5" />
        <div>
          <small>审核预算提案</small>
          <h2>{proposal.summary}</h2>
        </div>
      </div>
      <div
        className="plan-boundary-grid"
        aria-label="预算对比"
      >
        <article>
          <small>提案金额</small>
          <strong>
            {proposal.currency} {proposal.amount}
          </strong>
        </article>
        <article>
          <small>当前预算</small>
          <strong>
            {activeBudget
              ? `${activeBudget.currency} ${activeBudget.amount}`
              : "未设置"}
          </strong>
        </article>
      </div>
      {impactPreview && impactPreview.lines.length ? (
        <div className="impact-patches">
          {impactPreview.lines.map((line) => (
            <article key={line.line_ref}>
              <div>
                <StatusTag status={line.classification} />
                <code>{line.line_ref}</code>
              </div>
              <strong>
                {line.amount} {line.currency}
              </strong>
              <small>{line.reason}</small>
            </article>
          ))}
        </div>
      ) : null}
      <p className="impact-meta">
        批准会创建新的 SpendBudgetRevision 并取代旧上限；拒绝不改写任何项目事实。
      </p>
      {proposal.status === "proposed" ? (
        <div className="proposal-actions">
          <Button
            disabled={busy !== null}
            onClick={() => void resolve("applied")}
          >
            {busy === "applied" ? "正在批准…" : "批准预算"}
          </Button>
          <Button
            disabled={busy !== null}
            variant="outline"
            onClick={() => void resolve("rejected")}
          >
            {busy === "rejected" ? "正在拒绝…" : "拒绝预算"}
          </Button>
        </div>
      ) : (
        <div className="run-callout">
          {proposal.status === "applied"
            ? "已批准：新预算已生效"
            : "这份预算提案已处理，无需再次确认"}
        </div>
      )}
    </div>
  );
}

function ReviewChangeImpactAction({
  snapshot,
  preview,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  preview: ChangeImpactPreview;
  onDone: () => Promise<unknown>;
}) {
  const moduleById = new Map(
    snapshot.modules.map((module) => [module.id, module])
  );
  const moduleNames = (ids: string[]) =>
    ids.map((id) => moduleById.get(id)?.name ?? shortId(id)).join("、") || "无";
  const lockById = new Map(
    (snapshot.selection_locks ?? []).map((lock) => [lock.id, lock])
  );
  const lockNames = (ids: string[]) =>
    ids.map((id) => lockById.get(id)?.reason ?? shortId(id)).join("、") || "无";
  return (
    <div className="action-block change-impact-action">
      <div className="action-title">
        <Wrench className="h-5 w-5" />
        <div>
          <small>只读影响预览</small>
          <h2>你的调整会影响到哪些部分</h2>
        </div>
      </div>
      <p>{preview.summary}</p>
      <div
        className="impact-partition"
        aria-label="影响闭包"
      >
        <article>
          <small>DIRECT</small>
          <strong>{moduleNames(preview.direct_affected_module_ids)}</strong>
        </article>
        <article>
          <small>TRANSITIVE</small>
          <strong>
            {moduleNames(preview.transitive_affected_module_ids)}
          </strong>
        </article>
        <article className="is-reused">
          <small>UNCHANGED</small>
          <strong>{moduleNames(preview.unaffected_module_ids)}</strong>
        </article>
      </div>
      {preview.affected_selection_lock_ids.length ? (
        <p className="impact-meta">
          受影响选型锁：{lockNames(preview.affected_selection_lock_ids)}
        </p>
      ) : null}
      {preview.invalidated_refs.length ? (
        <div className="impact-patches">
          {preview.invalidated_refs.map((ref) => (
            <article key={ref.entity_id}>
              <div>
                <StatusTag status="stale" />
                <code>{ref.kind}</code>
              </div>
              <strong>{ref.summary}</strong>
              <small>{ref.reason}</small>
            </article>
          ))}
        </div>
      ) : null}
      <p className="impact-meta">
        这是只读预览，不会自动改写模块、选型锁或研究结果；后续深挖重研究仍由
        已批准的执行计划把关。
      </p>
      <Button
        variant="outline"
        onClick={() => void onDone()}
      >
        刷新项目状态
      </Button>
    </div>
  );
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
          <small>AI 需要你处理</small>
          <h2>{action ? humanizeActionTitle(action.kind, action.title) : "等待项目状态同步"}</h2>
        </div>
      </div>
      <p>
        {action
          ? humanizeActionExplanation(action.kind, action.explanation)
          : "当前没有可安全执行的下一步，请刷新项目状态。"}
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

function ClarifyHint({ onOpenRequirementsForm }: { onOpenRequirementsForm: () => void }) {
  return (
    <div className="action-block">
      <div className="action-title">
        <Bot className="h-5 w-5" />
        <div>
          <small>下一步</small>
          <h2>确认项目需求</h2>
        </div>
      </div>
      <p>
        你可以直接填写并确认需求；也可以先和 AI 对话，让它整理出草案后再确认。
      </p>
      <Button variant="outline" onClick={onOpenRequirementsForm}>
        填写需求单
      </Button>
    </div>
  );
}

function NextAction({
  snapshot,
  onDone,
  onOpenAudit,
  requirementsDraft,
  onDismissRequirementsDraft,
  manualRequirements,
  onOpenRequirementsForm,
}: {
  snapshot: ProjectSnapshot;
  onDone: () => Promise<unknown>;
  onOpenAudit: () => void;
  requirementsDraft?: Record<string, unknown> | null;
  onDismissRequirementsDraft?: () => void;
  manualRequirements?: boolean;
  onOpenRequirementsForm: () => void;
}) {
  const action = snapshot.workspace?.next_actions[0];
  const hasPendingClarifications =
    (snapshot.conversation?.open_clarifications?.length ?? 0) > 0;
  // 需求表单只在“需求尚未确认”时渲染；且要么用户已明确选择手动填写，
  // 要么存在来自对话页的首次需求草案且没有待答澄清。其余情况一律不预填需求。
  const showRequirementsForm =
    !snapshot.project.active_requirement_revision_id &&
    (Boolean(manualRequirements) ||
      (Boolean(requirementsDraft) && !hasPendingClarifications));
  if (showRequirementsForm) {
    return (
      <RequirementsRevisionForm
        snapshot={snapshot}
        onDone={onDone}
        draft={requirementsDraft}
        onDismissDraft={onDismissRequirementsDraft}
      />
    );
  }
  if (!action) return <ActionNotice onOpenAudit={onOpenAudit} />;

  switch (action.kind) {
    case "clarify_requirements":
      return <ClarifyHint onOpenRequirementsForm={onOpenRequirementsForm} />;
    case "discover_initial_modules":
      return <DiscoverInitialModulesAction snapshot={snapshot} onDone={onDone} />;
    case "start_research":
      return (
        <ResearchAction
          snapshot={snapshot}
          onDone={onDone}
        />
      );
    case "inspect_failure": {
      // 失败的 research run 已经具备 successor/retry 入口时，复用研究操作面板。
      // 只有没有可重试的 research run 时，才退回审计提示。
      const hasFailedResearchRun = (snapshot.agent_runs ?? []).some(
        (run) => run.kind === "research" && run.status === "failed"
      );
      return hasFailedResearchRun ? (
        <ResearchAction
          snapshot={snapshot}
          onDone={onDone}
        />
      ) : (
        <ActionNotice
          action={action}
          onOpenAudit={onOpenAudit}
        />
      );
    }
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
    case "review_execution_plan": {
      const planId = actionSourceId(action, "execution_plan");
      const plan = snapshot.execution_plans?.find(
        (item) => item.id === planId
      );
      return plan ? (
        <ReviewExecutionPlanAction
          snapshot={snapshot}
          plan={plan}
          onDone={onDone}
        />
      ) : (
        <ActionNotice
          action={action}
          onOpenAudit={onOpenAudit}
        />
      );
    }
    case "review_requirements_change": {
      const proposalId = actionSourceId(action, "requirements_change");
      const proposal = (snapshot.requirements_change_proposals ?? []).find(
        (item) => item.id === proposalId
      );
      return proposal ? (
        <ReviewRequirementsChangeAction
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
    case "review_reshape": {
      const proposalId = actionSourceId(action, "reshape_proposal");
      const proposal = (snapshot.reshape_proposals ?? []).find(
        (item) => item.id === proposalId
      );
      return proposal ? (
        <ReviewReshapeAction
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
    case "review_spend_budget": {
      const proposalId = actionSourceId(action, "spend_budget_proposal");
      const proposal = (snapshot.spend_budget_proposals ?? []).find(
        (item) => item.id === proposalId
      );
      return proposal ? (
        <ReviewSpendBudgetAction
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
    case "review_change_impact": {
      const previewId = actionSourceId(action, "change_impact_preview");
      const preview = (snapshot.change_impact_previews ?? []).find(
        (item) => item.id === previewId
      );
      return preview ? (
        <ReviewChangeImpactAction
          snapshot={snapshot}
          preview={preview}
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
  const [developerMode, setDeveloperMode] = useState(false);
  const [consolePage, setConsolePage] = useState<"activity" | "workbench">(
    "activity"
  );
  const [requirementsDraft, setRequirementsDraft] = useState<Record<string, unknown> | null>(null);
  // 用户显式选择“跳过 AI，手动填写”时置为 true：绕过澄清门禁直接展示需求表单。
  const [manualRequirements, setManualRequirements] = useState(false);
  const { data, error, isLoading, mutate } = useSWR(
    ["project-snapshot", initialProject.id],
    () => getClient().getSnapshot(initialProject.id),
    { keepPreviousData: true }
  );
  const refresh = useCallback(async () => mutate(), [mutate]);
  const onRuntimeEvent = useCallback(() => {
    void mutate();
  }, [mutate]);

  /** 各类行动完成后的通用收尾：刷新快照并收起需求表单状态。 */
  const handleActionDone = useCallback(async () => {
    await refresh();
    setRequirementsDraft(null);
    setManualRequirements(false);
  }, [refresh]);
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
            <small>正在制作</small>
            <h1>{project.name}</h1>
          </div>
          {developerMode ? (
            <div className="truth-badge">
              <Database className="h-4 w-4" />
              <span>REV {String(project.revision).padStart(2, "0")}</span>
              <i className={connected ? "is-online" : ""} />
              {connected ? "LIVE" : "RECONNECTING"}
            </div>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            onClick={() => void refresh()}
          >
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
          <Button
            aria-pressed={developerMode}
            className="developer-mode-toggle"
            onClick={() => setDeveloperMode((current) => !current)}
            size="sm"
            variant="ghost"
          >
            <Code2 className="h-4 w-4" />
            {developerMode ? "退出开发者模式" : "开发者模式"}
          </Button>
        </div>
        {developerMode ? <StageRail project={project} /> : null}
      </header>

      <div
        className={`console-body ${
          developerMode ? "is-developer" : "is-player"
        }`}
      >
        {/* 作品与模块的日常操作集中在像素工程台；传统模块列表只在开发者模式保留。 */}
        {developerMode ? (
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
        ) : null}

        {/* Center: 对话页（默认） + 工作台页（双模式） */}
        <section className="workbench">
          {developerMode ? (
            <ProjectPulse workspace={snapshot.workspace} />
          ) : null}
          <div className="goal-strip">
            <span>GOAL</span>
            <p>{project.goal}</p>
          </div>

          <div
            className="console-page-switch"
            role="group"
            aria-label="项目页面"
          >
            <button
              className={consolePage === "activity" ? "is-active" : ""}
              aria-pressed={consolePage === "activity"}
              onClick={() => setConsolePage("activity")}
              type="button"
            >
              与 AI 对话
            </button>
            <button
              className={consolePage === "workbench" ? "is-active" : ""}
              aria-pressed={consolePage === "workbench"}
              onClick={() => setConsolePage("workbench")}
              type="button"
            >
              工程工作台
            </button>
          </div>

          {consolePage === "activity" ? (
            <>
              <ConversationPanel
                projectId={project.id}
                projectRevision={project.revision}
                hasApprovedRequirements={Boolean(project.active_requirement_revision_id)}
                onSnapshotChanged={refresh}
                onUseRequirementsDraft={(draft) => {
                  setRequirementsDraft(draft);
                  setManualRequirements(false);
                  setConsolePage("workbench");
                }}
                onOpenRequirementsForm={() => {
                  setRequirementsDraft(null);
                  setManualRequirements(true);
                  setConsolePage("workbench");
                }}
              />
              <ActivityFeed
                connected={connected}
                developerMode={developerMode}
                onOpenAudit={() => {
                  setDeveloperMode(true);
                  setAuditOpen(true);
                }}
                onSelectModule={(selectedId) => {
                  selectModule(selectedId);
                  setConsolePage("workbench");
                }}
                pendingAction={
                  <NextAction
                    snapshot={snapshot}
                    onDone={handleActionDone}
                    requirementsDraft={requirementsDraft}
                    manualRequirements={manualRequirements}
                    onDismissRequirementsDraft={() => {
                      setRequirementsDraft(null);
                      setManualRequirements(true);
                    }}
                    onOpenRequirementsForm={() => {
                      setRequirementsDraft(null);
                      setManualRequirements(true);
                      setConsolePage("workbench");
                    }}
                    onOpenAudit={() => {
                      setDeveloperMode(true);
                      setAuditOpen(true);
                    }}
                  />
                }
                snapshot={snapshot}
              />
            </>
          ) : (
            <>
              <DraftWorkbench
                developerMode={developerMode}
                onOpenModuleIdChange={(selectedId) =>
                  void selectModule(selectedId ?? "")
                }
                openModuleId={moduleId}
                snapshot={snapshot}
                onRefresh={refresh}
              />
              <NextAction
                snapshot={snapshot}
                onDone={handleActionDone}
                requirementsDraft={requirementsDraft}
                manualRequirements={manualRequirements}
                onDismissRequirementsDraft={() => {
                  setRequirementsDraft(null);
                  setManualRequirements(true);
                }}
                onOpenRequirementsForm={() => {
                  setRequirementsDraft(null);
                  setManualRequirements(true);
                }}
                onOpenAudit={() => {
                  setDeveloperMode(true);
                  setAuditOpen(true);
                }}
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
            developerMode={developerMode}
          />
            </>
          )}
        </section>

        {/* 运行预算、Agent 及事件流属于工程审计，不占普通用户的工作台。 */}
        {developerMode ? (
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
                      ? `${item.completed_units} / ${item.total_units} 个子任务证据完成`
                      : "正在准备工作范围"}
                  </p>
                  {item.partial_units ? (
                    <small>{item.partial_units} 个子任务仅部分覆盖，仍待补题或核验</small>
                  ) : null}
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
                {(snapshot.agent_run_budget_accounts ?? []).toReversed().map((budget) => (
                  <div
                    className="runtime-stack"
                    key={budget.id}
                  >
                    <article>
                      <div>
                        <strong>Run budget</strong>
                        <StatusTag status={budget.state} />
                      </div>
                      <small>
                        tokens {budget.token_consumed.toLocaleString()} used +{" "}
                        {budget.token_reserved.toLocaleString()} reserved /{" "}
                        {budget.token_cap.toLocaleString()}
                        {" · "}tools {budget.tool_calls_consumed} used +{" "}
                        {budget.tool_calls_reserved} reserved /{" "}
                        {budget.tool_call_cap}
                      </small>
                    </article>
                  </div>
                ))}
                <div className="runtime-stack">
                  {(snapshot.agent_runs ?? []).toReversed().map((run) => {
                    return (
                      <article key={run.id}>
                        <div>
                          <Bot className="h-4 w-4" />
                          <strong>
                            {run.kind} AgentRun
                          </strong>
                          <StatusTag status={run.status} />
                        </div>
                        <small>
                          basis r{run.basis_project_revision} · {shortId(run.id)}
                        </small>
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
        ) : null}
      </div>
    </main>
  );
}
