"use client";

import { useCallback, useEffect, useState } from "react";
import { getClient } from "@/lib/api";
import type { ProjectSnapshot } from "@/app/types/types";

const PROJECTS_KEY = "aidison-projects";

interface StoredProject {
  id: string;
  name: string;
  lastAccessed: string;
}

function getStoredProjects(): StoredProject[] {
  if (typeof window === "undefined") return [];
  const raw = localStorage.getItem(PROJECTS_KEY);
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (project): project is StoredProject =>
        Boolean(project) &&
        typeof project === "object" &&
        typeof (project as StoredProject).id === "string" &&
        typeof (project as StoredProject).name === "string" &&
        typeof (project as StoredProject).lastAccessed === "string"
    );
  } catch {
    return [];
  }
}

function storeProject(id: string, name: string) {
  const list = getStoredProjects().filter((p) => p.id !== id);
  list.unshift({ id, name, lastAccessed: new Date().toISOString() });
  localStorage.setItem(PROJECTS_KEY, JSON.stringify(list.slice(0, 20)));
}

export function useProjects() {
  const [projects, setProjects] = useState<StoredProject[]>(
    getStoredProjects()
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const stored = getStoredProjects();
    // The project picker is deliberately a local "recent projects" list.
    // Listing every database project here made test fixtures and unrelated
    // historical projects appear in the normal user entry point. A project
    // that has not been opened in this browser can still be opened by UUID.
    setProjects(stored);
  }, []);

  const createProject = useCallback(
    async (name: string, goal: string) => {
      setLoading(true);
      setError(null);
      try {
        const { data } = await getClient().createProject(name, goal);
        storeProject(data.id, data.name);
        await refresh();
        return data;
      } catch (err: unknown) {
        const msg =
          err && typeof err === "object" && "error" in err
            ? (err as { error: { message: string } }).error.message
            : String(err);
        setError(msg);
        throw err;
      } finally {
        setLoading(false);
      }
    },
    [refresh]
  );

  const loadProject = useCallback(
    async (id: string): Promise<ProjectSnapshot> => {
      setLoading(true);
      setError(null);
      try {
        const snap = await getClient().getSnapshot(id);
        storeProject(snap.project.id, snap.project.name);
        await refresh();
        return snap;
      } catch (err: unknown) {
        const msg =
          err && typeof err === "object" && "error" in err
            ? (err as { error: { message: string } }).error.message
            : String(err);
        setError(msg);
        throw err;
      } finally {
        setLoading(false);
      }
    },
    [refresh]
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { projects, loading, error, createProject, loadProject, refresh };
}
