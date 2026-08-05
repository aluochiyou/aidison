"use client";

import { useMemo } from "react";
import {
  ArrowRight,
  Beaker,
  Check,
  Clock,
  GitBranch,
  Package,
  RotateCw,
  Wrench,
} from "lucide-react";
import type { ProjectSnapshot, SolutionVersion } from "@/app/types/types";

function shortId(value: string): string {
  return value.slice(0, 8);
}

interface SolutionDiffViewProps {
  versionId: string;
  snapshot: ProjectSnapshot;
  onClose: () => void;
}

function moduleSnapshotKey(
  snapshot: SolutionVersion["module_snapshots"][number],
): string {
  return "snapshot_hash" in snapshot && typeof snapshot.snapshot_hash === "string"
    ? snapshot.snapshot_hash
    : JSON.stringify(snapshot);
}

function moduleSnapshotCandidate(
  snapshot: SolutionVersion["module_snapshots"][number],
): string | null {
  return "candidate_id" in snapshot && typeof snapshot.candidate_id === "string"
    ? (snapshot as { candidate_name?: string }).candidate_name ?? null
    : null;
}

export function SolutionDiffView({ versionId, snapshot, onClose }: SolutionDiffViewProps) {
  const solution = useMemo(
    () => snapshot.solutions.find((s) => s.id === versionId) ?? null,
    [snapshot.solutions, versionId],
  );

  const previous = useMemo(
    () =>
      solution?.previous_version_id
        ? snapshot.solutions.find((s) => s.id === solution.previous_version_id) ?? null
        : null,
    [snapshot.solutions, solution?.previous_version_id],
  );

  const moduleById = useMemo(
    () => new Map(snapshot.modules.map((m) => [m.id, m])),
    [snapshot.modules],
  );

  const impact = useMemo(
    () =>
      previous
        ? snapshot.impacts.find((i) => i.base_solution_version_id === previous.id) ?? null
        : null,
    [snapshot.impacts, previous],
  );

  if (!solution) {
    return (
      <div className="solution-diff-empty">
        <p>方案版本未找到。</p>
        <button onClick={onClose}>返回</button>
      </div>
    );
  }

  return <SolutionDiffInner solution={solution} previous={previous} impact={impact} snapshot={snapshot} moduleById={moduleById} onClose={onClose} />;
}

interface SolutionDiffInnerProps {
  solution: SolutionVersion;
  previous: SolutionVersion | null;
  impact: import("@/app/types/types").ImpactAnalysis | null;
  snapshot: ProjectSnapshot;
  moduleById: Map<string, import("@/app/types/types").Module>;
  onClose: () => void;
}

