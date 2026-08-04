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
  FileCheck2,
  FlaskConical,
  GitBranch,
  RefreshCw,
  Search,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { useEventStream } from "@/app/hooks/useEventStream";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { getClient } from "@/lib/api";
import type {
  DecisionOption,
  DecisionRequest,
  ImpactAnalysis,
  Module,
  Project,
  ProjectSnapshot,
  SolutionProposal,
  SolutionVersion,
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

const DEFAULT_MODULES = JSON.stringify(
  [
    {
      key: "structure",
      name: "结构与接口",
      responsibility: "定义承载结构、空间边界和机械接口",
      dependency_keys: [],
      acceptance: ["关键尺寸可验证", "部件可拆换"],
      open_questions: ["可用材料和加工方式是什么？"],
    },
    {
      key: "power_control",
      name: "供能与控制",
      responsibility: "定义供能、控制链路与安全边界",
      dependency_keys: ["structure"],
      acceptance: ["功率预算闭合", "失效模式可测试"],
      open_questions: ["目标负载和续航是多少？"],
    },
  ],
  null,
  2,
);

interface ProjectConsoleProps {
  initialProject: Project;
  onBack: () => void;
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function normalizedDecisionOption(option: DecisionOption | string): DecisionOption {
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
  snapshot: SolutionVersion["module_snapshots"][number],
): string | null {
  return "snapshot_hash" in snapshot && typeof snapshot.snapshot_hash === "string"
    ? snapshot.snapshot_hash
    : null;
}

function moduleSnapshotIdentity(
  snapshot: SolutionVersion["module_snapshots"][number],
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

function stepLabel(step: SolutionVersion["implementation_steps"][number]): string {
  return "title" in step ? step.title : step.step;
}

function stepInstruction(step: SolutionVersion["implementation_steps"][number]): string {
  return "instruction" in step ? step.instruction : "";
}

function stepKey(
  step: SolutionVersion["implementation_steps"][number],
  index: number,
): string {
  return "step_id" in step ? step.step_id : `legacy-step-${index}-${step.step}`;
}

function eventLabel(type: string): string {
  return type.replaceAll(".", " / ").replaceAll("_", " ");
}

function statusTone(status: string): string {
  if (["succeeded", "approved", "supported", "compatible", "joined"].includes(status)) {
    return "tone-good";
  }
  if (["failed", "cancelled", "incompatible", "contradicted", "corrupt"].includes(status)) {
    return "tone-bad";
  }
  if (["running", "queued", "pending", "needs_test", "conditional"].includes(status)) {
    return "tone-live";
  }
  return "tone-muted";
}

function StatusTag({ status }: { status: string }) {
  return <span className={`status-tag ${statusTone(status)}`}>{status}</span>;
}

function StageRail({ project }: { project: Project }) {
  const activeIndex = STAGES.findIndex(([stage]) => stage === project.stage);
  return (
    <div className="stage-rail" aria-label={`当前阶段：${project.stage}`}>
      {STAGES.map(([stage, label], index) => (
        <div
          className={`stage-stop ${index < activeIndex ? "is-done" : ""} ${
            index === activeIndex ? "is-active" : ""
          }`}
          key={stage}
        >
          <span>{index < activeIndex ? <Check className="h-3 w-3" /> : index + 1}</span>
          <small>{label}</small>
        </div>
      ))}
    </div>
  );
}

function ModuleCard({ module }: { module: Module }) {
  return (
    <article className="module-card">
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

function RequirementsAction({
  snapshot,
  onDone,
}: {
  snapshot: ProjectSnapshot;
  onDone: () => Promise<unknown>;
}) {
  const [modulesText, setModulesText] = useState(DEFAULT_MODULES);
  const [busy, setBusy] = useState(false);

  const approve = async () => {
    setBusy(true);
    try {
      const modules = JSON.parse(modulesText) as Array<Record<string, unknown>>;
      if (!Array.isArray(modules) || modules.length < 1 || modules.length > 8) {
        throw new Error("模块必须是包含 1–8 项的 JSON 数组");
      }
      await getClient().approveRequirements(snapshot.project.id, snapshot.project.revision, {
        goal: snapshot.project.goal,
        hard_constraints: ["Agent 只提交建议，事实写入需要用户确认"],
        preferences: ["个人可维护，优先选择可购买和可验证的方案"],
        available_resources: ["Windows 工作站", "常见 DIY 工具"],
        unknowns: ["具体器件、接口和兼容性需要证据确认"],
        modules: modules as Parameters<
          ReturnType<typeof getClient>["approveRequirements"]
        >[2]["modules"],
      });
      toast.success("需求版本已批准");
      await onDone();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "需求格式无效");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="action-block">
      <div className="action-title">
        <FileCheck2 className="h-5 w-5" />
        <div>
          <small>01 / 冻结边界</small>
          <h2>批准第一版需求和模块</h2>
        </div>
      </div>
      <p>这里定义通用 DIY 模块，不把核心写成无人机专用。你可以直接修改 JSON。</p>
      <Textarea
        className="code-editor"
        rows={13}
        value={modulesText}
        onChange={(event) => setModulesText(event.target.value)}
      />
      <Button disabled={busy} onClick={() => void approve()}>
        {busy ? "正在批准…" : "批准需求版本"}
      </Button>
    </div>
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
  const activeRun = snapshot.runtime.jobs.find(
    (job) => !job.parent_job_id && ["queued", "running"].includes(job.status),
  );

  const start = async () => {
    setBusy(true);
    try {
      await getClient().startResearchRun(snapshot.project.id, snapshot.project.revision);
      toast.success("有界研究已进入 durable Job 队列");
      await onDone();
    } catch (error) {
      const message =
        error && typeof error === "object" && "error" in error
          ? (error as { error: { message: string } }).error.message
          : "无法启动研究";
      toast.error(message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="action-block research-action">
      <div className="action-title">
        <Search className="h-5 w-5" />
        <div>
          <small>02 / 证据化研究</small>
          <h2>{activeRun ? "研究正在执行" : "启动两路有界研究"}</h2>
        </div>
      </div>
      <p>
        PostgreSQL 持有 Job、Attempt、Delegation 与 JoinReceipt；Deep Agent 只生成 typed Proposal。
      </p>
      {activeRun ? (
        <div className="run-callout">
          <Activity className="h-4 w-4" />
          <span>Job {shortId(activeRun.id)}</span>
          <StatusTag status={activeRun.status} />
        </div>
      ) : (
        <Button disabled={busy} onClick={() => void start()}>
          <Bot className="h-4 w-4" />
          {busy ? "正在入队…" : "运行 Research Wave"}
        </Button>
      )}
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
  const options = decision.options.map(normalizedDecisionOption);
  const choose = async (optionId: string) => {
    setBusy(optionId);
    try {
      await getClient().resolveDecision(
        decision.id,
        snapshot.project.revision,
        optionId,
        decision.basis_hash,
      );
      toast.success("决策已写入 canonical state");
      await onDone();
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="action-block decision-action">
      <div className="action-title">
        <GitBranch className="h-5 w-5" />
        <div>
          <small>03 / 人工决策闸门</small>
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
              {option.option_id} · candidates {option.candidate_ids.length} · evidence{" "}
              {option.evidence_binding_ids.length}
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
      await getClient().freezeSolution(snapshot.project.id, snapshot.project.revision, {
        solution_proposal_id: proposal.id,
        basis_hash: proposal.basis_hash,
      });
      toast.success("不可变 SolutionVersion 已冻结");
      await onDone();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="action-block">
      <div className="action-title">
        <ShieldCheck className="h-5 w-5" />
        <div>
          <small>04 / 形成版本</small>
          <h2>把已批准决策冻结为方案</h2>
        </div>
      </div>
      <p>
        方案由服务端接纳的 <code>{proposal.profile_id}@{proposal.profile_revision}</code> Proposal
        生成；浏览器只能批准 proposal ID 和精确 basis，不能上传 BOM 或步骤。
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
        BOM {proposal.bom.length} 项 · 实施步骤 {proposal.implementation_steps.length} ·
        台架验证 {proposal.verification_steps.length} · unknown {proposal.unknowns.length}
      </p>
      <Button disabled={busy} onClick={() => void freeze()}>
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
    snapshot.modules[0] ? [snapshot.modules[0].id] : [],
  );
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await getClient().submitObservation(
        snapshot.project.id,
        snapshot.project.revision,
        statement,
        selected,
      );
      setStatement("");
      toast.success("现场观察已记录，durable impact-proposer 已入队");
      await onDone();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="action-block">
      <div className="action-title">
        <FlaskConical className="h-5 w-5" />
        <div>
          <small>05 / 真实反馈</small>
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
                    : current.filter((id) => id !== module.id),
                )
              }
            />
            {module.name}
          </label>
        ))}
      </div>
      <Button disabled={busy || !statement.trim() || selected.length === 0} onClick={() => void submit()}>
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
      await getClient().approveImpact(impact.id, snapshot.project.revision, impact.basis_hash);
      toast.success("PatchSet 已批准，新 SolutionVersion 已创建");
      await onDone();
    } finally {
      setBusy(false);
    }
  };
  const moduleById = new Map(snapshot.modules.map((module) => [module.id, module]));
  const moduleNames = (ids: string[]) =>
    ids.map((id) => moduleById.get(id)?.name ?? shortId(id)).join("、") || "无";
  return (
    <div className="action-block impact-action">
      <div className="action-title">
        <Wrench className="h-5 w-5" />
        <div>
          <small>06 / 局部修订</small>
          <h2>批准影响分析和模块补丁</h2>
        </div>
      </div>
      <p>{impact.summary}</p>
      <div className="impact-partition" aria-label="影响闭包">
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
            <strong>{moduleById.get(patch.module_id)?.name ?? shortId(patch.module_id)}</strong>
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
        Proposal：<code>{impact.profile_id}@{impact.profile_revision}</code>。浏览器只批准该 basis，
        不能编辑 PatchSet。
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

function NextAction({ snapshot, onDone }: { snapshot: ProjectSnapshot; onDone: () => Promise<unknown> }) {
  const pendingDecision = snapshot.decisions.find((item) => item.status === "pending");
  const approvedDecision = snapshot.decisions.find((item) => item.status === "approved");
  const proposedSolution = snapshot.solution_proposals.find((item) => item.status === "proposed");
  const proposedImpact = snapshot.impacts.find((item) => item.status === "proposed");
  const activeImpactJob = snapshot.runtime.jobs.find(
    (job) => job.kind === "impact_wave" && ["queued", "running"].includes(job.status),
  );
  if (!snapshot.project.active_requirement_revision_id) {
    return <RequirementsAction snapshot={snapshot} onDone={onDone} />;
  }
  if (!snapshot.decisions.length) {
    return <ResearchAction snapshot={snapshot} onDone={onDone} />;
  }
  if (pendingDecision) {
    return <DecisionAction snapshot={snapshot} decision={pendingDecision} onDone={onDone} />;
  }
  if (approvedDecision && !snapshot.project.active_solution_version_id && proposedSolution) {
    return <FreezeAction snapshot={snapshot} proposal={proposedSolution} onDone={onDone} />;
  }
  if (approvedDecision && !snapshot.project.active_solution_version_id) {
    return (
      <div className="action-block">
        <div className="action-title">
          <Bot className="h-5 w-5" />
          <div>
            <small>04 / 结构化方案</small>
            <h2>等待 durable solution-proposer</h2>
          </div>
        </div>
        <p>决策已经批准；服务端正在生成绑定候选、证据、BOM 和验证计划的 Proposal。</p>
      </div>
    );
  }
  if (proposedImpact) {
    return <ImpactAction snapshot={snapshot} impact={proposedImpact} onDone={onDone} />;
  }
  if (activeImpactJob) {
    return (
      <div className="action-block">
        <div className="action-title">
          <Bot className="h-5 w-5" />
          <div>
            <small>06 / 影响提案</small>
            <h2>等待 durable impact-proposer</h2>
          </div>
        </div>
        <p>Observation 已成为不可变事实；Agent 正在既定依赖闭包内生成 typed PatchSet 提案。</p>
        <div className="run-callout">
          <Activity className="h-4 w-4" />
          <span>Job {shortId(activeImpactJob.id)}</span>
          <StatusTag status={activeImpactJob.status} />
        </div>
      </div>
    );
  }
  return <ObservationAction snapshot={snapshot} onDone={onDone} />;
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
    (previous?.module_snapshots ?? []).map((snapshot) => [snapshot.module_id, snapshot]),
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
    0,
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
        <div className="reuse-rail" aria-label={`V${previous.version} 到 V${solution.version} 语义差异`}>
          {solution.module_snapshots.map((snapshot) => {
            const prior = previousSnapshots.get(snapshot.module_id);
            const changed = prior ? moduleSnapshotIdentity(prior) !== moduleSnapshotIdentity(snapshot) : true;
            const wasAffected = affected.has(snapshot.module_id);
            return (
              <div className={changed ? "is-changed" : "is-reused"} key={snapshot.module_id}>
                <i />
                <span>{moduleById.get(snapshot.module_id)?.name ?? shortId(snapshot.module_id)}</span>
                <small>{changed ? "changed" : wasAffected ? "affected · reused" : "reused"}</small>
                <code>{moduleSnapshotHash(snapshot)?.slice(0, 10) ?? "legacy"}</code>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="reuse-rail is-origin">
          {solution.module_snapshots.map((snapshot) => (
            <div key={snapshot.module_id}>
              <i />
              <span>{moduleById.get(snapshot.module_id)?.name ?? shortId(snapshot.module_id)}</span>
              <small>origin</small>
              <code>{moduleSnapshotHash(snapshot)?.slice(0, 10) ?? "legacy"}</code>
            </div>
          ))}
        </div>
      )}
      <div className="version-metrics">
        <span>BOM {solution.bom.length}{previous ? ` (${solution.bom.length - previous.bom.length >= 0 ? "+" : ""}${solution.bom.length - previous.bom.length})` : ""}</span>
        <span>实施 {solution.implementation_steps.length}</span>
        <span>验证 {solution.verification_steps.length}{previous ? ` (${solution.verification_steps.length - previous.verification_steps.length >= 0 ? "+" : ""}${solution.verification_steps.length - previous.verification_steps.length})` : ""}</span>
        {previous ? <span>变更模块 {changedCount}</span> : null}
      </div>
      <details>
        <summary>查看 BOM 与工程步骤</summary>
        <div className="version-detail-grid">
          <section>
            <small>BOM</small>
            {solution.bom.map((item, index) => (
              <p key={bomKey(item, index)}>{bomLabel(item)} × {item.quantity} {bomUnit(item)}</p>
            ))}
          </section>
          <section>
            <small>IMPLEMENT</small>
            {solution.implementation_steps.map((step, index) => (
              <p key={stepKey(step, index)}><strong>{stepLabel(step)}</strong>{stepInstruction(step) ? ` — ${stepInstruction(step)}` : ""}</p>
            ))}
          </section>
          <section>
            <small>VERIFY</small>
            {solution.verification_steps.map((step, index) => (
              <p key={stepKey(step, index)}><strong>{stepLabel(step)}</strong>{stepInstruction(step) ? ` — ${stepInstruction(step)}` : ""}</p>
            ))}
          </section>
        </div>
      </details>
    </article>
  );
}

export function ProjectConsole({ initialProject, onBack }: ProjectConsoleProps) {
  const { data, error, isLoading, mutate } = useSWR(
    ["project-snapshot", initialProject.id],
    () => getClient().getSnapshot(initialProject.id),
    { keepPreviousData: true },
  );
  const refresh = useCallback(async () => mutate(), [mutate]);
  const onRuntimeEvent = useCallback(() => {
    void mutate();
  }, [mutate]);
  const { connected, events } = useEventStream(initialProject.id, onRuntimeEvent);
  const snapshot = data;
  const project = snapshot?.project || initialProject;
  const latestEvents = useMemo(() => events.slice(-20).toReversed(), [events]);
  const moduleById = useMemo(
    () => new Map((snapshot?.modules ?? []).map((module) => [module.id, module])),
    [snapshot?.modules],
  );
  const solutionById = useMemo(
    () => new Map((snapshot?.solutions ?? []).map((solution) => [solution.id, solution])),
    [snapshot?.solutions],
  );
  const impactByBase = useMemo(
    () => new Map((snapshot?.impacts ?? []).map((impact) => [impact.base_solution_version_id, impact])),
    [snapshot?.impacts],
  );

  if (isLoading && !snapshot) {
    return <div className="console-loading">正在读取 canonical project snapshot…</div>;
  }
  if (error || !snapshot) {
    return (
      <div className="console-loading error-state">
        <CircleAlert className="h-6 w-6" />
        <p>无法读取项目：{error instanceof Error ? error.message : "unknown error"}</p>
        <Button onClick={() => void mutate()}>重试</Button>
      </div>
    );
  }

  return (
    <main className="console-shell">
      <header className="console-header">
        <div className="console-title-row">
          <Button variant="ghost" size="sm" onClick={onBack}>
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
          <Button variant="outline" size="sm" onClick={() => void refresh()}>
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
        </div>
        <StageRail project={project} />
      </header>

      <div className="console-body">
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
              snapshot.modules.map((module) => <ModuleCard key={module.id} module={module} />)
            ) : (
              <p className="empty-copy">批准需求后，这里会显示通用 DIY 模块。</p>
            )}
          </div>
        </aside>

        <section className="workbench">
          <div className="goal-strip">
            <span>GOAL</span>
            <p>{project.goal}</p>
          </div>
          <NextAction snapshot={snapshot} onDone={refresh} />

          <section className="evidence-board">
            <div className="panel-heading">
              <div>
                <small>EVIDENCE / OPTIONS</small>
                <h2>研究结果</h2>
              </div>
              <span>{snapshot.evidence.length}</span>
            </div>
            <div className="evidence-grid">
              {snapshot.candidates.map((candidate) => (
                <article className="candidate-card" key={candidate.id}>
                  <div>
                    <StatusTag status="candidate" />
                    <code>{shortId(candidate.module_id)}</code>
                  </div>
                  <h3>{candidate.name}</h3>
                  <p>{candidate.description}</p>
                  {candidate.risks.length ? <small>风险：{candidate.risks.join("；")}</small> : null}
                </article>
              ))}
              {snapshot.evidence.map((item) => (
                <article className="evidence-card" key={item.id}>
                  <div>
                    <StatusTag status={item.status} />
                    <a href={item.source_url} target="_blank" rel="noreferrer">
                      SOURCE
                    </a>
                  </div>
                  <p>{item.claim}</p>
                  <blockquote>{item.span_text}</blockquote>
                  <code>{item.snapshot_hash.slice(0, 16)}…</code>
                </article>
              ))}
              {!snapshot.evidence.length && !snapshot.candidates.length ? (
                <p className="empty-copy">研究完成后，候选方案和可追溯证据会出现在这里。</p>
              ) : null}
            </div>
          </section>

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
                  ? solutionById.get(solution.previous_version_id)
                  : undefined;
                return (
                  <SolutionVersionCard
                    key={solution.id}
                    solution={solution}
                    previous={previous}
                    impact={previous ? impactByBase.get(previous.id) : undefined}
                    moduleById={moduleById}
                    hasPatchSet={snapshot.patch_sets.some(
                      (patch) => patch.base_solution_version_id === solution.previous_version_id,
                    )}
                  />
                );
              })}
              {!snapshot.solutions.length ? (
                <p className="empty-copy">用户批准决策后才能冻结第一版方案。</p>
              ) : null}
            </div>
          </section>
        </section>

        <aside className="audit-board">
          <div className="panel-heading">
            <div>
              <small>DURABLE RUNTIME</small>
              <h2>Agent 编排</h2>
            </div>
            <Activity className="h-4 w-4" />
          </div>
          {snapshot.runtime.budget_accounts.toReversed().map((budget) => (
            <div className="runtime-stack" key={budget.id}>
              <article>
                <div>
                  <strong>Run budget</strong>
                  <StatusTag status={budget.status} />
                </div>
                <small>
                  tokens {budget.token_committed.toLocaleString()} / {budget.token_cap.toLocaleString()}
                  {" · "}tools {budget.tool_calls_committed} / {budget.tool_call_cap}
                </small>
              </article>
            </div>
          ))}
          <div className="runtime-stack">
            {snapshot.runtime.jobs.toReversed().map((job) => {
              const error = snapshot.runtime.attempts.findLast(
                (attempt) => attempt.job_id === job.id && attempt.normalized_error,
              )?.normalized_error;
              return (
                <article key={job.id}>
                  <div>
                    <Bot className="h-4 w-4" />
                    <strong>{job.parent_job_id ? job.profile_id : "Coordinator"}</strong>
                    <StatusTag status={job.status} />
                  </div>
                  <small>
                    gen {job.generation} · profile r{job.profile_revision} · {shortId(job.id)}
                  </small>
                  {error ? <code className="runtime-error">{error}</code> : null}
                </article>
              );
            })}
            {!snapshot.runtime.jobs.length ? (
              <p className="empty-copy">研究启动后显示 root Job 和最多两个 durable child Job。</p>
            ) : null}
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
                </div>
              </article>
            ))}
            {!latestEvents.length ? <p className="empty-copy">等待事件流连接…</p> : null}
          </div>
        </aside>
      </div>
    </main>
  );
}
