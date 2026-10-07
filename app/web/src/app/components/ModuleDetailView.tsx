"use client";

import { useMemo, useState } from "react";
import {
  ArrowLeftRight,
  Beaker,
  GitBranch,
  HelpCircle,
  Link2,
  ShieldCheck,
  Sparkles,
  Wrench,
} from "lucide-react";
import type {
  EvidenceBinding,
  Module,
  ModuleSelection,
  ProjectSnapshot,
} from "@/app/types/types";
import { computeFreshness } from "@/app/utils/freshness";
import { EvidenceDrawer } from "@/app/components/EvidenceDrawer";

function shortId(value: string): string {
  return value.slice(0, 8);
}

function statusTagClass(status: string): string {
  const good = [
    "approved",
    "supported",
    "compatible",
    "succeeded",
    "selected",
    "revised",
  ];
  const bad = [
    "rejected",
    "contradicted",
    "incompatible",
    "stale",
    "retracted",
    "corrupt",
  ];
  const live = [
    "pending",
    "researching",
    "comparing",
    "deciding",
    "verifying",
    "draft",
    "conditional",
    "needs_test",
  ];
  if (good.includes(status)) return "tone-good";
  if (bad.includes(status)) return "tone-bad";
  if (live.includes(status)) return "tone-live";
  return "tone-muted";
}

interface ModuleDetailViewProps {
  module: Module;
  snapshot: ProjectSnapshot;
  onClose: () => void;
}

