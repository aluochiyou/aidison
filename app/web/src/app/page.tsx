"use client";

import { useEffect, useState } from "react";
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
      <section className="launch-copy">
        <div className="launch-kicker">
          <span className="status-lamp" />
          AIDISON · 工程项目工作台
        </div>
        <h1>
          <span className="launch-title-primary">从一个想法开始，</span>
          <span className="launch-title-accent">做出靠谱的方案。</span>
        </h1>
        <p>
          先写需求单，再拆模块、查资料、比较选型。每一步都留在项目里；
          方案由你确认，不会被自动改掉。
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
        className="launch-workflow"
        aria-label="项目会经历的四步"
      >
        <div className="launch-workflow-title">
          <span>工程合成台</span>
          <small>把资料和模块装进同一个项目</small>
        </div>
        <div
          className="launch-crafting-preview"
          aria-hidden="true"
        >
          <img
            className="launch-preview-item is-book"
            src="/art/mc/book.png"
            alt=""
          />
          <img
            className="launch-preview-item is-table"
            src="/art/mc/crafting-table.png"
            alt=""
          />
          <img
            className="launch-preview-item is-compass"
            src="/art/mc/compass.png"
            alt=""
          />
          <img
            className="launch-preview-item is-result"
            src="/art/mc/chest.png"
            alt=""
          />
        </div>
        {[
          {
            icon: "book",
            title: "创建需求单",
            detail: "写下目标、预算和必须满足的条件",
          },
          {
            icon: "crafting-table",
            title: "拆分模块",
            detail: "确认各部分的职责与依赖",
          },
          {
            icon: "compass",
            title: "研究与选型",
            detail: "对照来源，比较候选的得失",
          },
          {
            icon: "chest",
            title: "确认并导出",
            detail: "定下方案，保留证据和修改记录",
          },
        ].map((step, index) => (
          <div
            className="launch-workflow-step"
            key={step.title}
          >
            <span className="launch-step-number">
              {String(index + 1).padStart(2, "0")}
            </span>
            <span className="launch-step-icon">
              <img
                src={`/art/mc/${step.icon}.png`}
                alt=""
              />
            </span>
            <span className="launch-step-copy">
              <strong>{step.title}</strong>
              <small>{step.detail}</small>
            </span>
          </div>
        ))}
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
              "项目已创建，但未能开始整理需求。打开项目后，可以在“与 AI 对话”中新建会话。"
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
