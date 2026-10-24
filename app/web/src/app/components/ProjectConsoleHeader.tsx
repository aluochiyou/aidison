"use client";

import { ArrowLeft, Check, Code2, Database, Download, RefreshCw } from "lucide-react";
import type { Project } from "@/app/types/types";
import { Button } from "@/components/ui/button";
import { getClient } from "@/lib/api";

const PROJECT_STAGES = [
  ["intake", "项目"],
  ["requirements", "需求"],
  ["research", "研究"],
  ["comparing", "比较"],
  ["deciding", "决策"],
  ["approved", "方案"],
  ["verifying", "验证"],
  ["revising", "修订"],
] as const;

interface ProjectConsoleHeaderProps {
  project: Project;
  connected: boolean;
  developerMode: boolean;
  onBack: () => void;
  onRefresh: () => void;
  onToggleDeveloperMode: () => void;
}

function ProjectStageRail({ project }: { project: Project }) {
  const activeIndex = PROJECT_STAGES.findIndex(
    ([stage]) => stage === project.stage
  );

  return (
    <div
      className="stage-rail"
      aria-label={`当前阶段：${project.stage}`}
      role="list"
    >
      {PROJECT_STAGES.map(([stage, label], index) => (
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

export function ProjectConsoleHeader({
  project,
  connected,
  developerMode,
  onBack,
  onRefresh,
  onToggleDeveloperMode,
}: ProjectConsoleHeaderProps) {
  return (
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
          onClick={onRefresh}
        >
          <RefreshCw className="h-4 w-4" />
          刷新
        </Button>
        {project.active_solution_version_id ? (
          <Button asChild size="sm">
            <a
              download
              href={getClient().getProjectReportUrl(project.id)}
              title="导出已确认方案及其证据链"
            >
              <Download className="h-4 w-4" />
              导出报告
            </a>
          </Button>
        ) : null}
        <Button
          aria-pressed={developerMode}
          className="developer-mode-toggle"
          onClick={onToggleDeveloperMode}
          size="sm"
          variant="ghost"
        >
          <Code2 className="h-4 w-4" />
          {developerMode ? "退出开发者模式" : "开发者模式"}
        </Button>
      </div>
      {developerMode ? <ProjectStageRail project={project} /> : null}
    </header>
  );
}
