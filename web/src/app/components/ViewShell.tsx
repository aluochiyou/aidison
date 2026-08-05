"use client";

import { useMemo } from "react";
import {
  AlertCircle,
  Beaker,
  CheckCircle2,
  GitBranch,
  Monitor,
  ShoppingCart,
  Wrench,
} from "lucide-react";
import type { ConsoleView, ProjectSnapshot } from "@/app/types/types";
import { ResearchEvidenceView } from "@/app/components/ResearchEvidenceView";
import { DecisionInbox } from "@/app/components/DecisionInbox";
import { VerificationRevisionsView } from "@/app/components/VerificationRevisionsView";
import { ShoppingView } from "@/app/components/ShoppingView";
import { IntegrationHealthCard } from "@/app/components/IntegrationHealthCard";
import { SolutionDiffView } from "@/app/components/SolutionDiffView";
import { ModuleDetailView } from "@/app/components/ModuleDetailView";
import { ArtifactViewer } from "@/app/components/ArtifactViewer";

interface ViewShellProps {
  snapshot: ProjectSnapshot;
  view: ConsoleView;
  moduleId: string | null;
  artifactId: string | null;
  decisionId: string | null;
  solutionVersionId: string | null;
  onSetView: (view: ConsoleView) => void;
  onSelectModule: (moduleId: string) => void;
  onSelectArtifact: (artifactId: string) => void;
  onSelectDecision: (decisionId: string) => void;
  onSelectSolutionVersion: (solutionVersionId: string) => void;
  onClearOverlay: () => void;
  onRefresh: () => void;
}

const NAV_ITEMS: {
  view: ConsoleView;
  label: string;
  icon: React.ReactNode;
}[] = [
  { view: "research", label: "研究证据", icon: <Beaker className="h-4 w-4" /> },
  {
    view: "decision-inbox",
    label: "决策收件箱",
    icon: <GitBranch className="h-4 w-4" />,
  },
  {
    view: "verification",
    label: "验证修订",
    icon: <Wrench className="h-4 w-4" />,
  },
  {
    view: "shopping",
    label: "Shopping",
    icon: <ShoppingCart className="h-4 w-4" />,
  },
  {
    view: "integration-health",
    label: "集成健康",
    icon: <AlertCircle className="h-4 w-4" />,
  },
];

export function ViewShell({
  snapshot,
  view,
  moduleId,
  artifactId,
  decisionId,
  solutionVersionId,
  onSetView,
  onSelectModule,
  onSelectArtifact,
  onSelectDecision,
  onSelectSolutionVersion,
  onClearOverlay,
  onRefresh,
}: ViewShellProps) {
  // compute active decision context
  const _focusedDecision = useMemo(
    () =>
      decisionId
        ? snapshot.decisions.find((d) => d.id === decisionId) ?? null
        : null,
    [snapshot.decisions, decisionId],
  );

  // compute focused module for drill-down
  const focusedModule = useMemo(
    () =>
      moduleId
        ? snapshot.modules.find((m) => m.id === moduleId) ?? null
        : null,
    [snapshot.modules, moduleId],
  );

  // ── Render main view ──

  const renderMainView = () => {
    switch (view) {
      case "research":
        return (
          <ResearchEvidenceView
            snapshot={snapshot}
            onSelectModule={onSelectModule}
          />
        );
      case "decision-inbox":
        return <DecisionInbox snapshot={snapshot} onResolved={onRefresh} />;
      case "verification":
        return (
          <VerificationRevisionsView
            snapshot={snapshot}
            onSelectSolution={onSelectSolutionVersion}
          />
        );
      case "shopping":
        return <ShoppingView snapshot={snapshot} />;
      case "integration-health":
        return <IntegrationHealthCard />;
      default:
        return (
          <ResearchEvidenceView
            snapshot={snapshot}
            onSelectModule={onSelectModule}
          />
        );
    }
  };

  return (
    <div className="view-shell">
      {/* Left Nav Rail */}
      <nav className="view-nav" aria-label="视图导航">
        <div className="view-nav-label">
          <Monitor className="h-4 w-4" />
          <small>VIEWS</small>
        </div>
        <ul>
          {NAV_ITEMS.map((item) => (
            <li key={item.view}>
              <button
                className={`view-nav-item ${view === item.view ? "is-active" : ""}`}
                onClick={() => onSetView(item.view)}
                aria-current={view === item.view ? "page" : undefined}
              >
                {item.icon}
                <span>{item.label}</span>
                {/* Badge counts */}
                {item.view === "decision-inbox" &&
                  snapshot.decisions.filter((d) => d.status === "pending")
                    .length > 0 && (
                    <span className="view-nav-badge">
                      {
                        snapshot.decisions.filter(
                          (d) => d.status === "pending",
                        ).length
                      }
                    </span>
                  )}
                {item.view === "shopping" &&
                  snapshot.solutions.length > 0 && (
                    <CheckCircle2 className="h-3 w-3 tone-good" />
                  )}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      {/* Main Content Area */}
      <div className="view-content">
        {renderMainView()}
      </div>

      {/* Overlays: Module Detail, Artifact Viewer, Solution Diff */}
      {focusedModule && (
        <div className="view-overlay" onClick={onClearOverlay}>
          <div
            className="view-overlay-panel"
            onClick={(e) => e.stopPropagation()}
          >
            <ModuleDetailView
              module={focusedModule}
              snapshot={snapshot}
              onClose={onClearOverlay}
            />
          </div>
        </div>
      )}

      {artifactId && (
        <div className="view-overlay" onClick={onClearOverlay}>
          <div
            className="view-overlay-panel"
            onClick={(e) => e.stopPropagation()}
          >
            <ArtifactViewer
              artifactId={artifactId}
              onClose={onClearOverlay}
            />
          </div>
        </div>
      )}

      {solutionVersionId && (
        <div className="view-overlay" onClick={onClearOverlay}>
          <div
            className="view-overlay-panel view-overlay-panel--wide"
            onClick={(e) => e.stopPropagation()}
          >
            <SolutionDiffView
              versionId={solutionVersionId}
              snapshot={snapshot}
              onClose={onClearOverlay}
            />
          </div>
        </div>
      )}
    </div>
  );
}
