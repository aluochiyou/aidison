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
  try { return JSON.parse(raw); } catch { return []; }
}

function storeProject(id: string, name: string) {
  const list = getStoredProjects().filter(p => p.id !== id);
  list.unshift({ id, name, lastAccessed: new Date().toISOString() });
  localStorage.setItem(PROJECTS_KEY, JSON.stringify(list.slice(0, 20)));
}

export function useProjects() {
  const [projects, setProjects] = useState<StoredProject[]>(getStoredProjects());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setProjects(getStoredProjects());
  }, []);

  const createProject = useCallback(async (name: string, goal: string) => {
    setLoading(true);
    setError(null);
    try {
      const { data } = await getClient().createProject(name, goal);
      storeProject(data.id, data.name);
      refresh();
      return data;
    } catch (err: unknown) {
      const msg = err && typeof err === "object" && "error" in err
        ? (err as { error: { message: string } }).error.message
        : String(err);
      setError(msg);
      throw err;
    } finally {
      setLoading(false);
    }
  }, [refresh]);

  const loadProject = useCallback(async (id: string): Promise<ProjectSnapshot> => {
    setLoading(true);
    setError(null);
    try {
      const snap = await getClient().getSnapshot(id);
      storeProject(snap.project.id, snap.project.name);
      refresh();
      return snap;
    } catch (err: unknown) {
      const msg = err && typeof err === "object" && "error" in err
        ? (err as { error: { message: string } }).error.message
        : String(err);
      setError(msg);
      throw err;
    } finally {
      setLoading(false);
    }
  }, [refresh]);

  useEffect(() => { refresh(); }, [refresh]);

  return { projects, loading, error, createProject, loadProject, refresh };
}
