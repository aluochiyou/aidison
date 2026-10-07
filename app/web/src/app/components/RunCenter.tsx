"use client";

import { useState } from "react";
import {
  Activity,
  CircleAlert,
  Clock3,
  Coins,
  ListChecks,
  PauseCircle,
  PlayCircle,
  Send,
  XCircle,
} from "lucide-react";
import { getClient } from "@/lib/api";
import type { ProjectSnapshot, ResearchQualitySummary } from "@/app/types/types";

interface RunCenterProps {
  snapshot: ProjectSnapshot;
  onRefresh: () => void;
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "时间未知"
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

const QUALITY_OUTCOME_LABELS: Record<ResearchQualitySummary["outcome"], string> = {
  waiting: "等待正在进行的研究任务",
  needs_more_evidence: "正在补充缺失证据",
  needs_verification: "需要交叉核验",
  complete: "关键研究问题已覆盖",
  partial: "以部分结果交付",
  blocked: "研究暂时无法继续",
  unavailable: "研究质量记录暂不可读取",
};

const COVERAGE_STATUS_LABELS: Record<string, string> = {
  answered: "已覆盖",
  unknown: "结论未知",
  missing: "尚未覆盖",
  insufficient_sources: "来源数量不足",
  conflicted: "证据结论冲突",
};

function reasonLabel(code: string): string {
  const labels: Record<string, string> = {
    all_must_coverage_satisfied: "所有关键问题均已获得足够证据支持",
    must_coverage_has_bounded_gap: "关键问题仍有可补充的证据缺口",
    must_coverage_requires_independent_verification: "关键问题需要独立来源交叉核验",
    must_conflict_requires_verification: "关键问题出现相互冲突的证据，需要核验",
    must_coverage_still_in_flight: "关键问题仍在等待已启动的任务返回",
    must_coverage_gap_cannot_expand: "关键问题存在缺口，但当前运行不能继续扩展",
    independent_verification_unavailable: "当前运行无法执行独立核验",
    must_conflict_without_verifier: "关键问题存在冲突，但当前运行没有可用核验路径",
    research_quality_projection_unavailable: "研究质量记录或其冻结依据无法读取",
  };
  return labels[code] ?? code;
}

function evidenceReasonLabel(code: string): string {
  const labels: Record<string, string> = {
    coverage_key_not_assigned_to_task: "使用了本任务未授权的覆盖项",
    source_key_not_collected: "声明的来源不在本次已保存资料中",
    quote_not_unique_or_not_exact: "引文无法在已保存来源中唯一且精确定位",
    task_module_scope_unavailable: "任务无法确定对应的模块范围",
  };
  if (code.startsWith("evidence_admission:")) {
    return `证据准入拒绝：${code.slice("evidence_admission:".length)}`;
  }
  return labels[code] ?? code;
}

function sourceGapReasonLabel(code: string): string {
  const labels: Record<string, string> = {
    no_trusted_research_sources: "本轮没有取得可保存、可核验的来源",
    tavily_network_failure: "来源服务出现瞬时网络故障",
    tavily_provider_unavailable: "来源服务暂时不可用",
  };
  return labels[code] ?? code;
}

function ResearchQuality({
  quality,
  moduleNames,
}: {
  quality: ResearchQualitySummary;
  moduleNames: Map<string, string>;
}) {
  const issues = quality.coverage.filter(
    (item) =>
      item.status !== "answered" ||
      (item.requires_independent_verification && !item.independently_verified)
  );
  return (
    <details className="research-quality-summary">
      <summary>
        <CircleAlert className="h-4 w-4" />
        研究质量：{QUALITY_OUTCOME_LABELS[quality.outcome]}
      </summary>
      <p>{quality.reason_codes.map(reasonLabel).join("；")}</p>
      {quality.context?.manifest_count ? (
        <small>
          本轮上下文预算：已编译 {quality.context.manifest_count} 份任务上下文，选入
          {" "}{quality.context.selected_source_count} 份来源片段，估算输入
          {" "}{quality.context.input_token_estimate} tokens。
          {quality.context.omitted_source_count
            ? `另有 ${quality.context.omitted_source_count} 份来源片段因上下文预算未选入`
            : "没有来源片段因预算省略"}
          {Object.keys(quality.context.omission_reason_counts).length
            ? `（${Object.entries(quality.context.omission_reason_counts)
                .map(([reason, count]) => `${reason} ×${count}`)
                .join("、")}）`
            : ""}
          {quality.context.unreadable_manifest_count
            ? `；${quality.context.unreadable_manifest_count} 份上下文记录不可读取`
            : ""}
          。
        </small>
      ) : null}
      {issues.length ? (
        <ul className="quality-issue-list">
          {issues.map((item) => (
            <li key={item.coverage_key}>
              <strong>{COVERAGE_STATUS_LABELS[item.status] ?? item.status}</strong>
              {item.module_ids.length ? (
                <small>
                  模块：{item.module_ids.map((id) => moduleNames.get(id) ?? id).join("、")}
                </small>
              ) : (
                <small>模块：项目整体</small>
              )}
              <span>{item.question}</span>
              {item.status === "insufficient_sources" ? (
                <small>
                  已获得 {item.observed_source_count} 份不同来源文档，至少需要 {item.min_distinct_sources} 份。
                  {item.min_distinct_origins > 1
                    ? `其中覆盖 ${item.observed_origin_count} 个不同来源站点，至少需要 ${item.min_distinct_origins} 个。`
                    : ""}
                </small>
              ) : null}
              {item.missing_source_kinds.length ? (
                <small>缺少：{item.missing_source_kinds.join("、")}</small>
              ) : null}
              {item.observed_source_kinds.length ? (
                <small>已验证来源：{item.observed_source_kinds.join("、")}</small>
              ) : null}
              {item.collected_source_count ? (
                <small>
                  本轮收集到 {item.collected_source_count} 份候选来源
                  {item.collection_profiles.length
                    ? `（${item.collection_profiles.join("、")} 策略）`
                    : ""}
                  ，其中已准入的才会计入覆盖。
                </small>
              ) : null}
              {item.unavailable_source_reason_codes.length ? (
                <small>
                  本轮未取得来源：
                  {item.unavailable_source_reason_codes.map(sourceGapReasonLabel).join("、")}
                  。系统会在预算与补题上限允许时换角度继续检索。
                </small>
              ) : null}
              {item.requires_independent_verification && !item.independently_verified ? (
                <small>仍需独立来源交叉核验。</small>
              ) : null}
              {item.rejected_claim_count ? (
                <small>
                  模型提出的 {item.rejected_claim_count} 条论断未进入证据链：
                  {item.rejected_reason_codes.map(evidenceReasonLabel).join("、")}
                </small>
              ) : null}
            </li>
          ))}
        </ul>
      ) : quality.outcome === "complete" ? (
        <small>所有关键问题已有已准入的证据支持。</small>
      ) : null}
    </details>
  );
}

/** User-facing run projection; technical attempts remain in Developer Diagnostics. */
export function RunCenter({ snapshot, onRefresh }: RunCenterProps) {
  const [cancellingRunId, setCancellingRunId] = useState<string | null>(null);
  const [controllingRunId, setControllingRunId] = useState<string | null>(null);
  const [steeringByRunId, setSteeringByRunId] = useState<Record<string, string>>({});
  const [resolvingDecisionId, setResolvingDecisionId] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const work = snapshot.workspace?.work ?? [];
  const agentRuns = snapshot.agent_runs ?? [];
  const plans = (snapshot.execution_plans ?? []).filter(
    (plan) => plan.status === "approved" || plan.status === "proposed"
  );
  const budgets = snapshot.agent_run_budget_accounts ?? [];
  const moduleNames = new Map((snapshot.modules ?? []).map((module) => [module.id, module.name]));
  const pendingRunDecisions = (snapshot.agent_run_decisions ?? []).filter(
    (decision) => decision.status === "pending"
  );

  const cancel = async (runId: string) => {
    setCancellingRunId(runId);
    setCommandError(null);
    try {
      await getClient().cancelAgentRun(snapshot.project.id, runId, snapshot.project.revision);
      onRefresh();
    } catch (error: unknown) {
      setCommandError(
        error && typeof error === "object" && "error" in error
          ? (error as { error: { message: string } }).error.message
          : "无法提交取消请求。"
      );
    } finally {
      setCancellingRunId(null);
    }
  };

  const requestPause = async (run: (typeof agentRuns)[number]) => {
    setControllingRunId(run.id);
    setCommandError(null);
    try {
      await getClient().createAgentRunControlRequest(
        snapshot.project.id,
        run.id,
        snapshot.project.revision,
        { kind: "pause", basis_hash: run.basis_hash }
      );
      onRefresh();
    } catch (error: unknown) {
      setCommandError(
        error && typeof error === "object" && "error" in error
          ? (error as { error: { message: string } }).error.message
          : "无法请求暂停运行。"
      );
    } finally {
      setControllingRunId(null);
    }
  };

  const resume = async (run: (typeof agentRuns)[number]) => {
    setControllingRunId(run.id);
    setCommandError(null);
    try {
      await getClient().resumeAgentRun(snapshot.project.id, run.id, snapshot.project.revision);
      onRefresh();
    } catch (error: unknown) {
      setCommandError(
        error && typeof error === "object" && "error" in error
          ? (error as { error: { message: string } }).error.message
          : "无法继续运行。"
      );
    } finally {
      setControllingRunId(null);
    }
  };

  const submitSteering = async (run: (typeof agentRuns)[number]) => {
    const instruction = steeringByRunId[run.id]?.trim();
    if (!instruction) {
      setCommandError("请输入对后续研究的具体引导。");
      return;
    }
    setControllingRunId(run.id);
    setCommandError(null);
    try {
      await getClient().createAgentRunControlRequest(
        snapshot.project.id,
        run.id,
        snapshot.project.revision,
        {
          kind: "runtime_steering",
          basis_hash: run.basis_hash,
          instruction,
        }
      );
      setSteeringByRunId((current) => ({ ...current, [run.id]: "" }));
      onRefresh();
    } catch (error: unknown) {
      setCommandError(
        error && typeof error === "object" && "error" in error
          ? (error as { error: { message: string } }).error.message
          : "无法提交运行引导。"
      );
    } finally {
      setControllingRunId(null);
    }
  };

  const resolveDecision = async (
    decision: (typeof pendingRunDecisions)[number],
    answer: "approved" | "rejected"
  ) => {
    setResolvingDecisionId(decision.id);
    setCommandError(null);
    try {
      await getClient().resolveAgentRunDecision(decision.id, snapshot.project.revision, {
        decision: answer,
        basis_hash: decision.basis_hash,
      });
      onRefresh();
    } catch (error: unknown) {
      setCommandError(
        error && typeof error === "object" && "error" in error
          ? (error as { error: { message: string } }).error.message
          : "无法提交研究提案的确认结果。"
      );
    } finally {
      setResolvingDecisionId(null);
    }
  };

  return (
    <section className="research-evidence-view" aria-label="运行中心">
      <header className="research-evidence-header">
        <div>
          <small>RUN CENTER</small>
          <h2>运行中心</h2>
          <p>查看 AI 为什么在工作、当前进度与已批准的执行边界。</p>
        </div>
        <span className="evidence-count-badge">
          <Activity className="h-4 w-4" /> {work.length} 项工作
        </span>
      </header>

      {work.length === 0 ? (
        <div className="decision-empty">
          <Activity className="h-6 w-6" />
          <p>当前没有正在执行或可审计的工作。批准执行计划后，运行会在这里出现。</p>
        </div>
      ) : (
        <div className="decision-options-list">
          {work.toReversed().map((item) => (
            <article className="decision-option-card" key={item.run_id}>
              <div className="decision-option-header">
                <strong>{item.user_label}</strong>
                <span className="status-tag">{item.state}</span>
              </div>
              <div className="decision-option-meta">
                <span>
                  <ListChecks className="h-3.5 w-3.5" /> {item.completed_units} / {item.total_units || "?"} 单元证据完成
                </span>
                <span>
                  <Clock3 className="h-3.5 w-3.5" /> 更新于 {formatTime(item.updated_at)}
                </span>
              </div>
              {item.failed_units > 0 ? <p>有 {item.failed_units} 个单元需要后续检查。</p> : null}
              {item.partial_units > 0 ? <p>有 {item.partial_units} 个单元仅部分覆盖，仍在等待补题或核验。</p> : null}
              {item.latest_error ? <p className="impact-meta">当前状态：{item.latest_error}</p> : null}
            </article>
          ))}
        </div>
      )}

      <section className="research-section">
        <h3>LangGraph 运行</h3>
        {commandError ? <p className="impact-meta">{commandError}</p> : null}
        {agentRuns.length ? (
          <div className="decision-options-list">
            {agentRuns.toReversed().map((run) => {
              const canCancel =
                !run.cancel_requested &&
                !["succeeded", "failed", "cancelled"].includes(run.status);
              const pausePending = run.control_requests.some(
                (request) => request.kind === "pause" && request.status === "requested"
              );
              const pauseAcknowledged = run.control_requests.some(
                (request) => request.kind === "pause" && request.status === "acknowledged"
              );
              const canPause =
                ["queued", "running"].includes(run.status) && !run.cancel_requested && !pausePending;
              const canResume = run.status === "waiting" && pauseAcknowledged;
              const canSteer = ["queued", "running"].includes(run.status) && !run.cancel_requested;
              const isControlling = controllingRunId === run.id;
              return (
                <article className="decision-option-card" key={run.id}>
                  <div className="decision-option-header">
                    <strong>{run.kind === "research" ? "研究运行" : run.kind}</strong>
                    <span className="status-tag">{run.status}</span>
                  </div>
                  <div className="decision-option-meta">
                    <span>基于项目修订：{run.basis_project_revision}</span>
                    <span>更新于 {formatTime(run.updated_at)}</span>
                  </div>
                  {run.research_progress ? (
                    <p className="impact-meta">
                      初始策略任务 {run.research_progress.initial_task_count} 个；已准入结果 {run.research_progress.admitted_task_count} 个，其中证据完成 {run.research_progress.succeeded_task_count} 个、部分覆盖 {run.research_progress.partial_task_count} 个
                      {run.research_progress.additional_task_count
                        ? `（含 ${run.research_progress.additional_task_count} 个补题或核验任务）`
                        : ""}
                      。
                    </p>
                  ) : null}
                  {run.latest_error ? (
                    <p className="impact-meta">停止原因：{run.latest_error}</p>
                  ) : null}
                  {run.cancel_requested ? <p>取消已请求，运行将在下一个安全点停止。</p> : null}
                  {run.kind === "research" && run.research_quality ? (
                  <ResearchQuality quality={run.research_quality} moduleNames={moduleNames} />
                  ) : null}
                  {run.control_requests.some((request) => request.status === "requested") ? (
                    <p>有待处理的运行控制请求。</p>
                  ) : null}
                  {canCancel ? (
                    <button
                      type="button"
                      className="decision-option-action"
                      disabled={cancellingRunId === run.id}
                      onClick={() => void cancel(run.id)}
                    >
                      <XCircle className="h-4 w-4" />
                      {cancellingRunId === run.id ? "正在提交…" : "取消运行"}
                    </button>
                  ) : null}
                  {canPause ? (
                    <button
                      type="button"
                      className="decision-option-action"
                      disabled={isControlling}
                      onClick={() => void requestPause(run)}
                    >
                      <PauseCircle className="h-4 w-4" />
                      {isControlling ? "正在提交…" : "暂停运行"}
                    </button>
                  ) : null}
                  {canResume ? (
                    <button
                      type="button"
                      className="decision-option-action"
                      disabled={isControlling}
                      onClick={() => void resume(run)}
                    >
                      <PlayCircle className="h-4 w-4" />
                      {isControlling ? "正在继续…" : "继续运行"}
                    </button>
                  ) : null}
                  {canSteer ? (
                    <div className="run-steering-control">
                      <label htmlFor={`run-steering-${run.id}`}>引导后续研究</label>
                      <div>
                        <input
                          id={`run-steering-${run.id}`}
                          value={steeringByRunId[run.id] ?? ""}
                          maxLength={1000}
                          placeholder="例如：优先比较官方规格与独立实测，明确温度限制。"
                          onChange={(event) =>
                            setSteeringByRunId((current) => ({
                              ...current,
                              [run.id]: event.target.value,
                            }))
                          }
                        />
                        <button
                          type="button"
                          className="decision-option-action"
                          disabled={isControlling || !(steeringByRunId[run.id]?.trim())}
                          onClick={() => void submitSteering(run)}
                        >
                          <Send className="h-4 w-4" />
                          提交引导
                        </button>
                      </div>
                      <small>只影响尚未派发的研究任务，不修改已批准的项目事实或研究范围。</small>
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        ) : (
          <p className="empty-copy">当前没有新运行时创建的 LangGraph 运行。</p>
        )}
      </section>

      <section className="research-section" aria-label="待确认的研究提案">
        <h3>待确认的研究提案</h3>
        {pendingRunDecisions.length ? (
          <div className="decision-options-list">
            {pendingRunDecisions.map((decision) => (
              <article className="decision-option-card run-decision-card" key={decision.id}>
                <div className="decision-option-header">
                  <strong>研究结果已就绪，等待你的判断</strong>
                  <span className="status-tag">待确认</span>
                </div>
                {decision.proposal_preview ? (
                  <>
                    <p>{decision.proposal_preview.question}</p>
                    <div className="run-decision-module-list">
                      {decision.proposal_preview.module_summaries.map((item) => (
                        <article key={`${decision.id}-${item.module_id}`}>
                          <small>
                            {snapshot.modules.find((module) => module.id === item.module_id)
                              ?.name ?? "模块"}
                            ：研究结论 · {item.evidence_count} 条已准入证据
                          </small>
                          <strong>{item.recommended_option}</strong>
                          <p>{item.summary}</p>
                          {item.alternatives.length ? (
                            <small>备选：{item.alternatives.join("；")}</small>
                          ) : null}
                        </article>
                      ))}
                    </div>
                    {decision.proposal_preview.unsupported_module_ids.length ? (
                      <p className="impact-meta">
                        本轮暂不能采纳：
                        {decision.proposal_preview.unsupported_module_ids
                          .map(
                            (moduleId) =>
                              snapshot.modules.find((module) => module.id === moduleId)
                                ?.name ?? "未命名模块"
                          )
                          .join("、")}
                        没有已准入证据支持的推荐。请查看研究质量缺口或重新规划。
                      </p>
                    ) : null}
                  </>
                ) : (
                  <p>提案正文暂不可读取；请先查看 Artifact 审计记录，确认后再操作。</p>
                )}
                <p className="impact-meta">
                  {decision.proposal_preview && !decision.proposal_preview.adoption_allowed
                    ? "当前缺少证据支持，不能采纳本轮推荐；拒绝只关闭本轮研究，不会修改项目事实。"
                    : "采纳会把本轮推荐方案写入项目的下一步决策；拒绝只关闭本轮研究，不会修改项目事实。"}
                </p>
                <div className="run-decision-actions">
                  <button
                    type="button"
                    className="decision-option-action"
                    disabled={
                      resolvingDecisionId !== null ||
                      !decision.proposal_preview?.adoption_allowed
                    }
                    onClick={() => void resolveDecision(decision, "approved")}
                  >
                    {resolvingDecisionId === decision.id
                      ? "正在提交…"
                      : decision.proposal_preview?.adoption_allowed
                        ? "采纳推荐方案"
                        : "证据不足，无法采纳"}
                  </button>
                  <button
                    type="button"
                    className="decision-option-action is-secondary"
                    disabled={resolvingDecisionId !== null}
                    onClick={() => void resolveDecision(decision, "rejected")}
                  >
                    拒绝本轮研究
                  </button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="empty-copy">当前没有等待你确认的研究提案。</p>
        )}
      </section>

      <section className="research-section">
        <h3>已批准的执行边界</h3>
        {plans.length ? (
          plans.map((plan) => (
            <article className="decision-option-card" key={plan.id}>
              <strong>{plan.objective}</strong>
              <p>{plan.work_summary.join("；") || "没有额外工作说明。"}</p>
              <div className="decision-option-meta">
                <span>并发上限：{plan.max_concurrency}</span>
                <span>Token 上限：{plan.max_token_budget.toLocaleString()}</span>
                <span>最长时长：{plan.max_duration_seconds.toLocaleString()} 秒</span>
                <span>{plan.requires_result_approval ? "结果需要你的确认" : "结果可自动完成"}</span>
              </div>
            </article>
          ))
        ) : (
          <p className="empty-copy">尚未有已批准或待审核的执行计划。</p>
        )}
      </section>

      <section className="research-section">
        <h3>本轮预算</h3>
        {budgets.length ? (
          budgets.map((budget) => (
            <article className="decision-option-card" key={budget.id}>
              <div className="decision-option-header">
                <strong><Coins className="h-4 w-4" /> 研究预算</strong>
                <span className="status-tag">{budget.state}</span>
              </div>
              <div className="decision-option-meta">
                <span>Token：{budget.token_consumed.toLocaleString()} 已消耗，{budget.token_reserved.toLocaleString()} 已预留 / {budget.token_cap.toLocaleString()}</span>
                <span>工具调用：{budget.tool_calls_consumed} 已消耗，{budget.tool_calls_reserved} 已预留 / {budget.tool_call_cap}</span>
              </div>
            </article>
          ))
        ) : (
          <p className="empty-copy">尚未为当前运行建立预算账户。</p>
        )}
      </section>
    </section>
  );
}
