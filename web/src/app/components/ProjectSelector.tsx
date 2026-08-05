"use client";

import { useState } from "react";
import { ArrowRight, FolderClock } from "lucide-react";
import { useProjects } from "@/app/hooks/useProjects";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { Project } from "@/app/types/types";

interface ProjectSelectorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect: (project: Project) => void;
  busy?: boolean;
}

export function ProjectSelector({
  open,
  onOpenChange,
  onSelect,
  busy = false,
}: ProjectSelectorProps) {
  const { projects, loadProject, loading, error } = useProjects();
  const [projectQuery, setProjectQuery] = useState("");
  const [queryError, setQueryError] = useState<string | null>(null);

  const openProject = async (id: string) => {
    setQueryError(null);
    const snapshot = await loadProject(id);
    onSelect(snapshot.project);
  };

  const openProjectQuery = async () => {
    const query = projectQuery.trim();
    const normalized = query.toLocaleLowerCase("zh-CN");
    const match = projects.find(
      (project) =>
        project.id === query || project.name.toLocaleLowerCase("zh-CN").includes(normalized),
    );
    if (!match) {
      setQueryError("没有找到该项目，请输入项目名称或 UUID");
      return;
    }
    await openProject(match.id);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>打开工程项目</DialogTitle>
          <DialogDescription>
            最近项目只保存在此浏览器；项目事实始终从 PostgreSQL 重新读取。
          </DialogDescription>
        </DialogHeader>
        <div className="project-picker-list">
          {projects.length ? (
            projects.map((project) => (
              <button key={project.id} onClick={() => void openProject(project.id)}>
                <FolderClock className="h-4 w-4" />
                <span>
                  <strong>{project.name}</strong>
                  <small>{project.id}</small>
                </span>
                <ArrowRight className="h-4 w-4" />
              </button>
            ))
          ) : (
            <p className="empty-copy">这里还没有最近打开的项目。</p>
          )}
        </div>
        <div className="project-id-row">
          <Input
            value={projectQuery}
            onChange={(event) => {
              setProjectQuery(event.target.value);
              setQueryError(null);
            }}
            placeholder="输入项目名称或 UUID"
          />
          <Button
            disabled={!projectQuery.trim() || loading || busy}
            onClick={() => void openProjectQuery()}
          >
            读取
          </Button>
        </div>
        {queryError || error ? <p className="form-error">{queryError || error}</p> : null}
      </DialogContent>
    </Dialog>
  );
}