export function ModuleDetailView({
  module,
  snapshot,
  onClose,
}: ModuleDetailViewProps) {
  const [drawerEvidence, setDrawerEvidence] = useState<EvidenceBinding | null>(
    null
  );

  // Filter data scoped to this module
  const moduleCandidates = useMemo(
    () => snapshot.candidates.filter((c) => c.module_id === module.id),
    [snapshot.candidates, module.id]
  );

  const moduleEvidence = useMemo(
    () => snapshot.evidence.filter((e) => e.module_id === module.id),
    [snapshot.evidence, module.id]
  );

  const moduleCompatibility = useMemo(
    () =>
      snapshot.compatibility_findings.filter((cf) =>
        cf.module_ids.includes(module.id)
      ),
    [snapshot.compatibility_findings, module.id]
  );

  const moduleDecisions = useMemo(
    () =>
      snapshot.decisions.filter((d) =>
        d.affected_module_ids.includes(module.id)
      ),
    [snapshot.decisions, module.id]
  );

  // Find current selection for this module across approved solutions
  const currentSelection = useMemo((): ModuleSelection | null => {
    const approvedSolution = snapshot.solutions.find(
      (s) => s.id === snapshot.project.active_solution_version_id
    );
    if (!approvedSolution) {
      // latest solution
      const latest = snapshot.solutions.at(-1);
      if (!latest) return null;
      const sel = latest.module_snapshots.find(
        (ms) => ms.module_id === module.id
      );
      return sel && "candidate_id" in sel
        ? (sel as unknown as ModuleSelection)
        : null;
    }
    const sel = approvedSolution.module_snapshots.find(
      (ms) => ms.module_id === module.id
    );
    return sel && "candidate_id" in sel
      ? (sel as unknown as ModuleSelection)
      : null;
  }, [
    snapshot.solutions,
    snapshot.project.active_solution_version_id,
    module.id,
  ]);

  // Dependencies
  const dependencyModules = useMemo(
    () =>
      module.dependency_ids
        .map((depId) => snapshot.modules.find((m) => m.id === depId))
        .filter(Boolean) as Module[],
    [module.dependency_ids, snapshot.modules]
  );

  return (
    <div
      className="module-detail-view"
      role="region"
      aria-label={`模块详情: ${module.name}`}
    >
      {/* Header */}
      <div className="module-detail-header">
        <button
          className="module-detail-back"
          onClick={onClose}
        >
          ← 返回模块列表
        </button>
        <div className="module-detail-title">
          <code>{module.key}</code>
          <h2>{module.name}</h2>
          <span className={`status-tag ${statusTagClass(module.stage)}`}>
            {module.stage}
          </span>
        </div>
        <p className="module-detail-responsibility">{module.responsibility}</p>
      </div>

      {/* Metadata grid */}
      <div className="module-detail-meta">
        {/* Dependencies */}
        <section className="module-detail-section">
          <h3>
            <Link2 className="h-4 w-4" />
            依赖关系
          </h3>
          {dependencyModules.length > 0 ? (
            <ul className="module-dep-list">
              {dependencyModules.map((dep) => (
                <li key={dep.id}>
                  <code>{dep.key}</code>
                  <span>{dep.name}</span>
                  <span className={`status-tag ${statusTagClass(dep.stage)}`}>
                    {dep.stage}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="module-detail-empty">
              无上游依赖 — 该模块可独立决策。
            </p>
          )}
        </section>

        {/* Stage timeline */}
        <section className="module-detail-section">
          <h3>
            <GitBranch className="h-4 w-4" />
            当前阶段
          </h3>
          <div className="module-stage-badge">
            <span className={`status-tag ${statusTagClass(module.stage)}`}>
              {module.stage}
            </span>
          </div>
        </section>

        {/* Acceptance criteria */}
        {module.acceptance.length > 0 && (
          <section className="module-detail-section">
            <h3>
              <ShieldCheck className="h-4 w-4" />
              验收标准
            </h3>
            <ul className="module-acceptance-list">
              {module.acceptance.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </section>
        )}

        {/* Open questions */}
        {module.open_questions.length > 0 && (
          <section className="module-detail-section">
            <h3>
              <HelpCircle className="h-4 w-4" />
              未解问题
            </h3>
            <ul className="module-questions-list">
              {module.open_questions.map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
          </section>
        )}
      </div>

      {/* Candidates */}
      <section className="module-detail-section module-detail-section--wide">
        <h3>
          <Sparkles className="h-4 w-4" />
          候选方案 ({moduleCandidates.length})
        </h3>
        {moduleCandidates.length > 0 ? (
          <div className="module-candidates-grid">
            {moduleCandidates.map((candidate) => (
              <article
                className="candidate-detail-card"
                key={candidate.id}
              >
                <header>
                  <strong>{candidate.name}</strong>
                  <code>{shortId(candidate.id)}</code>
                </header>
                <p>{candidate.description}</p>
                {candidate.risks.length > 0 && (
                  <div className="candidate-risks">
                    <small>风险</small>
                    <ul>
                      {candidate.risks.map((r) => (
                        <li key={r}>{r}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {candidate.evidence_binding_ids.length > 0 && (
                  <div className="candidate-evidence-refs">
                    <small>
                      关联证据: {candidate.evidence_binding_ids.length} 条
                    </small>
                    <div className="candidate-evidence-links">
                      {candidate.evidence_binding_ids.map((ebId) => {
                        const binding = snapshot.evidence.find(
                          (e) => e.id === ebId
                        );
                        if (!binding) return null;
                        return (
                          <button
                            key={ebId}
                            className="evidence-ref-chip"
                            onClick={() => setDrawerEvidence(binding)}
                          >
                            <span
                              className={`status-tag ${statusTagClass(
                                binding.status
                              )}`}
                            >
                              {binding.status}
                            </span>
                            <span className="evidence-ref-claim">
                              {binding.claim.slice(0, 60)}…
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </article>
            ))}
          </div>
        ) : (
          <p className="module-detail-empty">
            尚未生成候选方案 — 等待研究完成。
          </p>
        )}
      </section>

      {/* Evidence */}
      <section className="module-detail-section module-detail-section--wide">
        <h3>
          <Beaker className="h-4 w-4" />
          证据绑定 ({moduleEvidence.length})
        </h3>
        {moduleEvidence.length > 0 ? (
          <div className="module-evidence-grid">
            {moduleEvidence.map((binding) => {
              const freshness = computeFreshness(binding);
              return (
                <article
                  className="evidence-compact-card"
                  key={binding.id}
                >
                  <header>
                    <span
                      className={`status-tag ${statusTagClass(binding.status)}`}
                    >
                      {binding.status}
                    </span>
                    <span
                      className={`status-tag ${
                        freshness.freshness === "fresh"
                          ? "tone-good"
                          : freshness.freshness === "aging"
                          ? "tone-live"
                          : "tone-bad"
                      }`}
                    >
                      {freshness.freshness === "fresh"
                        ? "新鲜"
                        : freshness.freshness === "aging"
                        ? "老化中"
                        : "过时"}
                    </span>
                  </header>
                  <p>{binding.claim}</p>
                  <div className="evidence-compact-footer">
                    <code>{binding.snapshot_hash.slice(0, 12)}…</code>
                    <button
                      className="evidence-inspect-btn"
                      onClick={() => setDrawerEvidence(binding)}
                    >
                      查看详情
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <p className="module-detail-empty">
            尚未收集证据 — 启动研究后证据会出现在这里。
          </p>
        )}
      </section>

      {/* Compatibility */}
      {moduleCompatibility.length > 0 && (
        <section className="module-detail-section module-detail-section--wide">
          <h3>
            <ArrowLeftRight className="h-4 w-4" />
            兼容性 ({moduleCompatibility.length})
          </h3>
          <div className="module-compat-grid">
            {moduleCompatibility.map((cf) => (
              <article
                className="compat-card"
                key={cf.id}
              >
                <header>
                  <span className={`status-tag ${statusTagClass(cf.status)}`}>
                    {cf.status}
                  </span>
                  <code>{cf.rule_id}</code>
                </header>
                <p>{cf.summary}</p>
                {cf.required_test && (
                  <div className="compat-test-needed">
                    <Beaker className="h-3 w-3" />
                    <small>需要测试: {cf.required_test}</small>
                  </div>
                )}
              </article>
            ))}
          </div>
        </section>
      )}

      {/* Decisions */}
      {moduleDecisions.length > 0 && (
        <section className="module-detail-section module-detail-section--wide">
          <h3>
            <GitBranch className="h-4 w-4" />
            相关决策 ({moduleDecisions.length})
          </h3>
          <div className="module-decisions-list">
            {moduleDecisions.map((decision) => (
              <article
                className="decision-ref-card"
                key={decision.id}
              >
                <header>
                  <span
                    className={`status-tag ${statusTagClass(decision.status)}`}
                  >
                    {decision.status}
                  </span>
                  <strong>{decision.question}</strong>
                </header>
                {decision.selected_option_id && (
                  <p>
                    已选: <code>{decision.selected_option_id}</code>
                  </p>
                )}
              </article>
            ))}
          </div>
        </section>
      )}

      {/* Current Selection / Solution */}
      {currentSelection && (
        <section className="module-detail-section module-detail-section--wide">
          <h3>
            <Wrench className="h-4 w-4" />
            当前方案选择
          </h3>
          <div className="current-selection-card">
            <strong>{currentSelection.candidate_name}</strong>
            <p>{currentSelection.rationale}</p>
            {currentSelection.risks.length > 0 && (
              <div className="selection-risks">
                <small>风险</small>
                <ul>
                  {currentSelection.risks.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </section>
      )}

      {/* Evidence Drawer (overlay) */}
      {drawerEvidence && (
        <div
          className="evidence-drawer-overlay"
          onClick={() => setDrawerEvidence(null)}
        >
          <div onClick={(e) => e.stopPropagation()}>
            <EvidenceDrawer
              evidence={computeFreshness(drawerEvidence)}
              onClose={() => setDrawerEvidence(null)}
            />
          </div>
        </div>
      )}
    </div>
  );
}
