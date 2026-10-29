"use client";

import {
  Bot,
  Boxes,
  CheckCircle2,
  Database,
  GitBranch,
  Network,
  Route,
  Search,
  ShieldCheck,
} from "lucide-react";
import type { AgentRunSummary, ProjectSnapshot } from "@/app/types/types";

interface DeveloperExecutionMapProps {
  snapshot: ProjectSnapshot;
  eventConnected: boolean;
}

const RUN_KIND_LABELS: Record<string, string> = {
  research: "研究任务 Agent",
  module_visual: "模块视觉 Agent",
  impact: "影响分析 Agent",
  impact_analysis: "影响分析 Agent",
  solution: "方案归并 Agent",
  solution_composition: "方案归并 Agent",
  verifier: "交叉核验 Agent",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队",
  running: "执行中",
  waiting: "等待确认",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function runKindLabel(run: AgentRunSummary): string {
  return RUN_KIND_LABELS[run.kind] ?? `${run.kind} Agent`;
}

function runStatusClass(status: AgentRunSummary["status"]): string {
  if (status === "succeeded") return "is-complete";
  if (status === "running" || status === "queued" || status === "waiting") {
    return "is-active";
  }
  if (status === "failed") return "is-error";
  return "is-pending";
}

export function DeveloperExecutionMap({
  snapshot,
  eventConnected,
}: DeveloperExecutionMapProps) {
  const runs = (snapshot.agent_runs ?? [])
    .toSorted((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, 5);
  const activeRuns = runs.filter((run) =>
    ["queued", "running", "waiting"].includes(run.status)
  );
  const latestPlan = (snapshot.execution_plans ?? []).toSorted((left, right) =>
    right.created_at.localeCompare(left.created_at)
  )[0];
  const planTasks = latestPlan?.research_strategy?.tasks ?? [];
  const taskCount = planTasks.length;
  const moduleNameById = new Map(
    snapshot.modules.map((module) => [module.id, module.name])
  );
  const scopedModuleCount = new Set(
    latestPlan?.research_strategy?.tasks.flatMap((task) => task.module_ids) ??
      []
  ).size;
  const admittedTaskCount = runs.reduce(
    (total, run) => total + (run.research_progress?.admitted_task_count ?? 0),
    0
  );

  const stages = [
    {
      key: "requirements",
      title: "需求与版本",
      detail: "用户确认后形成可追踪修订",
      metric: snapshot.project.active_requirement_revision_id
        ? `项目 r${snapshot.project.revision}`
        : "等待需求确认",
      state: snapshot.project.active_requirement_revision_id
        ? "complete"
        : "active",
      Icon: Boxes,
    },
    {
      key: "compile",
      title: "计划编译",
      detail: "校验任务结构、依赖、权限与预算",
      metric: latestPlan
        ? `${taskCount} 个任务 · ${latestPlan.status}`
        : "尚未生成计划",
      state: latestPlan ? "complete" : "pending",
      Icon: GitBranch,
    },
    {
      key: "schedule",
      title: "依赖调度",
      detail: "只派发依赖已满足的就绪任务",
      metric: latestPlan
        ? `并发上限 ${latestPlan.max_concurrency}`
        : "等待计划",
      state: activeRuns.length ? "active" : latestPlan ? "complete" : "pending",
      Icon: Route,
    },
    {
      key: "agents",
      title: "临时任务 Agent",
      detail: "每次能力调用临时装配 LangGraph 子图",
      metric: activeRuns.length
        ? `${activeRuns.length} 个正在运行`
        : `${runs.length} 条近期运行`,
      state: activeRuns.length
        ? "active"
        : runs.length
        ? "complete"
        : "pending",
      Icon: Bot,
    },
    {
      key: "admission",
      title: "结果准入",
      detail: "覆盖率、证据与冲突通过后才可归并",
      metric: `${admittedTaskCount} 个任务已准入`,
      state: admittedTaskCount
        ? "complete"
        : runs.length
        ? "active"
        : "pending",
      Icon: ShieldCheck,
    },
    {
      key: "facts",
      title: "项目事实",
      detail: "正式结论进入 PostgreSQL，原文进入 Artifact",
      metric: `${snapshot.evidence.length} 条证据 · ${snapshot.solutions.length} 个方案`,
      state:
        snapshot.evidence.length || snapshot.solutions.length
          ? "complete"
          : "pending",
      Icon: Database,
    },
  ] as const;

  return (
    <section
      className="developer-execution-map"
      aria-label="Agent 执行链路"
      data-testid="developer-execution-map"
    >
      <header className="developer-map-heading">
        <div>
          <small>运行架构 · 实时投影</small>
          <h2>任务如何从需求流转为项目事实</h2>
          <p>
            调度器把已批准计划编译成有依赖的任务；任务按模块范围读取记忆，
            按能力标识和工具白名单执行。结果须通过准入，才进入项目事实。
          </p>
        </div>
        <div className="developer-live-badges">
          <span className={eventConnected ? "is-online" : "is-offline"}>
            <i />
            事件流{eventConnected ? "在线" : "断开"}
          </span>
          <span>游标 #{snapshot.event_cursor}</span>
          <span>{scopedModuleCount || snapshot.modules.length} 个模块范围</span>
        </div>
      </header>

      <div
        className="developer-flow"
        aria-label="六阶段执行流程"
      >
        {stages.map(({ key, title, detail, metric, state, Icon }, index) => (
          <div
            className="developer-flow-step-wrap"
            key={key}
          >
            <article className={`developer-flow-step is-${state}`}>
              <div className="developer-flow-icon">
                <Icon className="h-5 w-5" />
              </div>
              <small>{String(index + 1).padStart(2, "0")}</small>
              <strong>{title}</strong>
              <p>{detail}</p>
              <span>{metric}</span>
            </article>
            {index < stages.length - 1 ? (
              <div
                className="developer-flow-arrow"
                aria-hidden="true"
              >
                →
              </div>
            ) : null}
          </div>
        ))}
      </div>

      <section
        className="developer-agent-anatomy"
        aria-label="任务 Agent 构成"
      >
        <div className="developer-subheading">
          <div>
            <Bot className="h-4 w-4" />
            <strong>一项研究任务怎样变成临时 Agent</strong>
          </div>
          <small>
            LangGraph 负责检查点与节点执行；业务准入由 Aidison 控制层负责
          </small>
        </div>

        <div className="developer-anatomy-flow">
          <article>
            <small>计划任务</small>
            <strong>模块范围 + 前置任务</strong>
            <p>
              计划列出目标、覆盖范围和依赖；编译后再绑定能力、工具与运行预算。
            </p>
          </article>
          <span aria-hidden="true">→</span>
          <article>
            <small>执行封套 TaskEnvelope</small>
            <strong>输入引用 + 权限 + 预算</strong>
            <p>
              携带 capability、coverage_keys、allowed_tool_ids 和
              budget_ref；覆盖键映射回模块范围。
            </p>
          </article>
          <span aria-hidden="true">→</span>
          <article>
            <small>上下文装配</small>
            <strong>只加载本任务可用材料</strong>
            <p>
              组合模块资料、已准入证据与协作线索；校验范围并生成上下文清单。
            </p>
          </article>
          <span aria-hidden="true">→</span>
          <article>
            <small>LangGraph 能力子图</small>
            <strong>装配上下文 → 执行能力</strong>
            <p>
              返回有类型的结果引用；子图不另建长期会话或检查点，父图的任务记录负责恢复。
            </p>
          </article>
        </div>

        <div className="developer-collaboration-strip">
          <Network className="h-4 w-4" />
          <p>
            <strong>实时协作：</strong>研究线索先写入
            PostgreSQL，再通知同一运行范围内的订阅任务；
            进程内有界队列负责即时唤醒，断线后按序号回查。线索只是检索提示，不是证据或项目事实。
          </p>
        </div>

        <div className="developer-plan-task-list">
          <div className="developer-plan-task-heading">
            <strong>当前计划任务与依赖</strong>
            <span>
              {latestPlan
                ? `${taskCount} 项 · ${latestPlan.status}`
                : "等待生成计划"}
            </span>
          </div>
          {planTasks.length ? (
            <div className="developer-plan-task-grid">
              {planTasks.slice(0, 6).map((task) => (
                <article key={task.task_key}>
                  <strong>{task.title}</strong>
                  <small>
                    模块：
                    {task.module_ids
                      .map((id) => moduleNameById.get(id) ?? id.slice(0, 8))
                      .join("、") || "全局"}
                  </small>
                  <small>
                    前置：
                    {task.depends_on_task_keys.join("、") ||
                      "无，可进入首批并发"}
                  </small>
                </article>
              ))}
              {taskCount > 6 ? (
                <small className="developer-plan-task-more">
                  其余 {taskCount - 6} 项在完整计划中
                </small>
              ) : null}
            </div>
          ) : (
            <p className="developer-plan-task-empty">
              需求批准并生成研究计划后，这里会显示真实的任务、模块范围和依赖；现在不会用示例数据冒充运行状态。
            </p>
          )}
        </div>
      </section>

      <div className="developer-map-bottom">
        <section className="developer-agent-pool">
          <div className="developer-subheading">
            <div>
              <Network className="h-4 w-4" />
              <strong>子 Agent 构成与流转</strong>
            </div>
            <small>模块范围决定记忆 · 能力类型决定工具</small>
          </div>
          <div className="developer-agent-list">
            {runs.length ? (
              runs.map((run) => (
                <article
                  className="developer-agent-card"
                  key={run.id}
                >
                  <div>
                    <Bot className="h-4 w-4" />
                    <strong>{runKindLabel(run)}</strong>
                    <span className={runStatusClass(run.status)}>
                      {RUN_STATUS_LABELS[run.status] ?? run.status}
                    </span>
                  </div>
                  <p>
                    基于项目 r{run.basis_project_revision} · 运行{" "}
                    {run.id.slice(0, 8)}
                  </p>
                  {run.research_progress ? (
                    <small>
                      初始 {run.research_progress.initial_task_count} · 准入{" "}
                      {run.research_progress.admitted_task_count} · 补充{" "}
                      {run.research_progress.additional_task_count}
                    </small>
                  ) : (
                    <small>该能力子图没有研究任务计数</small>
                  )}
                  {run.latest_error ? (
                    <small className="developer-agent-error">
                      最近错误：{run.latest_error}
                    </small>
                  ) : null}
                </article>
              ))
            ) : (
              <div className="developer-empty-agent">
                <Search className="h-5 w-5" />
                当前没有任务 Agent；批准执行计划后，调度器会按依赖动态创建。
              </div>
            )}
          </div>
        </section>

        <aside className="developer-legend">
          <strong>三类状态不要混淆</strong>
          <p>
            <CheckCircle2 className="h-4 w-4" />
            <span>
              <b>项目事实</b>：用户确认且准入后的长期结论。
            </span>
          </p>
          <p>
            <GitBranch className="h-4 w-4" />
            <span>
              <b>运行状态</b>：队列、重试、预算和子图进度。
            </span>
          </p>
          <p>
            <Database className="h-4 w-4" />
            <span>
              <b>原始材料</b>：网页快照与报告文件，由引用指针按需加载。
            </span>
          </p>
        </aside>
      </div>
    </section>
  );
}
