"use client";

import { useMemo, type ReactNode } from "react";
import {
  Bot,
  CircleAlert,
  FileCheck2,
  GitBranch,
  History,
  PackageSearch,
  Search,
  ShieldCheck,
  SquareStack,
  Wrench,
} from "lucide-react";
import type { ProjectSnapshot } from "@/app/types/types";

const ATTENTION_LABELS: Record<string, string> = {
  up_to_date: "已同步",
  working: "处理中",
  needs_input: "需要补充",
  ready_to_review: "等你确认",
  recoverable_failure: "需要检查",
  blocked: "暂时受阻",
  complete: "已完成",
};

const ATTENTION_TONE: Record<string, string> = {
  up_to_date: "tone-good",
  working: "tone-live",
  needs_input: "tone-live",
  ready_to_review: "tone-live",
  recoverable_failure: "tone-bad",
  blocked: "tone-bad",
  complete: "tone-good",
};

const KIND_LABELS: Record<string, string> = {
  clarify_requirements: "确认需求边界",
  start_research: "开始查找资料",
  review_decision: "确认研究决策",
  review_solution: "确认方案",
  review_impact: "确认影响分析",
  record_observation: "记录现场观察",
  follow_work: "查看 AI 工作进展",
  inspect_failure: "AI 工作遇到问题",
};

