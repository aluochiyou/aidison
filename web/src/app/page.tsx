"use client";

import { useEffect, useState } from "react";
import { ArrowRight, FolderOpen, Plus } from "lucide-react";
import { ProjectConsole } from "@/app/components/ProjectConsole";
import { CreateProjectDialog } from "@/app/components/CreateProjectDialog";
import { ProjectSelector } from "@/app/components/ProjectSelector";
import { useProjects } from "@/app/hooks/useProjects";
import { Button } from "@/components/ui/button";
import type { Project } from "@/app/types/types";

export default function HomePage() {
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [restoringProject, setRestoringProject] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [showSelector, setShowSelector] = useState(false);
  const { createProject, loadProject, loading, error } = useProjects();

  useEffect(() => {
    const projectId = new URLSearchParams(window.location.search).get("project");
    if (!projectId) {
      setRestoringProject(false);
      return;
    }
    void loadProject(projectId)
      .then((snapshot) => setActiveProject(snapshot.project))
      .catch(() => window.history.replaceState(null, "", window.location.pathname))
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
    return <main className="launch-shell launch-restoring">正在恢复工程项目…</main>;
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
      <div className="launch-grid" aria-hidden="true" />
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
          Aidison 把需求、研究证据、候选方案、用户决策和现场反馈串成一条可审计闭环。
          Agent 提建议，只有你的确认才能写入项目事实。
        </p>
        <div className="launch-actions">
          <Button size="lg" onClick={() => setShowCreate(true)}>
            <Plus className="h-4 w-4" />
            创建项目
          </Button>
          <Button size="lg" variant="outline" onClick={() => setShowSelector(true)}>
            <FolderOpen className="h-4 w-4" />
            打开项目
          </Button>
        </div>
        {error ? <p className="launch-error">{error}</p> : null}
      </section>

      <aside className="launch-specimen" aria-label="Aidison workflow preview">
        <div className="specimen-head">
          <span>PROJECT / QUAD-01</span>
          <span>REV 07</span>
        </div>
        <div className="specimen-route">
          {["需求冻结", "并行研究", "兼容决策", "方案版本", "反馈修订"].map(
            (label, index) => (
              <div className="specimen-step" key={label}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <strong>{label}</strong>
                <ArrowRight className="h-4 w-4" />
              </div>
            ),
          )}
        </div>
        <div className="specimen-note">
          <code>PostgreSQL canonical truth</code>
          <span>Agent output → Proposal → Human decision</span>
        </div>
      </aside>

      <CreateProjectDialog
        open={showCreate}
        onOpenChange={setShowCreate}
        onSubmit={async (name, goal) => {
          const project = await createProject(name, goal);
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
