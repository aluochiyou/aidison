"use client";

import React, { useEffect, useState } from "react";
import { FolderOpen, Plus } from "lucide-react";
import { toast } from "sonner";
import { ProjectConsole } from "@/app/components/ProjectConsole";
import { CreateProjectDialog } from "@/app/components/CreateProjectDialog";
import { ProjectSelector } from "@/app/components/ProjectSelector";
import { useProjects } from "@/app/hooks/useProjects";
import { Button } from "@/components/ui/button";
import { getClient } from "@/lib/api";
import type { Project } from "@/app/types/types";

function buildClarificationPrompt(project: Project): string {
  return (
    `我已创建项目「${project.name}」，目标：${project.goal}\n\n` +
    "请先只向我确认一两个最关键的边界，不要启动研究，也不要修改任何项目事实。" +
    "确认后据此整理一份初始需求草案，供我审阅后再确认。"
  );
}

export default function HomePage() {
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [restoringProject, setRestoringProject] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [showSelector, setShowSelector] = useState(false);
  const { createProject, loadProject, loading, error } = useProjects();

  useEffect(() => {
    const projectId = new URLSearchParams(window.location.search).get(
      "project"
    );
    if (!projectId) {
      setRestoringProject(false);
      return;
    }
    void loadProject(projectId)
      .then((snapshot) => setActiveProject(snapshot.project))
      .catch(() =>
        window.history.replaceState(null, "", window.location.pathname)
      )
      .finally(() => setRestoringProject(false));
  }, [loadProject]);

  const selectProject = (project: Project) => {
    setActiveProject(project);
    const url = new URL(window.location.href);
    url.searchParams.set("project", project.id);
    window.history.replaceState(null, "", url);
  };

  const closeProject = () => {
    setActiveProject(null);
    window.history.replaceState(null, "", window.location.pathname);
  };

  if (restoringProject) {
    return (
      <main className="launch-shell launch-restoring">正在恢复工程项目…</main>
    );
  }

  if (activeProject) {
    return (
      <ProjectConsole
        initialProject={activeProject}
        onBack={closeProject}
      />
    );
  }

  return (
    <main className="launch-shell">
      <div
        className="launch-grid"
        aria-hidden="true"
      />
      <section className="launch-copy">
        <div className="launch-kicker">
          <span className="status-lamp" />
          Local engineering workspace
        </div>
        <h1>
          把 DIY 想法变成
          <span>可验证的工程方案</span>
        </h1>
        <p>
          Aidison
          把需求、研究证据、候选方案、用户决策和现场反馈串成一条可审计闭环。
          Agent 提建议，只有你的确认才能写入项目事实。
        </p>
        <div className="launch-actions">
          <Button
            size="lg"
            onClick={() => setShowCreate(true)}
          >
            <Plus className="h-4 w-4" />
            创建项目
          </Button>
          <Button
            size="lg"
            variant="outline"
            onClick={() => setShowSelector(true)}
          >
            <FolderOpen className="h-4 w-4" />
            打开项目
          </Button>
        </div>
        {error ? <p className="launch-error">{error}</p> : null}
      </section>

      <aside
        className="launch-specimen"
        aria-label="Aidison collaboration preview"
      >
        <div className="specimen-head">
          <span>你的想法 → 可验证的方案</span>
          <span>你决定 · AI 执行</span>
        </div>
        <div className="specimen-loop" aria-hidden="true">
          {[
            { icon: "✎", label: "需求" },
            { icon: "▦", label: "模块" },
            { icon: "◎", label: "候选" },
            { icon: "✓", label: "决策" },
            { icon: "▣", label: "方案" },
            { icon: "↺", label: "反馈" },
          ].map((item, index) => (
            <div
              className="specimen-loop-node"
              key={item.label}
              style={
                {
                  "--node-angle": `${index * 60}deg`,
                } as React.CSSProperties
              }
            >
              <span className="specimen-loop-icon">{item.icon}</span>
              <strong>{item.label}</strong>
            </div>
          ))}
          <div className="specimen-loop-center">
            <span>闭环协作</span>
            <small>你确认才生效</small>
          </div>
        </div>
        <div className="specimen-note">
          <span>AI 负责查找、比较、提议</span>
          <span>你负责确认边界与最终决定</span>
        </div>
      </aside>

      <CreateProjectDialog
        open={showCreate}
        onOpenChange={setShowCreate}
        onSubmit={async (name, goal) => {
          let project: Project;
          try {
            project = await createProject(name, goal);
          } catch (createError) {
            toast.error(
              createError &&
                typeof createError === "object" &&
                "error" in createError &&
                typeof (createError as { error: { message?: unknown } }).error
                  ?.message === "string"
                ? (createError as { error: { message: string } }).error.message
                : "项目创建失败，请稍后重试。"
            );
            throw createError;
          }
          try {
            const session = await getClient().newConversationSession(
              project.id
            );
            await getClient().postConversationMessage(
              project.id,
              buildClarificationPrompt(project),
              session.data.id
            );
          } catch {
            toast.error(
              "项目已创建，但自动开启澄清对话失败；进入项目后可在“与 AI 对话”页手动新建会话并重试。"
            );
          }
          selectProject(project);
          setShowCreate(false);
        }}
      />
      <ProjectSelector
        open={showSelector}
        onOpenChange={setShowSelector}
        busy={loading}
        onSelect={(project) => {
          selectProject(project);
          setShowSelector(false);
        }}
      />
    </main>
  );
}
