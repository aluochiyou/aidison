"use client";

import { useMemo, useState } from "react";
import { Beaker, FileText, FlaskConical, Globe, Sparkles } from "lucide-react";
import type {
  Candidate,
  EvidenceBinding,
  ProjectSnapshot,
} from "@/app/types/types";
import { computeFreshness, freshLabel, freshTone } from "@/app/utils/freshness";
import { EvidenceDrawer } from "@/app/components/EvidenceDrawer";

function shortId(value: string): string {
  return value.slice(0, 8);
}

interface ResearchEvidenceViewProps {
  snapshot: ProjectSnapshot;
  onSelectModule: (moduleId: string) => void;
}

export function ResearchEvidenceView({
  snapshot,
  onSelectModule,
}: ResearchEvidenceViewProps) {
  const [drawerEvidence, setDrawerEvidence] = useState<EvidenceBinding | null>(
    null
  );

  // Group candidates by module
  const candidatesByModule = useMemo(() => {
    const map = new Map<string, Candidate[]>();
    for (const c of snapshot.candidates) {
      const existing = map.get(c.module_id) ?? [];
      existing.push(c);
      map.set(c.module_id, existing);
    }
    return map;
  }, [snapshot.candidates]);

  // Evidence grouped by module
  const evidenceByModule = useMemo(() => {
    const map = new Map<string, EvidenceBinding[]>();
    for (const e of snapshot.evidence) {
      const existing = map.get(e.module_id) ?? [];
      existing.push(e);
      map.set(e.module_id, existing);
    }
    return map;
  }, [snapshot.evidence]);

  // All module IDs that have any evidence or candidates
  const activeModuleIds = useMemo(
    () => new Set([...candidatesByModule.keys(), ...evidenceByModule.keys()]),
    [candidatesByModule, evidenceByModule]
  );

  return (
    <div
      className="research-evidence-view"
      role="region"
      aria-label="研究与证据"
    >
      <div className="research-header">
        <div>
          <small>INDEPENDENT RESEARCH & EVIDENCE</small>
          <h2>研究与证据</h2>
        </div>
        <div className="research-stats">
          <span className="research-stat">
            <Sparkles className="h-4 w-4" /> {snapshot.candidates.length} 候选
          </span>
          <span className="research-stat">
            <FileText className="h-4 w-4" /> {snapshot.evidence.length} 证据
          </span>
        </div>
      </div>

      {snapshot.candidates.length === 0 && snapshot.evidence.length === 0 ? (
        <div className="research-empty">
          <FlaskConical className="h-8 w-8" />
          <p>
            尚未有研究结果。启动 Research Wave 后，证据和候选方案会出现在这里。
          </p>
        </div>
      ) : (
        <div className="research-content">
          {/* Candidates section */}
          {snapshot.candidates.length > 0 && (
            <section className="research-section">
              <h3>
                <Sparkles className="h-4 w-4" />
                候选方案
              </h3>
              <div className="candidates-grid">
                {snapshot.candidates.map((candidate) => {
                  const module = snapshot.modules.find(
                    (m) => m.id === candidate.module_id
                  );
                  return (
                    <article
                      className="candidate-research-card"
                      key={candidate.id}
                    >
                      <header>
                        <strong>{candidate.name}</strong>
                        <button
                          className="candidate-module-link"
                          onClick={() => module && onSelectModule(module.id)}
                        >
                          {module?.name ?? shortId(candidate.module_id)}
                        </button>
                      </header>
                      <p>{candidate.description}</p>
                      {candidate.risks.length > 0 && (
                        <div className="candidate-risks-compact">
                          <small>风险: {candidate.risks.join("；")}</small>
                        </div>
                      )}
                      <div className="candidate-attrs">
                        {Object.entries(candidate.attributes).map(
                          ([key, value]) => (
                            <span
                              className="candidate-attr-tag"
                              key={key}
                            >
                              {key}:{" "}
                              {typeof value === "string"
                                ? value.slice(0, 40)
                                : JSON.stringify(value).slice(0, 40)}
                            </span>
                          )
                        )}
                      </div>
                      <div className="candidate-evidence-count">
                        <small>
                          绑定证据: {candidate.evidence_binding_ids.length}
                        </small>
                      </div>
                    </article>
                  );
                })}
              </div>
            </section>
          )}

          {/* Evidence section — grouped by module */}
          {snapshot.evidence.length > 0 && (
            <section className="research-section">
              <h3>
                <Beaker className="h-4 w-4" />
                证据绑定
              </h3>

              {Array.from(activeModuleIds).map((moduleId) => {
                const evidence = evidenceByModule.get(moduleId) ?? [];
                if (evidence.length === 0) return null;
                const module = snapshot.modules.find((m) => m.id === moduleId);
                return (
                  <div
                    className="evidence-module-group"
                    key={moduleId}
                  >
                    <div className="evidence-module-group-header">
                      <h4>{module?.name ?? shortId(moduleId)}</h4>
                      <button
                        className="evidence-module-link"
                        onClick={() => module && onSelectModule(module.id)}
                      >
                        查看模块 →
                      </button>
                    </div>
                    <div className="evidence-cards-grid">
                      {evidence.map((binding) => {
                        const freshness = computeFreshness(binding);
                        return (
                          <article
                            className="evidence-research-card"
                            key={binding.id}
                          >
                            <header>
                              <span
                                className={`status-tag ${
                                  binding.status === "supported"
                                    ? "tone-good"
                                    : "tone-bad"
                                }`}
                              >
                                {binding.status}
                              </span>
                              <span
                                className={`status-tag ${freshTone(
                                  freshness.freshness
                                )}`}
                              >
                                {freshLabel(freshness.freshness)}
                              </span>
                            </header>
                            <p className="evidence-claim">{binding.claim}</p>
                            {binding.span_text && (
                              <blockquote className="evidence-span">
                                {binding.span_text.slice(0, 200)}
                                {binding.span_text.length > 200 ? "…" : ""}
                              </blockquote>
                            )}
                            <footer className="evidence-card-footer">
                              {binding.source_url && (
                                <a
                                  href={binding.source_url}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  <Globe className="h-3 w-3" />
                                  SOURCE
                                </a>
                              )}
                              <code>{binding.snapshot_hash.slice(0, 12)}…</code>
                              <button
                                className="evidence-detail-btn"
                                onClick={() => setDrawerEvidence(binding)}
                              >
                                详情
                              </button>
                            </footer>
                          </article>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </section>
          )}
        </div>
      )}

      {/* Evidence Drawer */}
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
