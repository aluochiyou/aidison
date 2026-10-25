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
  const latestPlan = (snapshot.execution_plans ?? [])
    .toSorted((left, right) => right.created_at.localeCompare(left.created_at))[0];
  const taskCount = latestPlan?.research_strategy?.tasks.length ?? 0;
  const scopedModuleCount = new Set(
    latestPlan?.research_strategy?.tasks.flatMap((task) => task.module_ids) ?? []
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
      detail: "一个任务对应一张可循环的 LangGraph 子图",
      metric: activeRuns.length
        ? `${activeRuns.length} 个正在运行`
        : `${runs.length} 条近期运行`,
      state: activeRuns.length ? "active" : runs.length ? "complete" : "pending",
      Icon: Bot,
    },
    {
      key: "admission",
      title: "结果准入",
      detail: "覆盖率、证据与冲突通过后才可归并",
      metric: `${admittedTaskCount} 个任务已准入`,
      state: admittedTaskCount ? "complete" : runs.length ? "active" : "pending",
      Icon: ShieldCheck,
    },
    {
      key: "facts",
      title: "项目事实",
      detail: "正式结论进入 PostgreSQL，原文进入 Artifact",
      metric: `${snapshot.evidence.length} 条证据 · ${snapshot.solutions.length} 个方案`,
      state: snapshot.evidence.length || snapshot.solutions.length
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
            主调度器负责编译和派发；任务 Agent 只在自己的模块记忆范围内工作，
            并按能力类型获得搜索、读取或核验工具。
          </p>
        </div>
        <div className="developer-live-badges">
          <span className={eventConnected ? "is-online" : "is-offline"}>
            <i />事件流{eventConnected ? "在线" : "断开"}
          </span>
          <span>游标 #{snapshot.event_cursor}</span>
          <span>{scopedModuleCount || snapshot.modules.length} 个模块范围</span>
        </div>
      </header>

      <div className="developer-flow" aria-label="六阶段执行流程">
        {stages.map(({ key, title, detail, metric, state, Icon }, index) => (
          <div className="developer-flow-step-wrap" key={key}>
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
              <div className="developer-flow-arrow" aria-hidden="true">
                →
              </div>
            ) : null}
          </div>
        ))}
      </div>

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
                <article className="developer-agent-card" key={run.id}>
                  <div>
                    <Bot className="h-4 w-4" />
                    <strong>{runKindLabel(run)}</strong>
                    <span className={runStatusClass(run.status)}>
                      {RUN_STATUS_LABELS[run.status] ?? run.status}
                    </span>
                  </div>
                  <p>
                    基于项目 r{run.basis_project_revision} · 运行 {run.id.slice(0, 8)}
                  </p>
                  {run.research_progress ? (
                    <small>
                      初始 {run.research_progress.initial_task_count} · 准入 {" "}
                      {run.research_progress.admitted_task_count} · 补充 {" "}
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
            <span><b>项目事实</b>：用户确认且准入后的长期结论。</span>
          </p>
          <p>
            <GitBranch className="h-4 w-4" />
            <span><b>运行状态</b>：队列、重试、预算和子图进度。</span>
          </p>
          <p>
            <Database className="h-4 w-4" />
            <span><b>原始材料</b>：网页快照与报告文件，由引用指针按需加载。</span>
          </p>
        </aside>
      </div>
    </section>
  );
}
