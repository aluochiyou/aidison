"use client";

import { parseAsString, parseAsStringLiteral, useQueryStates } from "nuqs";
import { useCallback, useMemo } from "react";
import type { ConsoleView } from "@/app/types/types";

const VIEW_OPTIONS: ConsoleView[] = [
  "research",
  "decision-inbox",
  "verification",
  "shopping",
  "integration-health",
];

/**
 * URL-recoverable view state — preserves which view, module, artifact, decision,
 * and solution version are selected across page refreshes.
 */
export function useViewState() {
  const [state, setState] = useQueryStates(
    {
      view: parseAsStringLiteral(VIEW_OPTIONS).withDefault("research"),
      moduleId: parseAsString.withDefault(""),
      artifactId: parseAsString.withDefault(""),
      decisionId: parseAsString.withDefault(""),
      solutionVersionId: parseAsString.withDefault(""),
    },
    { history: "replace" }
  );

  const setView = useCallback(
    (view: ConsoleView) => setState({ view }),
    [setState]
  );

  const selectModule = useCallback(
    (moduleId: string) => setState({ moduleId: moduleId || null }),
    [setState]
  );

  const selectArtifact = useCallback(
    (artifactId: string) => setState({ artifactId: artifactId || null }),
    [setState]
  );

  const selectDecision = useCallback(
    (decisionId: string) => setState({ decisionId: decisionId || null }),
    [setState]
  );

  const selectSolutionVersion = useCallback(
    (solutionVersionId: string) =>
      setState({ solutionVersionId: solutionVersionId || null }),
    [setState]
  );

  const clearOverlay = useCallback(
    () =>
      setState({
        moduleId: null,
        artifactId: null,
        decisionId: null,
        solutionVersionId: null,
      }),
    [setState]
  );

  const effectiveModuleId = useMemo(
    () => state.moduleId || null,
    [state.moduleId]
  );
  const effectiveArtifactId = useMemo(
    () => state.artifactId || null,
    [state.artifactId]
  );
  const effectiveDecisionId = useMemo(
    () => state.decisionId || null,
    [state.decisionId]
  );
  const effectiveSolutionVersionId = useMemo(
    () => state.solutionVersionId || null,
    [state.solutionVersionId]
  );

  return {
    view: state.view,
    moduleId: effectiveModuleId,
    artifactId: effectiveArtifactId,
    decisionId: effectiveDecisionId,
    solutionVersionId: effectiveSolutionVersionId,
    setView,
    selectModule,
    selectArtifact,
    selectDecision,
    selectSolutionVersion,
    clearOverlay,
  };
}
