"use client";

import { useEffect, useMemo, useRef } from "react";
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
import { RunCenter } from "@/app/components/RunCenter";
import { CostWorkbench } from "@/app/components/CostWorkbench";

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
  developerMode: boolean;
}

const NAV_ITEMS: {
  view: ConsoleView;
  label: string;
  icon: React.ReactNode;
}[] = [
  { view: "run-center", label: "运行中心", icon: <Monitor className="h-4 w-4" /> },
  { view: "cost-workbench", label: "成本工作台", icon: <Beaker className="h-4 w-4" /> },
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

/**
 * Hook: close overlay on Escape key.
 */
function useEscapeClose(onClose: () => void) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);
}

function Overlay({
  children,
  onClose,
  wide,
}: {
  children: React.ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  useEscapeClose(onClose);

  // Focus trap: focus the panel when mounted
  useEffect(() => {
    const el = panelRef.current;
    if (el) {
      const firstFocusable = el.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      firstFocusable?.focus();
    }
  }, []);

  return (
    <div
      className="view-overlay"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        className={`view-overlay-panel ${
          wide ? "view-overlay-panel--wide" : ""
        }`}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

export function ViewShell({
  snapshot,
  view,
  moduleId,
  artifactId,
  solutionVersionId,
  onSetView,
  onSelectModule,
  onSelectArtifact,
  onSelectSolutionVersion,
  onClearOverlay,
  onRefresh,
  developerMode,
}: ViewShellProps) {
  // compute focused module for drill-down
  const focusedModule = useMemo(
    () =>
      moduleId ? snapshot.modules.find((m) => m.id === moduleId) ?? null : null,
    [snapshot.modules, moduleId]
  );

  // ── Render main view ──

  const renderMainView = () => {
    switch (view) {
      case "run-center":
        return <RunCenter snapshot={snapshot} onRefresh={onRefresh} />;
      case "cost-workbench":
        return <CostWorkbench snapshot={snapshot} />;
      case "research":
        return (
          <ResearchEvidenceView
            snapshot={snapshot}
            onSelectModule={onSelectModule}
          />
        );
      case "decision-inbox":
        return (
          <DecisionInbox
            snapshot={snapshot}
            onResolved={onRefresh}
          />
        );
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
        return developerMode ? (
          <IntegrationHealthCard />
        ) : (
          <ResearchEvidenceView
            snapshot={snapshot}
            onSelectModule={onSelectModule}
          />
        );
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
      <nav
        className="view-nav"
        aria-label="视图导航"
      >
        <div className="view-nav-label">
          <Monitor className="h-4 w-4" />
          <small>VIEWS</small>
        </div>
        <ul>
          {NAV_ITEMS.filter(
            (item) => developerMode || item.view !== "integration-health"
          ).map((item) => (
            <li key={item.view}>
              <button
                className={`view-nav-item ${
                  view === item.view ? "is-active" : ""
                }`}
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
                        snapshot.decisions.filter((d) => d.status === "pending")
                          .length
                      }
                    </span>
                  )}
                {item.view === "shopping" && snapshot.solutions.length > 0 && (
                  <CheckCircle2 className="tone-good h-3 w-3" />
                )}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      {/* Main Content Area */}
      <div className="view-content">{renderMainView()}</div>

      {/* Overlays: Module Detail, Artifact Viewer, Solution Diff */}
      {focusedModule && (
        <Overlay onClose={onClearOverlay}>
          <ModuleDetailView
            module={focusedModule}
            snapshot={snapshot}
            onClose={onClearOverlay}
          />
        </Overlay>
      )}

      {artifactId && (
        <Overlay onClose={onClearOverlay}>
          <ArtifactViewer
            projectId={snapshot.project.id}
            artifactId={artifactId}
            onClose={onClearOverlay}
          />
        </Overlay>
      )}

      {solutionVersionId && (
        <Overlay
          onClose={onClearOverlay}
          wide
        >
          <SolutionDiffView
            versionId={solutionVersionId}
            snapshot={snapshot}
            onClose={onClearOverlay}
          />
        </Overlay>
      )}
    </div>
  );
}
