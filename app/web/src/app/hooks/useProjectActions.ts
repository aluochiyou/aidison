"use client";

import { useCallback, useState } from "react";
import { getClient } from "@/lib/api";

export function useProjectActions(
  projectId: string | null,
  onRefresh: () => void
) {
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const wrap = useCallback(
    async <T>(label: string, fn: () => Promise<T>): Promise<T> => {
      setActionLoading(label);
      setActionError(null);
      try {
        return await fn();
      } catch (err: unknown) {
        const msg =
          err && typeof err === "object" && "error" in err
            ? (err as { error: { message: string } }).error.message
            : String(err);
        setActionError(msg);
        throw err;
      } finally {
        setActionLoading(null);
        onRefresh();
      }
    },
    [onRefresh]
  );

  const approveRequirements = useCallback(
    async (
      revision: number,
      body: Parameters<ReturnType<typeof getClient>["approveRequirements"]>[2]
    ) => {
      if (!projectId) throw new Error("no project");
      return wrap("approve_requirements", () =>
        getClient().approveRequirements(projectId, revision, body)
      );
    },
    [projectId, wrap]
  );

  const submitResearchProposal = useCallback(
    async (
      revision: number,
      body: Parameters<
        ReturnType<typeof getClient>["submitResearchProposal"]
      >[2]
    ) => {
      if (!projectId) throw new Error("no project");
      return wrap("submit_research", () =>
        getClient().submitResearchProposal(projectId, revision, body)
      );
    },
    [projectId, wrap]
  );

  const resolveDecision = useCallback(
    async (
      decisionId: string,
      revision: number,
      option: string,
      basisHash: string,
      executionPlanId: string
    ) => {
      if (!projectId) throw new Error("no project");
      return wrap("resolve_decision", () =>
        getClient().resolveDecision(
          decisionId,
          revision,
          option,
          basisHash,
          executionPlanId
        )
      );
    },
    [projectId, wrap]
  );

  const freezeSolution = useCallback(
    async (
      revision: number,
      body: Parameters<ReturnType<typeof getClient>["freezeSolution"]>[2]
    ) => {
      if (!projectId) throw new Error("no project");
      return wrap("freeze_solution", () =>
        getClient().freezeSolution(projectId, revision, body)
      );
    },
    [projectId, wrap]
  );

  const submitObservation = useCallback(
    async (
      revision: number,
      statement: string,
      affectedModuleIds: string[],
      executionPlanId: string
    ) => {
      if (!projectId) throw new Error("no project");
      return wrap("submit_observation", () =>
        getClient().submitObservation(
          projectId,
          revision,
          statement,
          affectedModuleIds,
          executionPlanId
        )
      );
    },
    [projectId, wrap]
  );

  const approveImpact = useCallback(
    async (impactId: string, revision: number, basisHash: string) => {
      if (!projectId) throw new Error("no project");
      return wrap("approve_impact", () =>
        getClient().approveImpact(impactId, revision, basisHash)
      );
    },
    [projectId, wrap]
  );

  return {
    actionLoading,
    actionError,
    approveRequirements,
    submitResearchProposal,
    resolveDecision,
    freezeSolution,
    submitObservation,
    approveImpact,
  };
}