function humanizeWarning(warning: string): string | null {
  const labels: Record<string, string> = {
    runtime_module_relation_unavailable:
      "模块关系图暂时不可用，稍后可重试。",
    runtime_budget_exhausted: "本轮 AI 预算已用完，可以稍后再试。",
    runtime_worker_offline: "AI 工作进程离线，暂时无法运行研究任务。",
  };
  return labels[warning] ?? null;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

interface ActivityFeedProps {
  snapshot: ProjectSnapshot;
  developerMode?: boolean;
  pendingAction: ReactNode;
  onOpenAudit: () => void;
  /** 点击需求单里的模块时回调（应切换到工作台并打开该模块）。 */
  onSelectModule?: (moduleId: string) => void;
  /** SSE 事件流是否在线。 */
  connected?: boolean;
}

/**
 * 对话式活动流：把 AI 的工作状态、当前待你确认的动作、以及已经发生过的
 * 需求 / 执行计划 / 研究决策 / 方案冻结 / 现场观察串成一条“AI↔你”时间线。
 *
 * 它是玩家模式的默认落地页；工作台（DraftWorkbench）通过顶部切换访问。
 * 不发起额外请求，全部数据来自同一个 ProjectSnapshot。
 */
export function ActivityFeed({
  snapshot,
  developerMode = false,
  pendingAction,
  onOpenAudit,
  onSelectModule,
  connected = true,
}: ActivityFeedProps) {
  const workspace = snapshot.workspace;
  const action = workspace?.next_actions[0];

  const requirement = snapshot.requirements.find(
    (item) => item.id === snapshot.project.active_requirement_revision_id
  );

  const activeModules = useMemo(() => {
    const reqId = snapshot.project.active_requirement_revision_id;
    return reqId
      ? snapshot.modules.filter(
          (module) => module.requirement_revision_id === reqId
        )
      : snapshot.modules;
  }, [snapshot]);

  const latestWork = useMemo(
    () => [...(workspace?.work ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    [workspace]
  );

  const attention = workspace?.attention;
  const attentionLabel = attention
    ? ATTENTION_LABELS[attention.state] ?? attention.state
    : "待连接";
  const attentionTone = attention
    ? ATTENTION_TONE[attention.state] ?? "tone-muted"
    : "tone-muted";

  const recentDecisions = useMemo(
    () =>
      [...snapshot.decisions]
        .sort((a, b) =>
          (b.resolved_at ?? b.id).localeCompare(a.resolved_at ?? a.id)
        )
        .slice(0, developerMode ? 5 : 3),
    [snapshot.decisions, developerMode]
  );

  const recentSolutions = useMemo(
    () =>
      [...snapshot.solutions]
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 3),
    [snapshot.solutions]
  );

  const recentImpacts = useMemo(
    () =>
      [...snapshot.impacts]
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 3),
    [snapshot.impacts]
  );

  const recentObservations = useMemo(
    () =>
      [...snapshot.observations]
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 3),
    [snapshot.observations]
  );

  const plans = useMemo(
    () => [...(snapshot.execution_plans ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [snapshot.execution_plans]
  );

  const planById = useMemo(
    () => new Map((snapshot.execution_plans ?? []).map((plan) => [plan.id, plan])),
    [snapshot.execution_plans]
  );

  return (
    <div className="activity-feed">
      {/* AI 状态条 */}
      <section className="activity-status-strip" aria-label="AI 当前状态">
        <div className="activity-status-avatar" aria-hidden="true">
          <Bot className="h-5 w-5" />
        </div>
        <div className="activity-status-copy">
          <small>AI 状态</small>
          <strong className={`activity-status-state ${attentionTone}`}>
            {attentionLabel}
          </strong>
          <p>{attention?.reason || attention?.title || ""}</p>
        </div>
        <span
          className={`activity-conn ${connected ? "is-live" : "is-offline"}`}
          title={connected ? "实时连接中" : "实时连接断开，正在重连"}
        >
          {connected ? "实时更新中" : "重新连接中…"}
        </span>
        {workspace?.warnings?.length ? (
          (() => {
            const message = humanizeWarning(workspace.warnings[0]);
            return message ? (
              <div className="activity-status-warning">
                <CircleAlert className="h-4 w-4" />
                <span>{message}</span>
              </div>
            ) : null;
          })()
        ) : null}
      </section>

      {/* 当前待办：AI 在等你确认什么 */}
      {action ? (
        <section className="activity-current" aria-label="需要你处理">
          <div className="activity-turn-label">
            <span className="activity-turn-dot" />
            <small>{KIND_LABELS[action.kind] ?? "需要你处理"}</small>
          </div>
          <div className="activity-current-card">{pendingAction}</div>
        </section>
      ) : null}

      {/* 工作状态：AI 正在做什么 */}
      {latestWork.length ? (
        <section className="activity-group" aria-label="AI 工作进度">
          <div className="activity-group-head">
            <Search className="h-4 w-4" aria-hidden="true" />
            <strong>AI 正在做什么</strong>
          </div>
          <div className="activity-work-list">
            {latestWork.map((work) => {
              const plan = work.run_id
                ? (planById.get(work.run_id) ?? planById.get(work.kind))
                : undefined;
              const label =
                plan?.objective ??
                (work.kind === "research_wave"
                  ? "分模块查找资料"
                  : work.kind === "solution_generation"
                  ? "整理方案"
                  : work.kind === "impact_analysis"
                  ? "分析影响"
                  : work.user_label || work.kind);
              const progress =
                work.total_units > 0
                  ? Math.round((work.completed_units / work.total_units) * 100)
                  : work.state === "succeeded"
                  ? 100
                  : work.state === "failed"
                  ? 0
                  : null;
              return (
                <article className="activity-work-item" key={work.run_id}>
                  <div className="activity-work-main">
                    <strong>{label}</strong>
                    <span>
                      {work.total_units > 0
                        ? `${work.completed_units}/${work.total_units} 完成`
                        : work.state === "succeeded"
                        ? "已完成"
                        : work.state === "failed"
                        ? "需要检查"
                        : "进行中"}
                    </span>
                  </div>
                  {progress !== null ? (
                    <div
                      className="activity-progress"
                      aria-label={`进度 ${progress}%`}
                    >
                      <i
                        className={progress === 100 ? "is-done" : ""}
                        style={{ width: `${Math.max(progress, 4)}%` }}
                      />
                    </div>
                  ) : null}
                  {work.latest_error && developerMode ? (
                    <p className="activity-work-error">
                      {work.latest_error}
                    </p>
                  ) : null}
                </article>
              );
            })}
          </div>
        </section>
      ) : null}

      {/* 需求单 */}
      <section className="activity-group" aria-label="需求单">
        <div className="activity-group-head">
          <FileCheck2 className="h-4 w-4" aria-hidden="true" />
          <strong>需求单</strong>
        </div>
        {requirement ? (
          <article className="activity-sheet">
            <header>
              <strong>{requirement.goal}</strong>
              <span>需求 R{requirement.revision}</span>
            </header>
            {requirement.hard_constraints.length ? (
              <dl>
                <dt>硬约束</dt>
                <dd>{requirement.hard_constraints.join("；")}</dd>
              </dl>
            ) : null}
            {requirement.preferences.length ? (
              <dl>
                <dt>偏好</dt>
                <dd>{requirement.preferences.join("；")}</dd>
              </dl>
            ) : null}
            {requirement.unknowns.length ? (
              <dl>
                <dt>待确认</dt>
                <dd>{requirement.unknowns.join("；")}</dd>
              </dl>
            ) : null}
            {activeModules.length ? (
              <div className="activity-sheet-modules">
                {activeModules.map((module) => (
                  <button
                    className="activity-sheet-module"
                    key={module.id}
                    onClick={() => onSelectModule?.(module.id)}
                    type="button"
                  >
                    <code>{module.key}</code>
                    {module.name}
                  </button>
                ))}
              </div>
            ) : null}
          </article>
        ) : (
          <p className="activity-empty">
            还没有批准的需求。告诉 AI 你的想法，它会先帮你把边界列出来。
          </p>
        )}
      </section>

      {/* 执行计划 */}
      {plans.length ? (
        <section className="activity-group" aria-label="AI 计划">
          <div className="activity-group-head">
            <GitBranch className="h-4 w-4" aria-hidden="true" />
            <strong>AI 的工作边界</strong>
          </div>
          <div className="activity-plan-list">
            {plans.map((plan) => (
              <article className="activity-plan-item" key={plan.id}>
                <div>
                  <strong>{plan.objective}</strong>
                  <span>
                    {plan.work_summary.join(" · ")}
                  </span>
                </div>
                <small
                  className={
                    plan.status === "approved"
                      ? "tone-good"
                      : plan.status === "rejected"
                      ? "tone-bad"
                      : "tone-live"
                  }
                >
                  {plan.status === "approved"
                    ? "已批准范围"
                    : plan.status === "rejected"
                    ? "已拒绝"
                    : "待你确认"}
                </small>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {/* 研究决策时间线 */}
      {recentDecisions.length ? (
        <section className="activity-group" aria-label="研究决策">
          <div className="activity-group-head">
            <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            <strong>AI 的研究结论</strong>
          </div>
          <div className="activity-decision-list">
            {recentDecisions.map((decision) => (
              <article className="activity-decision-item" key={decision.id}>
                <header>
                  <strong>{decision.question}</strong>
                  <span className={decision.status === "approved" ? "tone-good" : "tone-live"}>
                    {decision.status === "approved" ? "已决定" : "待你选择"}
                  </span>
                </header>
                {decision.status === "approved" ? (
                  <p>
                    你选择了：
                    {(Array.isArray(decision.options) ? decision.options : [])
                      .map((option) =>
                        typeof option === "string"
                          ? option
                          : option.option_id === decision.selected_option_id
                          ? option.label
                          : null
                      )
                      .filter(Boolean)
                      .join("、")}
                  </p>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {/* 方案冻结 */}
      {recentSolutions.length ? (
        <section className="activity-group" aria-label="已冻结方案">
          <div className="activity-group-head">
            <PackageSearch className="h-4 w-4" aria-hidden="true" />
            <strong>已冻结的方案</strong>
          </div>
          <div className="activity-solution-list">
            {recentSolutions.map((solution) => (
              <article className="activity-solution-item" key={solution.id}>
                <strong>V{solution.version}</strong>
                <span>{formatTime(solution.created_at)}</span>
                <small>
                  {solution.module_snapshots.length} 个模块 ·{" "}
                  {solution.bom.length} 项清单
                </small>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {/* 影响分析 */}
      {recentImpacts.length ? (
        <section className="activity-group" aria-label="影响分析">
          <div className="activity-group-head">
            <Wrench className="h-4 w-4" aria-hidden="true" />
            <strong>影响分析</strong>
          </div>
          <div className="activity-impact-list">
            {recentImpacts.map((impact) => (
              <article className="activity-impact-item" key={impact.id}>
                <span
                  className={
                    impact.status === "approved" ? "tone-good" : "tone-live"
                  }
                >
                  {impact.status === "approved" ? "已批准" : "待确认"}
                </span>
                <strong>
                  影响 {impact.affected_module_ids.length} 个模块
                </strong>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {/* 现场观察 */}
      {recentObservations.length ? (
        <section className="activity-group" aria-label="现场观察">
          <div className="activity-group-head">
            <SquareStack className="h-4 w-4" aria-hidden="true" />
            <strong>你的现场观察</strong>
          </div>
          <div className="activity-impact-list">
            {recentObservations.map((observation) => (
              <article className="activity-impact-item" key={observation.id}>
                <span className="tone-live">
                  {formatTime(observation.created_at)}
                </span>
                <strong>{observation.statement}</strong>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {/* 历史入口 */}
      <button
        type="button"
        className="activity-history-link"
        onClick={onOpenAudit}
      >
        <History className="h-4 w-4" aria-hidden="true" />
        查看完整工作记录
      </button>
    </div>
  );
}