function SolutionDiffInner({ solution, previous, impact, snapshot, moduleById, onClose }: SolutionDiffInnerProps) {
  // Compute changed / reused module sets
  const previousHashes = useMemo(
    () => new Map((previous?.module_snapshots ?? []).map((ms) => [ms.module_id, moduleSnapshotKey(ms)])),
    [previous],
  );

  const { changed, reused, added, removed } = useMemo(() => {
    const currentIds = new Set(solution.module_snapshots.map((s) => s.module_id));
    const previousIds = new Set(previous?.module_snapshots.map((s) => s.module_id) ?? []);

    const ch: SolutionVersion["module_snapshots"] = [];
    const re: SolutionVersion["module_snapshots"] = [];
    const ad = solution.module_snapshots.filter((s) => !previousIds.has(s.module_id));
    const rm = (previous?.module_snapshots ?? []).filter((s) => !currentIds.has(s.module_id));

    for (const ms of solution.module_snapshots) {
      if (!previousIds.has(ms.module_id)) continue;
      const prevHash = previousHashes.get(ms.module_id);
      if (prevHash && moduleSnapshotKey(ms) !== prevHash) {
        ch.push(ms);
      } else {
        re.push(ms);
      }
    }

    return { changed: ch, reused: re, added: ad, removed: rm };
  }, [solution, previous, previousHashes]);

  // BOM diff
  const bomDiff = useMemo(() => {
    const prevBomIds = new Set(
      (previous?.bom ?? []).map((item, i) => "line_id" in item ? item.line_id : `legacy-${i}`),
    );
    const changedBom = solution.bom.filter((item, i) => {
      const key = "line_id" in item ? item.line_id : `legacy-${i}`;
      return prevBomIds.has(key);
    });
    const addedBom = solution.bom.filter((item, i) => {
      const key = "line_id" in item ? item.line_id : `legacy-${i}`;
      return !prevBomIds.has(key);
    });
    return { changed: changedBom, added: addedBom };
  }, [solution.bom, previous?.bom]);

  // Steps diff
  const stepDiff = useMemo(() => {
    const prevStepIds = new Set(
      (previous?.implementation_steps ?? []).map((s, i) => "step_id" in s ? s.step_id : `legacy-${i}`),
    );
    const changedSteps = solution.implementation_steps.filter((s, i) => {
      const key = "step_id" in s ? s.step_id : `legacy-${i}`;
      return prevStepIds.has(key);
    });
    const addedSteps = solution.implementation_steps.filter((s, i) => {
      const key = "step_id" in s ? s.step_id : `legacy-${i}`;
      return !prevStepIds.has(key);
    });
    return { changed: changedSteps, added: addedSteps };
  }, [solution.implementation_steps, previous?.implementation_steps]);

  // Stale evidence (from impact analysis)
  const staleEvidence = useMemo(() => {
    if (!impact) return [];
    return impact.stale_evidence_binding_ids
      .map((ebId) => snapshot.evidence.find((e) => e.id === ebId))
      .filter(Boolean);
  }, [impact, snapshot.evidence]);

  return (
    <div className="solution-diff-view" role="region" aria-label="方案版本对比">
      <div className="solution-diff-header">
        <button className="solution-diff-back" onClick={onClose}>
          ← 返回
        </button>
        <div className="solution-diff-titles">
          <h2>
            {previous ? (
              <>
                V{previous.version} <ArrowRight className="h-4 w-4" /> V{solution.version}
              </>
            ) : (
              <>V{solution.version} (初始版本)</>
            )}
          </h2>
          <code>{solution.basis_hash.slice(0, 16)}…</code>
        </div>
        <span className="solution-diff-date">
          {new Date(solution.created_at).toLocaleString("zh-CN")}
        </span>
      </div>

      {!previous ? (
        <div className="solution-diff-origin">
          <p>这是第一个方案版本，无前驱可对比。以下是完整快照：</p>
          <div className="solution-diff-overview">
            <div className="diff-stat">
              <GitBranch className="h-4 w-4" />
              <strong>{solution.module_snapshots.length}</strong>
              <span>模块</span>
            </div>
            <div className="diff-stat">
              <Package className="h-4 w-4" />
              <strong>{solution.bom.length}</strong>
              <span>BOM 项</span>
            </div>
            <div className="diff-stat">
              <Wrench className="h-4 w-4" />
              <strong>{solution.implementation_steps.length}</strong>
              <span>实施步骤</span>
            </div>
            <div className="diff-stat">
              <Beaker className="h-4 w-4" />
              <strong>{solution.verification_steps.length}</strong>
              <span>验证步骤</span>
            </div>
          </div>
          {/* Module list */}
          <div className="solution-diff-modules">
            <h3>模块快照</h3>
            {solution.module_snapshots.map((ms) => (
              <div className="diff-module-row origin" key={ms.module_id}>
                <Check className="h-3 w-3" />
                <span>{moduleById.get(ms.module_id)?.name ?? shortId(ms.module_id)}</span>
                <span className="diff-module-tag tone-good">origin</span>
                <code>{moduleSnapshotKey(ms).slice(0, 10)}…</code>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="solution-diff-body">
          {/* Module diff */}
          <section className="diff-section">
            <h3>模块语义 Diff</h3>

            {changed.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-live">CHANGED ({changed.length})</small>
                {changed.map((ms) => {
                  const prevMs = previous.module_snapshots.find(
                    (p) => p.module_id === ms.module_id,
                  );
                  const prevName = prevMs
                    ? moduleSnapshotCandidate(prevMs)
                    : null;
                  const currName = moduleSnapshotCandidate(ms) ?? shortId(ms.module_id);
                  return (
                    <div className="diff-module-row changed" key={ms.module_id}>
                      <RotateCw className="h-3 w-3" />
                      <span>{moduleById.get(ms.module_id)?.name ?? shortId(ms.module_id)}</span>
                      <div className="diff-module-change-detail">
                        {prevName && prevName !== currName ? (
                          <span className="diff-module-candidate-change">
                            {prevName} → {currName}
                          </span>
                        ) : (
                          <span className="diff-module-hash-change">hash 变更</span>
                        )}
                      </div>
                      <code>{moduleSnapshotKey(ms).slice(0, 10)}…</code>
                    </div>
                  );
                })}
              </div>
            )}

            {added.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-good">ADDED ({added.length})</small>
                {added.map((ms) => (
                  <div className="diff-module-row added" key={ms.module_id}>
                    <span>+</span>
                    <span>{moduleById.get(ms.module_id)?.name ?? shortId(ms.module_id)}</span>
                    <code>{moduleSnapshotKey(ms).slice(0, 10)}…</code>
                  </div>
                ))}
              </div>
            )}

            {removed.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-bad">REMOVED ({removed.length})</small>
                {removed.map((ms) => (
                  <div className="diff-module-row removed" key={ms.module_id}>
                    <span>−</span>
                    <span>{moduleById.get(ms.module_id)?.name ?? shortId(ms.module_id)}</span>
                    <code>{moduleSnapshotKey(ms).slice(0, 10)}…</code>
                  </div>
                ))}
              </div>
            )}

            {reused.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-muted">REUSED ({reused.length})</small>
                {reused.map((ms) => {
                  const wasAffected = impact?.affected_module_ids.includes(ms.module_id);
                  return (
                    <div className="diff-module-row reused" key={ms.module_id}>
                      <Check className="h-3 w-3" />
                      <span>{moduleById.get(ms.module_id)?.name ?? shortId(ms.module_id)}</span>
                      <span className="diff-module-tag tone-muted">
                        {wasAffected ? "affected · reused" : "reused"}
                      </span>
                      <code>{moduleSnapshotKey(ms).slice(0, 10)}…</code>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* BOM diff */}
          <section className="diff-section">
            <h3>BOM Diff</h3>
            <div className="diff-summary-row">
              <span>总计 {solution.bom.length} 项</span>
              {bomDiff.added.length > 0 && (
                <span className="tone-good">+{bomDiff.added.length} 新增</span>
              )}
            </div>
            {bomDiff.added.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-good">ADDED</small>
                {bomDiff.added.map((item, i) => {
                  const label = "name" in item ? item.name : item.item;
                  const key = "line_id" in item ? item.line_id : `bom-${i}`;
                  return (
                    <div className="diff-item-row" key={key}>
                      <span>+</span>
                      <span>{label} × {item.quantity}</span>
                    </div>
                  );
                })}
              </div>
            )}
            {bomDiff.changed.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-live">CONTINUED</small>
                {bomDiff.changed.map((item, i) => {
                  const label = "name" in item ? item.name : item.item;
                  const key = "line_id" in item ? item.line_id : `bom-${i}`;
                  return (
                    <div className="diff-item-row" key={key}>
                      <RotateCw className="h-3 w-3" />
                      <span>{label} × {item.quantity}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* Implementation steps diff */}
          <section className="diff-section">
            <h3>实施步骤 Diff</h3>
            <div className="diff-summary-row">
              <span>总计 {solution.implementation_steps.length} 步</span>
              {stepDiff.added.length > 0 && (
                <span className="tone-good">+{stepDiff.added.length} 新增</span>
              )}
            </div>
            {stepDiff.added.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-good">NEW STEPS</small>
                {stepDiff.added.map((step, i) => {
                  const title = "title" in step ? step.title : step.step;
                  const instruction = "instruction" in step ? step.instruction : "";
                  const key = "step_id" in step ? step.step_id : `step-${i}`;
                  return (
                    <div className="diff-item-row" key={key}>
                      <span>+</span>
                      <div>
                        <strong>{title}</strong>
                        {instruction && <p>{instruction}</p>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            {stepDiff.changed.length > 0 && (
              <div className="diff-group">
                <small className="diff-group-label tone-live">CONTINUED</small>
                {stepDiff.changed.map((step, i) => {
                  const title = "title" in step ? step.title : step.step;
                  const key = "step_id" in step ? step.step_id : `step-${i}`;
                  return (
                    <div className="diff-item-row" key={key}>
                      <RotateCw className="h-3 w-3" />
                      <strong>{title}</strong>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* Stale evidence */}
          {staleEvidence.length > 0 && (
            <section className="diff-section diff-section--warn">
              <h3>
                <Clock className="h-4 w-4" />
                过期证据 ({staleEvidence.length})
              </h3>
              <div className="stale-evidence-list">
                {staleEvidence.map((eb) =>
                  eb ? (
                    <article className="stale-evidence-card" key={eb.id}>
                      <span className="status-tag tone-bad">过时</span>
                      <p>{eb.claim}</p>
                      <code>{eb.snapshot_hash.slice(0, 16)}…</code>
                    </article>
                  ) : null,
                )}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
