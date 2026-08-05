"use client";

import { useMemo } from "react";
import {
  AlertTriangle,
  GitBranch,
  RotateCw,
  Wrench,
} from "lucide-react";
import type { ProjectSnapshot } from "@/app/types/types";

function shortId(value: string): string {
  return value.slice(0, 8);
}

interface VerificationRevisionsViewProps {
  snapshot: ProjectSnapshot;
  onSelectSolution: (solutionVersionId: string) => void;
}

export function VerificationRevisionsView({
  snapshot,
  onSelectSolution,
}: VerificationRevisionsViewProps) {
  const moduleById = useMemo(
    () => new Map(snapshot.modules.map((m) => [m.id, m])),
    [snapshot.modules],
  );

  return (
    <div
      className="verification-revisions-view"
      role="region"
      aria-label="验证与修订"
    >
      <div className="verification-header">
        <div>
          <small>VERIFICATION & REVISIONS</small>
          <h2>验证与修订</h2>
        </div>
      </div>

      {/* Observations */}
      <section className="verification-section">
        <h3>
          <AlertTriangle className="h-4 w-4" />
          现场观察 ({snapshot.observations.length})
        </h3>
        {snapshot.observations.length > 0 ? (
          <div className="observations-list">
            {snapshot.observations.map((obs) => (
              <article className="observation-card" key={obs.id}>
                <p>{obs.statement}</p>
                <footer>
                  <span>
                    影响模块:{" "}
                    {obs.affected_module_hints
                      .map((hint) => {
                        const m = snapshot.modules.find(
                          (mod) => mod.id === hint,
                        );
                        return m?.name ?? shortId(hint);
                      })
                      .join("、") || "无"}
                  </span>
                  <code>
                    {new Date(obs.created_at).toLocaleString("zh-CN")}
                  </code>
                </footer>
              </article>
            ))}
          </div>
        ) : (
          <p className="verification-empty">
            暂无观察记录。已部署方案后，可以在这里提交现场制造和测试反馈。
          </p>
        )}
      </section>

      {/* Impact Analyses */}
      <section className="verification-section">
        <h3>
          <Wrench className="h-4 w-4" />
          影响分析 ({snapshot.impacts.length})
        </h3>
        {snapshot.impacts.length > 0 ? (
          <div className="impacts-list">
            {snapshot.impacts.map((impact) => (
              <article
                className={`impact-summary-card ${impact.status === "proposed" ? "is-active" : ""}`}
                key={impact.id}
              >
                <header>
                  <span
                    className={`status-tag ${impact.status === "approved" ? "tone-good" : impact.status === "rejected" ? "tone-bad" : "tone-live"}`}
                  >
                    {impact.status}
                  </span>
                  <strong>{impact.summary}</strong>
                </header>
                <div className="impact-summary-meta">
                  <span>
                    直接影响: {impact.direct_affected_module_ids.length} 模块
                  </span>
                  <span>
                    传递影响: {impact.transitive_affected_module_ids.length} 模块
                  </span>
                  {impact.stale_evidence_binding_ids.length > 0 && (
                    <span className="impact-stale-evidence">
                      过期证据: {impact.stale_evidence_binding_ids.length}
                    </span>
                  )}
                </div>
                {impact.module_patches.length > 0 && (
                  <div className="impact-patches-mini">
                    {impact.module_patches.map((patch) => {
                      const m = moduleById.get(patch.module_id);
                      return (
                        <div className="impact-patch-mini" key={patch.module_id}>
                          <RotateCw className="h-3 w-3" />
                          <span>
                            {m?.name ?? shortId(patch.module_id)} →{" "}
                            {patch.replacement.candidate_name}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
                <div className="impact-date-row">
                  <code>
                    {new Date(impact.created_at).toLocaleString("zh-CN")}
                  </code>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="verification-empty">
            暂无影响分析。提交观察后，服务端生成影响分析提案。
          </p>
        )}
      </section>

      {/* Solution Versions Timeline */}
      <section className="verification-section">
        <h3>
          <GitBranch className="h-4 w-4" />
          方案版本链 ({snapshot.solutions.length})
        </h3>
        {snapshot.solutions.length > 0 ? (
          <div className="solution-chain">
            {snapshot.solutions.map((solution, index) => (
              <div className="solution-chain-node" key={solution.id}>
                <div className="solution-chain-indicator">
                  <span className="solution-chain-dot" />
                  {index < snapshot.solutions.length - 1 && (
                    <span className="solution-chain-line" />
                  )}
                </div>
                <div className="solution-chain-card">
                  <header>
                    <strong>V{solution.version}</strong>
                    <span
                      className={`status-tag ${solution.approved_decision_id ? "tone-good" : "tone-muted"}`}
                    >
                      {solution.id ===
                      snapshot.project.active_solution_version_id
                        ? "活跃"
                        : "历史"}
                    </span>
                  </header>
                  <div className="solution-chain-meta">
                    <span>
                      {solution.module_snapshots.length} 模块 ·{" "}
                      {solution.bom.length} BOM ·{" "}
                      {solution.implementation_steps.length} 实施步骤
                    </span>
                    <code>
                      {new Date(solution.created_at).toLocaleString("zh-CN")}
                    </code>
                  </div>
                  {solution.previous_version_id && (
                    <button
                      className="solution-diff-link"
                      onClick={() => onSelectSolution(solution.id)}
                    >
                      查看 V{snapshot.solutions.find((s) => s.id === solution.previous_version_id)?.version ?? "?"} → V{solution.version} 语义 Diff
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="verification-empty">
            尚未有方案版本。决策批准后方案会被冻结为不可变版本。
          </p>
        )}
      </section>
    </div>
  );
}
