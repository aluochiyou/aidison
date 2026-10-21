import type { Module, ProjectWorkspaceProjectionV1 } from "@/app/types/types";

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

export function StatusTag({ status }: { status: string }) {
  return (
    <span className={`status-tag ${statusTone(status)}`}>
      {STATUS_LABELS[status] ?? status}
    </span>
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

export function ProjectPulse({
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
    .map(
      (moduleId) =>
        workspace.modules.find((module) => module.id === moduleId)?.name
    )
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

export function ModuleCard({
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
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
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
