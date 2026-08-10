"use client";

import { useMemo, useRef, useState, useCallback } from "react";
import { toast } from "sonner";
import {
  Box,
  Boxes,
  Check,
  CircleAlert,
  GitBranch,
  Hammer,
  History,
  LayoutGrid,
  LockKeyhole,
  Network,
  PackageSearch,
  Play,
  RotateCcw,
  Save,
  Send,
  ShieldAlert,
  Sparkles,
  UnlockKeyhole,
  WandSparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { getClient } from "@/lib/api";
import { ModuleDependencyGraph } from "@/app/components/ModuleDependencyGraph";
import { ModulePixel } from "@/app/components/ModulePixel";
import { SchemaParameterForm } from "@/app/components/SchemaParameterForm";
import type {
  Candidate,
  Module,
  ModuleConfiguration,
  ProjectSnapshot,
  SelectionLock,
} from "@/app/types/types";

interface DraftWorkbenchProps {
  snapshot: ProjectSnapshot;
  onRefresh: () => Promise<unknown>;
  developerMode?: boolean;
}

interface Feedback {
  kind: "success" | "conflict" | "forbidden" | "error";
  title: string;
  message: string;
}

const KIND_LABEL: Record<string, string> = {
  adjustment: "调整",
  lock: "锁定",
  unlock: "解锁",
  snapshot_save: "保存检查点",
  snapshot_restore: "恢复检查点",
};

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 10)}…` : "未设置";
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function activeConfiguration(
  configurations: ModuleConfiguration[],
  moduleId: string
): ModuleConfiguration | undefined {
  return configurations.find(
    (configuration) =>
      configuration.module_id === moduleId && configuration.status === "active"
  );
}

function currentLock(
  locks: SelectionLock[],
  moduleId: string
): SelectionLock | undefined {
  return locks.find((lock) => lock.module_id === moduleId && lock.active);
}

function selectedCandidateId(
  configuration?: ModuleConfiguration
): string | undefined {
  const value = configuration?.options.selected_candidate_id;
  return typeof value === "string" ? value : undefined;
}

function stageTone(stage: string): string {
  const good = ["selected", "revised"];
  const live = ["researching", "comparing", "deciding", "verifying", "draft"];
  if (good.includes(stage)) return "tone-good";
  if (live.includes(stage)) return "tone-live";
  return "tone-muted";
}

function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    draft: "可调整",
    researching: "正在查找",
    comparing: "正在比较",
    deciding: "等待决定",
    verifying: "正在验证",
    selected: "已选择",
    revised: "已更新",
  };
  return labels[stage] ?? "进行中";
}

function describeApiError(error: unknown): Feedback {
  const payload = (error as { error?: { code?: string; message?: string } })
    ?.error;
  const code = payload?.code ?? "unknown";
  const message = payload?.message ?? "未知错误，请刷新后重试";
  if (code === "domain_conflict" && /lock|锁定|protected/i.test(message)) {
    return {
      kind: "forbidden",
      title: "该选择受用户锁保护",
      message: `${message}（前端仅作提示，服务端强制拒绝）`,
    };
  }
  if (code === "domain_conflict" || code === "idempotency_conflict") {
    return { kind: "conflict", title: "操作冲突", message };
  }
  if (code === "precondition_failed") {
    return {
      kind: "conflict",
      title: "草稿修订已过期",
      message: `${message}（快照已更新，界面已同步最新状态）`,
    };
  }
  if (code === "validation_error" || code === "not_found") {
    return { kind: "forbidden", title: "操作不被允许", message };
  }
  return { kind: "error", title: "操作失败", message };
}

function optionEntries(options: Record<string, unknown>): [string, unknown][] {
  return Object.entries(options).filter(
    ([key]) => key !== "selected_candidate_id" && key !== "custom_candidate"
  );
}

interface ModuleInspectorProps {
  module: Module;
  candidates: Candidate[];
  configuration?: ModuleConfiguration;
  revisions: ModuleConfiguration[];
  lock?: SelectionLock;
  projectId: string;
  revision: number;
  developerMode: boolean;
  onRefresh: () => Promise<unknown>;
  onReport: (feedback: Feedback) => void;
}

function ModuleInspector({
  module,
  candidates,
  configuration,
  revisions,
  lock,
  projectId,
  revision,
  developerMode,
  onRefresh,
  onReport,
}: ModuleInspectorProps) {
  const [busy, setBusy] = useState(false);
  const [paramKey, setParamKey] = useState("");
  const [paramValue, setParamValue] = useState("");
  const [localFeedback, setLocalFeedback] = useState<Feedback | null>(null);
  const revisionRef = useRef(revision);
  revisionRef.current = revision;
  const currentSelection = selectedCandidateId(configuration);
  const selectedCandidate = candidates.find(
    (candidate) => candidate.id === currentSelection
  );
  const editableOptions = useMemo(() => {
    const entries = optionEntries(configuration?.options ?? {});
    return Object.fromEntries(entries);
  }, [configuration]);

  const report = (feedback: Feedback) => {
    setLocalFeedback(feedback);
    onReport(feedback);
  };

  const applyCandidate = async (candidateId: string): Promise<void> => {
    setBusy(true);
    try {
      const result = await getClient().recordUserAdjustment(
        projectId,
        module.id,
        revisionRef.current,
        {
          kind: "select_candidate",
          target: { candidate_id: candidateId },
        }
      );
      revisionRef.current = result.data.project_revision;
      report({
        kind: "success",
        title: `已选型 ${module.name}`,
        message: "选择已持久化，进入待批处理状态。",
      });
      await onRefresh();
    } catch (error) {
      report(describeApiError(error));
    } finally {
      setBusy(false);
    }
  };

  const saveParameter = async (
    key: string,
    value: unknown
  ): Promise<boolean> => {
    setBusy(true);
    try {
      const result = await getClient().recordUserAdjustment(
        projectId,
        module.id,
        revisionRef.current,
        {
          kind: "set_parameter",
          target: { key, value },
        }
      );
      revisionRef.current = result.data.project_revision;
      report({
        kind: "success",
        title: `已更新参数 ${key}`,
        message: `${key} = ${String(value)} 已持久化。`,
      });
      await onRefresh();
      return true;
    } catch (error) {
      report(describeApiError(error));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const addParameter = async (): Promise<void> => {
    if (!paramKey.trim()) return;
    const ok = await saveParameter(paramKey.trim(), paramValue);
    if (ok) {
      setParamKey("");
      setParamValue("");
    }
  };

  const toggleLock = async (): Promise<void> => {
    setBusy(true);
    try {
      if (lock) {
        const result = await getClient().unlockSelectionLock(
          lock.id,
          revisionRef.current
        );
        revisionRef.current = result.data.project_revision;
        report({
          kind: "success",
          title: `已解除 ${module.name} 的用户锁`,
          message: "Agent 的后续提案可以重新评估该模块。",
        });
      } else {
        const result = await getClient().createSelectionLock(
          projectId,
          module.id,
          revisionRef.current,
          {
            ...(currentSelection ? { candidate_id: currentSelection } : {}),
            reason: currentSelection
              ? "用户确认保留当前候选路线"
              : "用户确认保留当前模块职责边界",
          }
        );
        revisionRef.current = result.data.project_revision;
        report({
          kind: "success",
          title: `已锁定 ${module.name}`,
          message: "Agent 不会静默替换该选择；如有风险仍可提出提案说明。",
        });
      }
      await onRefresh();
    } catch (error) {
      report(describeApiError(error));
    } finally {
      setBusy(false);
    }
  };

  const lockExplanation = lock
    ? lock.candidate_id === null
      ? "该模块被整体锁定，候选更换被服务端拒绝。"
      : lock.candidate_id === currentSelection
      ? "当前候选已被用户锁定；更换其他候选会被服务端拒绝。"
      : "该模块被锁定到其他候选；更换被服务端拒绝。"
    : null;

  return (
    <section
      aria-label={`${module.name} 模块详情与草稿调整`}
      className="crafting-inspector"
    >
      <header className="crafting-inspector-head">
        <div className="crafting-inspector-title">
          <ModulePixel
            seed={module.key}
            tone={stageTone(module.stage)}
            size={9}
          />
          <div>
            <code>{module.key}</code>
            <h4>{module.name}</h4>
          </div>
        </div>
        <span className={`status-tag ${stageTone(module.stage)}`}>
          {developerMode ? module.stage : stageLabel(module.stage)}
        </span>
      </header>

      <p className="crafting-inspector-copy">{module.responsibility}</p>

      {localFeedback ? <FeedbackChip feedback={localFeedback} /> : null}

      <div className="crafting-inspector-grid">
        <div className="crafting-panel">
          <div className="draft-section-label">当前选型</div>
          {selectedCandidate ? (
            <article className="crafting-current-selection">
              <strong>{selectedCandidate.name}</strong>
              <span>{selectedCandidate.description}</span>
              {developerMode ? (
                <small>候选草稿 r{configuration?.revision ?? 0}</small>
              ) : null}
            </article>
          ) : (
            <p className="crafting-empty">
              尚未选型；从下方候选路线中选择一项。
            </p>
          )}

          <div className="draft-section-label">
            候选路线（{candidates.length}）
          </div>
          <div className="crafting-candidates">
            {candidates.length ? (
              candidates.map((candidate) => {
                const blocked = Boolean(
                  lock &&
                    (lock.candidate_id === null ||
                      lock.candidate_id !== candidate.id)
                );
                const isSelected = candidate.id === currentSelection;
                return (
                  <button
                    aria-label={`选择 ${candidate.name}`}
                    className={
                      isSelected
                        ? "crafting-candidate is-selected"
                        : "crafting-candidate"
                    }
                    disabled={busy || blocked}
                    key={candidate.id}
                    onClick={() => void applyCandidate(candidate.id)}
                    type="button"
                  >
                    <span className="crafting-candidate-name">
                      <strong>{candidate.name}</strong>
                      {isSelected ? <Check className="h-3.5 w-3.5" /> : null}
                      {blocked ? (
                        <LockKeyhole
                          aria-label="该候选受锁保护"
                          className="h-3.5 w-3.5"
                        />
                      ) : null}
                    </span>
                    <span>{candidate.description}</span>
                    {candidate.risks.length ? (
                      <small className="crafting-risk-line">
                        风险：{candidate.risks.join("；")}
                      </small>
                    ) : null}
                  </button>
                );
              })
            ) : (
              <p className="crafting-empty">
                尚无可选候选；研究完成后会出现在这里。
              </p>
            )}
          </div>
          {lockExplanation ? (
            <p className="crafting-lock-note">
              <LockKeyhole className="h-3.5 w-3.5" />
              {lockExplanation}
            </p>
          ) : null}
        </div>

        <div className="crafting-panel">
          <div className="crafting-panel-head">
            <div className="draft-section-label">参数与选型选项</div>
            <Button
              disabled={busy}
              onClick={() => void toggleLock()}
              size="sm"
              variant={lock ? "secondary" : "outline"}
            >
              {lock ? (
                <UnlockKeyhole className="h-3.5 w-3.5" />
              ) : (
                <LockKeyhole className="h-3.5 w-3.5" />
              )}
              {lock ? "解除锁定" : "锁定此模块"}
            </Button>
          </div>

          {Object.keys(editableOptions).length ? (
            <SchemaParameterForm
              busy={busy}
              onReport={report}
              onSave={saveParameter}
              options={editableOptions}
            />
          ) : (
            <p className="crafting-empty">暂无参数；可在下方添加一个参数。</p>
          )}

          <div className="crafting-param-add">
            <input
              aria-label="参数名称"
              disabled={busy}
              onChange={(event) => setParamKey(event.target.value)}
              placeholder="参数，例如 capacity_mah"
              value={paramKey}
            />
            <input
              aria-label="参数值"
              disabled={busy}
              onChange={(event) => setParamValue(event.target.value)}
              placeholder="值"
              value={paramValue}
            />
            <Button
              disabled={busy || !paramKey.trim()}
              onClick={() => void addParameter()}
              size="sm"
              variant="outline"
            >
              <Send className="h-3.5 w-3.5" />
              记录
            </Button>
          </div>

          <div className="draft-section-label">
            {developerMode ? "模块调整历史（revisions）" : "我做过的调整"}
          </div>
          <ol className="crafting-revisions">
            {revisions
              .slice()
              .reverse()
              .map((item) => (
                <li
                  className={item.status === "active" ? "is-active" : ""}
                  key={item.id}
                >
                  <span>{developerMode ? `r${item.revision}` : "已保存"}</span>
                  <time>{formatTime(item.created_at)}</time>
                  <code>
                    {selectedCandidateId(item)
                      ? "已选型"
                      : optionEntries(item.options).length
                      ? optionEntries(item.options)
                          .map(([key, value]) => `${key}=${String(value)}`)
                          .join(", ")
                      : "职责保持"}
                  </code>
                </li>
              ))}
          </ol>
        </div>
      </div>

      <footer className="crafting-inspector-foot">
        <span>
          {developerMode
            ? `草稿 r${configuration?.revision ?? 0}`
            : "当前调整会自动保留"}
        </span>
        {developerMode ? (
          <code>{shortHash(configuration?.base_snapshot_hash)}</code>
        ) : null}
      </footer>
    </section>
  );
}

function FeedbackChip({ feedback }: { feedback: Feedback }) {
  const icons = {
    success: (
      <Check
        className="h-4 w-4"
        aria-hidden="true"
      />
    ),
    conflict: (
      <CircleAlert
        className="h-4 w-4"
        aria-hidden="true"
      />
    ),
    forbidden: (
      <LockKeyhole
        className="h-4 w-4"
        aria-hidden="true"
      />
    ),
    error: (
      <CircleAlert
        className="h-4 w-4"
        aria-hidden="true"
      />
    ),
  } as const;
  return (
    <div
      aria-live="polite"
      className={`feedback-chip is-${feedback.kind}`}
      role="status"
    >
      {icons[feedback.kind]}
      <div>
        <strong>{feedback.title}</strong>
        <span>{feedback.message}</span>
      </div>
    </div>
  );
}

export function DraftWorkbench({
  snapshot,
  onRefresh,
  developerMode = false,
}: DraftWorkbenchProps) {
  const [busyPlanId, setBusyPlanId] = useState<string | null>(null);
  const [busyReshapeId, setBusyReshapeId] = useState<string | null>(null);
  const [busySnapshotId, setBusySnapshotId] = useState<string | null>(null);
  const [openModuleId, setOpenModuleId] = useState<string | null>(null);
  const [workbenchView, setWorkbenchView] = useState<"table" | "graph">(
    "table"
  );
  const [snapshotLabel, setSnapshotLabel] = useState("");
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  // Draft edge state — local-only, not sent to the server
  const [draftEdges, setDraftEdges] = useState<Set<string>>(
    () => new Set()
  );
  const [removedEdges, setRemovedEdges] = useState<Set<string>>(
    () => new Set()
  );

  const handleDraftEdgeAdd = useCallback(
    (source: string, target: string) => {
      const key = `${source}->${target}`;
      setDraftEdges((prev) => {
        const next = new Set(prev);
        next.add(key);
        return next;
      });
      setRemovedEdges((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    },
    []
  );

  const handleDraftEdgeRemove = useCallback(
    (edgeKey: string) => {
      setDraftEdges((prev) => {
        const next = new Set(prev);
        next.delete(edgeKey);
        return next;
      });
      setRemovedEdges((prev) => {
        const next = new Set(prev);
        next.add(edgeKey);
        return next;
      });
    },
    []
  );

  const configurations = useMemo(
    () => snapshot.module_configurations ?? [],
    [snapshot.module_configurations]
  );
  const locks = useMemo(
    () => snapshot.selection_locks ?? [],
    [snapshot.selection_locks]
  );
  const batches = snapshot.adjustment_batches ?? [];
  const blueprints = snapshot.blueprints ?? [];
  const executionPlans = snapshot.execution_plans ?? [];
  const reshapeProposals = snapshot.reshape_proposals ?? [];
  const solutionSnapshots = snapshot.solution_snapshots ?? [];
  const draftHistory = snapshot.draft_history ?? [];
  const activeBlueprint = blueprints.find(
    (blueprint) => blueprint.id === snapshot.project.active_blueprint_id
  );
  const openBatch = batches.find((batch) => batch.status === "open");

  const candidateByModule = useMemo(() => {
    const grouped = new Map<string, Candidate[]>();
    for (const candidate of snapshot.candidates) {
      grouped.set(candidate.module_id, [
        ...(grouped.get(candidate.module_id) ?? []),
        candidate,
      ]);
    }
    return grouped;
  }, [snapshot.candidates]);

  const candidateById = useMemo(() => {
    return new Map(
      snapshot.candidates.map((candidate) => [candidate.id, candidate])
    );
  }, [snapshot.candidates]);

  const revisionsByModule = useMemo(() => {
    const grouped = new Map<string, ModuleConfiguration[]>();
    for (const configuration of configurations) {
      const list = grouped.get(configuration.module_id) ?? [];
      list.push(configuration);
      grouped.set(configuration.module_id, list);
    }
    for (const list of grouped.values())
      list.sort((a, b) => a.revision - b.revision);
    return grouped;
  }, [configurations]);

  const openModule = openModuleId
    ? snapshot.modules.find((module) => module.id === openModuleId)
    : undefined;
  const activeConfig = openModule
    ? activeConfiguration(configurations, openModule.id)
    : undefined;

  const lockedModuleIds = useMemo(
    () =>
      new Set(
        locks.filter((lock) => lock.active).map((lock) => lock.module_id)
      ),
    [locks]
  );

  const selectedNames = useMemo(() => {
    const names = new Map<string, string>();
    for (const module of snapshot.modules) {
      const configuration = activeConfiguration(configurations, module.id);
      const selected = selectedCandidateId(configuration);
      names.set(
        module.id,
        selected ? candidateById.get(selected)?.name ?? "已选型" : "未选型"
      );
    }
    return names;
  }, [snapshot.modules, configurations, candidateById]);

  const report = (next: Feedback) => setFeedback(next);

  const flush = async () => {
    if (!openBatch) return;
    try {
      await getClient().flushAdjustmentBatch(
        openBatch.id,
        snapshot.project.revision
      );
      report({
        kind: "success",
        title: "本轮调整已结束",
        message: "后续局部分析会遵守已批准的执行范围。",
      });
      toast.success("已结束本轮调整收集；后续分析会遵守已批准的执行范围。");
      await onRefresh();
    } catch (error) {
      const next = describeApiError(error);
      report(next);
      toast.error(next.message);
    }
  };

  const resolvePlan = async (
    planId: string,
    decision: "approved" | "rejected",
    scopeHash: string
  ) => {
    setBusyPlanId(planId);
    try {
      await getClient().resolveExecutionPlan(
        planId,
        snapshot.project.revision,
        decision,
        scopeHash
      );
      report({
        kind: "success",
        title: decision === "approved" ? "已批准执行边界" : "已拒绝执行提案",
        message:
          decision === "approved"
            ? "Agent 只能在该边界内分解与执行；这不是对骨架的直接修改。"
            : "拒绝后不创建任何自动执行。",
      });
      await onRefresh();
    } catch (error) {
      report(describeApiError(error));
    } finally {
      setBusyPlanId(null);
    }
  };

  const proposeDefaultPlan = async () => {
    try {
      await getClient().proposeDefaultResearchExecutionPlan(
        snapshot.project.id,
        snapshot.project.revision
      );
      report({
        kind: "success",
        title: "已提交给 AI 生成研究执行提案",
        message:
          "新 ExecutionPlanProposal 已生成，需你批准后才会进入执行边界。",
      });
      await onRefresh();
    } catch (error) {
      report(describeApiError(error));
    }
  };

  const resolveReshape = async (
    proposalId: string,
    decision: "applied" | "rejected"
  ) => {
    setBusyReshapeId(proposalId);
    try {
      await getClient().resolveProjectReshape(
        proposalId,
        snapshot.project.revision,
        decision
      );
      report({
        kind: "success",
        title:
          decision === "applied" ? "已应用新的工程骨架" : "已拒绝结构重塑提案",
        message:
          decision === "applied"
            ? "旧骨架 supersede，草稿记录仍可追溯。"
            : "骨架保持不变。",
      });
      await onRefresh();
    } catch (error) {
      report(describeApiError(error));
    } finally {
      setBusyReshapeId(null);
    }
  };

  const saveSnapshot = async () => {
    if (!snapshotLabel.trim()) return;
    setBusySnapshotId("save");
    try {
      await getClient().saveSolutionSnapshot(
        snapshot.project.id,
        snapshot.project.revision,
        snapshotLabel.trim()
      );
      setSnapshotLabel("");
      report({
        kind: "success",
        title: "已保存草稿检查点",
        message: "它不会覆盖已冻结方案。",
      });
      await onRefresh();
    } catch (error) {
      report(describeApiError(error));
    } finally {
      setBusySnapshotId(null);
    }
  };

  const restoreSnapshot = async (snapshotId: string, label: string) => {
    setBusySnapshotId(snapshotId);
    try {
      await getClient().restoreSolutionSnapshot(
        snapshotId,
        snapshot.project.revision
      );
      report({
        kind: "success",
        title: `已从“${label}”恢复草稿`,
        message: "恢复会新增配置 revision，原记录不被覆盖。",
      });
      await onRefresh();
    } catch (error) {
      report(describeApiError(error));
    } finally {
      setBusySnapshotId(null);
    }
  };

  if (!snapshot.modules.length) return null;

  return (
    <section
      aria-label="V3 用户可控工程工作台"
      className="crafting-workbench"
    >
      <header className="crafting-head">
        <div className="crafting-head-title">
          <small>你的 DIY 工程工作台</small>
          <h2>
            <Hammer className="h-5 w-5" />
            先选模块，再慢慢做出你的作品
          </h2>
          <p>
            点右侧模块查看方案与参数；你的调整会先保存，AI
            只能提出建议，不能替你改决定。
          </p>
        </div>
        {openBatch ? (
          <Button
            onClick={() => void flush()}
            size="sm"
            variant="outline"
          >
            <Play className="h-3.5 w-3.5" />
            结束本轮调整 · {openBatch.adjustment_ids.length}
          </Button>
        ) : (
          <span className="draft-batch-status">没有待合并的调整</span>
        )}
      </header>

      {feedback ? <FeedbackChip feedback={feedback} /> : null}

      <aside
        className="crafting-quickstart"
        aria-label="第一次使用提示"
      >
        <strong>第一次来这里？</strong>
        <ol>
          <li>
            <b>1</b> 点一个模块，看看它负责什么。
          </li>
          <li>
            <b>2</b> 有候选方案时，比较后选择你想要的路线。
          </li>
          <li>
            <b>3</b> 切到“模块关系”，了解它会影响哪些部分。
          </li>
        </ol>
      </aside>

      {/* 像素台面：作品在左、可调整的模块格在右；依赖关系按需切换。 */}
      <div className="crafting-table">
        <div
          className="crafting-view-switch"
          role="group"
          aria-label="工作台视图"
        >
          <span>查看方式</span>
          <Button
            aria-pressed={workbenchView === "table"}
            className={workbenchView === "table" ? "is-active" : ""}
            onClick={() => setWorkbenchView("table")}
            size="sm"
            type="button"
            variant="outline"
          >
            <LayoutGrid className="h-3.5 w-3.5" />
            工作台
          </Button>
          <Button
            aria-pressed={workbenchView === "graph"}
            className={workbenchView === "graph" ? "is-active" : ""}
            onClick={() => setWorkbenchView("graph")}
            size="sm"
            type="button"
            variant="outline"
          >
            <Network className="h-3.5 w-3.5" />
            模块关系
          </Button>
        </div>

        {workbenchView === "table" ? (
          <div className="crafting-table-layout">
            <aside className="crafting-output">
              <div
                className="crafting-output-icon"
                aria-hidden="true"
              >
                <Boxes className="h-5 w-5" />
              </div>
              <div className="crafting-output-copy">
                <small>
                  {developerMode
                    ? "PROJECT OUTPUT SLOT · 你在制作"
                    : "你正在制作"}
                </small>
                <strong>{snapshot.project.name}</strong>
                <span>{snapshot.project.goal}</span>
              </div>
              <div className="crafting-output-meta">
                {developerMode ? (
                  <>
                    <span className="status-tag tone-good">
                      r{snapshot.project.revision}
                    </span>
                    <code>
                      骨架{" "}
                      {activeBlueprint
                        ? `v${activeBlueprint.version}`
                        : "待确认"}
                    </code>
                  </>
                ) : (
                  <span className="status-tag tone-good">作品骨架已保留</span>
                )}
              </div>
            </aside>

            <div
              className="crafting-grid"
              aria-label="可选择的工程模块"
            >
              {snapshot.modules.map((module) => {
                const selected = selectedNames.get(module.id) ?? "未选型";
                const locked = lockedModuleIds.has(module.id);
                const isOpen = openModuleId === module.id;
                return (
                  <div
                    className="crafting-block-wrap"
                    key={module.id}
                  >
                    <button
                      aria-pressed={isOpen}
                      className={
                        isOpen ? "crafting-block is-open" : "crafting-block"
                      }
                      onClick={() => setOpenModuleId(module.id)}
                      type="button"
                    >
                      <span className="crafting-block-pixel">
                        <ModulePixel
                          seed={module.key}
                          tone={stageTone(module.stage)}
                          size={7}
                        />
                        {locked ? (
                          <LockKeyhole className="crafting-lock-badge" />
                        ) : null}
                      </span>
                      <span className="crafting-block-name">
                        <code>{module.key}</code>
                        <strong>{module.name}</strong>
                      </span>
                      <span className="crafting-block-selection">
                        <small>当前选型</small>
                        <span>{selected}</span>
                      </span>
                    </button>
                    {module.dependency_ids.length ? (
                      <span className="crafting-depends">
                        依赖 {module.dependency_ids.length} 个模块
                      </span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        ) : (
          <ModuleDependencyGraph
            key={`${snapshot.modules
              .map((module) => module.id)
              .sort()
              .join(",")}|${snapshot.modules
              .flatMap((module) =>
                module.dependency_ids.map((depId) => `${depId}->${module.id}`)
              )
              .sort()
              .join(",")}`}
            draftEdges={draftEdges}
            lockedModuleIds={lockedModuleIds}
            modules={snapshot.modules}
            onDraftEdgeAdd={handleDraftEdgeAdd}
            onDraftEdgeRemove={handleDraftEdgeRemove}
            onSelectModule={setOpenModuleId}
            removedEdges={removedEdges}
            selectedModuleId={openModuleId}
            selectedNames={selectedNames}
          />
        )}
      </div>

      {/* 模块详情与草稿调整 */}
      {openModule ? (
        <ModuleInspector
          candidates={candidateByModule.get(openModule.id) ?? []}
          configuration={activeConfig}
          lock={currentLock(locks, openModule.id)}
          module={openModule}
          onRefresh={onRefresh}
          onReport={report}
          projectId={snapshot.project.id}
          revision={snapshot.project.revision}
          revisions={revisionsByModule.get(openModule.id) ?? []}
          developerMode={developerMode}
        />
      ) : null}

      {/* 商品输出与采购边界 */}
      <div className="crafting-products">
        <div className="crafting-section-title">
          <PackageSearch
            className="h-4 w-4"
            aria-hidden="true"
          />
          <div>
            <small>
              {developerMode ? "OUTPUT · PRODUCTS" : "下一步：找商品"}
            </small>
            <strong>商品推荐</strong>
          </div>
          <span className="crafting-placeholder-tag">需要时再搜索</span>
        </div>
        <p className="crafting-products-note">
          选好模块后，这里会汇总可参考的商品。价格和库存以你主动搜索时的结果为准；Aidison
          只给推荐，不会替你下单或付款。
        </p>
        <div className="crafting-products-grid">
          {snapshot.modules.map((module) => {
            const selected = selectedCandidateId(
              activeConfiguration(configurations, module.id)
            );
            const candidate = selected
              ? candidateById.get(selected)
              : undefined;
            return (
              <article
                className="product-placeholder-card"
                key={module.id}
              >
                <span
                  className="product-placeholder-icon"
                  aria-hidden="true"
                >
                  <Box className="h-4 w-4" />
                </span>
                <div>
                  <code>{module.key}</code>
                  <strong>{candidate?.name ?? "未选型"}</strong>
                  <small>
                    {candidate
                      ? candidate.description
                      : "完成选型后这里会显示对应的选型投影。"}
                  </small>
                </div>
                <span className="product-placeholder-price">¥ —</span>
              </article>
            );
          })}
        </div>
      </div>

      {/* 结构性变更边界：前端不静默改写骨架 */}
      <div className="crafting-boundary">
        <div className="crafting-boundary-note">
          <ShieldAlert
            className="h-4 w-4"
            aria-hidden="true"
          />
          <div>
            <strong>想改变作品结构？先看 AI 的建议，再由你决定</strong>
            <p>
              新增、调整或删除模块会影响整个作品，所以会经过“AI 建议 → 你确认 →
              应用”。平时调参数和选型不会偷偷改变你已确定的作品结构。
            </p>
          </div>
        </div>

        <div className="execution-plan-stack">
          <div className="crafting-section-title">
            <WandSparkles
              className="h-4 w-4"
              aria-hidden="true"
            />
            <div>
              <small>
                {developerMode ? "EXECUTION PLAN PROPOSALS" : "需要 AI 帮忙时"}
              </small>
              <strong>让 AI 先写一份查找与修改计划</strong>
            </div>
            <Button
              disabled={busyPlanId !== null}
              onClick={() => void proposeDefaultPlan()}
              size="sm"
              variant="outline"
            >
              <Sparkles className="h-3.5 w-3.5" />让 AI 先写计划
            </Button>
          </div>
          {executionPlans.length ? (
            executionPlans.map((plan) => (
              <article
                className="execution-plan-card"
                key={plan.id}
              >
                <div>
                  <strong>{plan.objective}</strong>
                  <p>{plan.work_summary.join(" · ")}</p>
                  <small>
                    {developerMode
                      ? `并发 ≤ ${
                          plan.max_concurrency
                        } · 预算 ≤ ${plan.max_token_budget.toLocaleString()} · 结果需确认 ${
                          plan.requires_result_approval ? "是" : "否"
                        } · scope ${shortHash(plan.scope_hash)}`
                      : "确认后，AI 只会在这份计划的范围内工作；结果仍由你决定是否采用。"}
                  </small>
                </div>
                {plan.status === "proposed" ? (
                  <div className="execution-plan-actions">
                    <Button
                      disabled={busyPlanId !== null}
                      onClick={() =>
                        void resolvePlan(plan.id, "approved", plan.scope_hash)
                      }
                      size="sm"
                    >
                      批准范围
                    </Button>
                    <Button
                      disabled={busyPlanId !== null}
                      onClick={() =>
                        void resolvePlan(plan.id, "rejected", plan.scope_hash)
                      }
                      size="sm"
                      variant="outline"
                    >
                      拒绝
                    </Button>
                  </div>
                ) : (
                  <span className="draft-batch-status">
                    {plan.status === "approved"
                      ? "已批准：Agent 只能在该边界内执行"
                      : plan.status}
                  </span>
                )}
              </article>
            ))
          ) : (
            <p className="crafting-empty">
              还没有计划。需要查资料、修改模块或验证方案时，先让 AI
              把它准备做什么写清楚，再由你确认。
            </p>
          )}
        </div>

        {reshapeProposals.length ? (
          <div className="execution-plan-stack">
            <div className="crafting-section-title">
              <GitBranch
                className="h-4 w-4"
                aria-hidden="true"
              />
              <div>
                <small>PROJECT RESHAPE PROPOSALS</small>
                <strong>模块骨架重塑提案</strong>
              </div>
            </div>
            {reshapeProposals.map((proposal) => (
              <article
                className="execution-plan-card"
                key={proposal.id}
              >
                <div>
                  <strong>{proposal.summary}</strong>
                  <p>
                    保留 {proposal.unchanged_module_ids.length} 个模块 · 调整{" "}
                    {proposal.affected_module_ids.length} 个模块 · 新增{" "}
                    {proposal.new_modules.map((item) => item.name).join("、") ||
                      "无"}
                  </p>
                  <small>
                    应用后会生成新的模块骨架；现有骨架和草稿记录仍可追溯。
                  </small>
                </div>
                {proposal.status === "proposed" ? (
                  <div className="execution-plan-actions">
                    <Button
                      disabled={busyReshapeId !== null}
                      onClick={() =>
                        void resolveReshape(proposal.id, "applied")
                      }
                      size="sm"
                    >
                      应用骨架
                    </Button>
                    <Button
                      disabled={busyReshapeId !== null}
                      onClick={() =>
                        void resolveReshape(proposal.id, "rejected")
                      }
                      size="sm"
                      variant="outline"
                    >
                      拒绝
                    </Button>
                  </div>
                ) : (
                  <span className="draft-batch-status">{proposal.status}</span>
                )}
              </article>
            ))}
          </div>
        ) : null}
      </div>

      {/* 调整历史时间线 */}
      <div className="crafting-history">
        <div className="crafting-section-title">
          <History
            className="h-4 w-4"
            aria-hidden="true"
          />
          <div>
            <small>
              {developerMode ? "DRAFT HISTORY · ADJUSTMENT LOG" : "做过什么"}
            </small>
            <strong>调整记录</strong>
          </div>
        </div>
        {draftHistory.length ? (
          <ol className="crafting-history-list">
            {draftHistory
              .slice()
              .reverse()
              .map((entry) => (
                <li key={entry.id}>
                  <span
                    className={`history-dot is-${entry.kind}`}
                    aria-hidden="true"
                  />
                  <span className="history-kind">
                    {KIND_LABEL[entry.kind] ?? entry.kind}
                  </span>
                  <time>{formatTime(entry.created_at)}</time>
                  {developerMode ? <code>#{entry.sequence}</code> : null}
                  {developerMode ? (
                    <code className="history-hash">
                      {shortHash(entry.draft_state_hash)}
                    </code>
                  ) : null}
                </li>
              ))}
          </ol>
        ) : (
          <p className="crafting-empty">还没有调整记录。</p>
        )}
      </div>

      {/* 草稿检查点 */}
      <div className="draft-snapshot-bar">
        <div>
          <div className="draft-section-label">
            {developerMode ? "DRAFT CHECKPOINTS" : "保存一个可回来的版本"}
          </div>
          <p>
            给当前选型和参数起个名字；之后可以一键回到这里，之前的记录不会丢失。
          </p>
        </div>
        <div className="draft-snapshot-save">
          <input
            aria-label="检查点名称"
            disabled={busySnapshotId !== null}
            onChange={(event) => setSnapshotLabel(event.target.value)}
            placeholder="例如：低功耗基线"
            value={snapshotLabel}
          />
          <Button
            disabled={busySnapshotId !== null || !snapshotLabel.trim()}
            onClick={() => void saveSnapshot()}
            size="sm"
            variant="outline"
          >
            <Save className="h-3.5 w-3.5" />
            保存
          </Button>
        </div>
        {solutionSnapshots.length ? (
          <div className="draft-snapshot-list">
            {solutionSnapshots
              .slice(-4)
              .reverse()
              .map((item) => (
                <button
                  disabled={busySnapshotId !== null}
                  key={item.id}
                  onClick={() => void restoreSnapshot(item.id, item.label)}
                  type="button"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  {item.label}
                </button>
              ))}
          </div>
        ) : null}
      </div>
    </section>
  );
}
