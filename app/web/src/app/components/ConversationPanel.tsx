"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import {
  ArrowRight,
  Bot,
  Check,
  CornerDownLeft,
  FileCheck2,
  Loader2,
  Plus,
  Send,
  Sparkles,
  X,
} from "lucide-react";
import { getClient } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { dedupeClarifications } from "@/app/utils/clarifications";
import type {
  ConversationActionProposal,
  ConversationClarification,
  ConversationTurn,
} from "@/app/types/types";

interface ConversationPanelProps {
  projectId: string;
  projectRevision: number;
  hasApprovedRequirements: boolean;
  onSnapshotChanged: () => Promise<unknown> | void;
  /** 将首次需求草案交给可编辑的需求确认表单，不执行 proposal。 */
  onUseRequirementsDraft?: (payload: Record<string, unknown>) => void;
  /** 直接去工作台打开“确认需求”表单（无 AI 草案时的手动兜底入口）。 */
  onOpenRequirementsForm?: () => void;
}

const INITIAL_REQUIREMENTS_DRAFT_MARKER =
  "_aidison_initial_requirements_draft";

/** 需求草案的轻量预览：目标 + 模块名，帮助用户决定是否带入表单。 */
interface RequirementsDraftPreviewData {
  goal: string | null;
  modules: { key: string; name: string }[];
}

function requirementsDraftPreview(
  payload: Record<string, unknown> | undefined
): RequirementsDraftPreviewData | null {
  if (!payload) return null;
  const goal = typeof payload.goal === "string" ? payload.goal : null;
  const rawModules = Array.isArray(payload.modules) ? payload.modules : [];
  const modules = rawModules
    .filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object"
    )
    .map((item) => ({
      key: typeof item.key === "string" ? item.key : "",
      name: typeof item.name === "string" ? item.name : "",
    }))
    .filter((item) => item.key || item.name);
  if (!goal && !modules.length) return null;
  return { goal, modules };
}

const PROPOSAL_KIND_LABELS: Record<string, string> = {
  rewrite_requirements: "需求草案",
  start_research: "开始研究",
  add_module: "新增模块",
  reshape_project: "结构调整",
  change_selection: "选型调整",
  change_spend_budget: "预算调整",
  restore_solution_snapshot: "恢复快照",
};

/** 接受某类提案后，下一步发生在哪张“待确认”卡片上。 */
const ACCEPT_GUIDANCE: Record<string, string> = {
  start_research:
    "已生成执行计划提案；请在待确认卡片中批准后，AI 才会开始查找资料。",
  rewrite_requirements: "已生成需求变更提案；请在待确认卡片中应用后才会生效。",
  add_module: "已生成结构草案；请在待确认卡片中应用。",
  reshape_project: "已生成结构草案；请在待确认卡片中应用。",
  change_selection: "已记录选型调整；影响分析会作为待确认卡片出现。",
  change_spend_budget: "已生成预算提案；请在待确认卡片中批准。",
  restore_solution_snapshot: "已恢复所选快照的模块配置。",
};

function researchPlanPreview(payload: Record<string, unknown> | undefined) {
  if (!payload) return [];
  const entries: string[] = [];
  // The governed accept path freezes an omitted depth as `deep`; disclose the
  // same default here instead of merely echoing the untrusted model payload.
  const researchDepth = payload.research_depth ?? "deep";
  if (researchDepth === "focused") {
    entries.push("聚焦研究");
  } else if (researchDepth === "standard") {
    entries.push("标准研究");
  } else if (researchDepth === "deep") {
    entries.push(
      payload.requires_independent_verification === false
        ? "深度研究（已关闭独立核验）"
        : "深度研究（默认独立核验）"
    );
  }
  if (typeof payload.max_concurrency === "number") {
    entries.push(`最多 ${payload.max_concurrency} 个并行研究任务`);
  }
  if (typeof payload.max_token_budget === "number") {
    entries.push(`Token 预算 ${payload.max_token_budget.toLocaleString()}`);
  }
  if (typeof payload.max_duration_seconds === "number") {
    entries.push(`最长时长 ${payload.max_duration_seconds.toLocaleString()} 秒`);
  }
  if (payload.requires_independent_verification === true && researchDepth !== "deep") {
    entries.push("要求独立核验");
  }
  const sourceStrategy = payload.source_strategy;
  if (sourceStrategy === "official") {
    entries.push("优先官方资料");
  } else if (sourceStrategy === "independent") {
    entries.push("优先独立资料");
  } else if (sourceStrategy === "mixed") {
    entries.push("官方与独立来源混合");
  } else if (sourceStrategy === "primary") {
    entries.push("优先一手资料");
  }
  return entries;
}

function isInitialRequirementsDraft(proposal: ConversationActionProposal): boolean {
  return (
    proposal.kind === "rewrite_requirements" &&
    proposal.proposed_payload?.[INITIAL_REQUIREMENTS_DRAFT_MARKER] === true
  );
}

function friendlyError(error: unknown): string {
  const status = (error as { status?: number } | null)?.status;
  const detail = (error as { detail?: unknown } | null)?.detail;
  const apiMessage = (error as { error?: { message?: unknown } } | null)?.error
    ?.message;
  const message =
    typeof detail === "string" && detail
      ? detail
      : typeof apiMessage === "string" && apiMessage
      ? apiMessage
      : error instanceof Error
      ? error.message
      : "";
  if (status === 409) {
    return `操作被拒绝（409）：${message || "请求已过期或已被处理"}。请刷新后重试。`;
  }
  if (status === 412) {
    return `项目版本已更新（412）：${message || "revision 不匹配"}。请刷新后重试。`;
  }
  return message || "操作失败，请稍后重试。";
}

/** 首次需求草案的卡片预览：目标 + 模块名，避免用户盲目确认。 */
function RequirementsDraftPreviewCard({
  payload,
}: {
  payload: Record<string, unknown> | undefined;
}) {
  const preview = requirementsDraftPreview(payload);
  if (!preview) return null;
  return (
    <div className="conversation-draft-preview">
      <small>
        <Sparkles className="h-3 w-3" />
        AI 建议的草案预览
      </small>
      {preview.goal ? <strong>{preview.goal}</strong> : null}
      {preview.modules.length ? (
        <ul>
          {preview.modules.map((module) => (
            <li key={module.key || module.name}>
              <code>{module.key}</code>
              {module.name}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function ConversationPanel({
  projectId,
  projectRevision,
  hasApprovedRequirements,
  onSnapshotChanged,
  onUseRequirementsDraft,
  onOpenRequirementsForm,
}: ConversationPanelProps) {
  const { data, error, isLoading, mutate } = useSWR(
    ["project-snapshot", projectId],
    () => getClient().getSnapshot(projectId),
    { keepPreviousData: true }
  );

  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [clarifyInputs, setClarifyInputs] = useState<Record<string, string>>(
    {}
  );
  const [actionError, setActionError] = useState<string | null>(null);
  // Turns returned by resolveConversationClarification that may not yet have
  // landed in the snapshot. Merged into the visible turns and deduplicated by
  // id so a later snapshot revalidate never shows a duplicate.
  const [resolvedTurns, setResolvedTurns] = useState<ConversationTurn[]>([]);
  // Questions the user already answered in this session. Guards against the
  // backend re-asking the same clarification after a free-form message.
  const [resolvedQuestionTexts, setResolvedQuestionTexts] = useState<string[]>(
    []
  );
  const turnsRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    await Promise.allSettled([onSnapshotChanged?.(), mutate()]);
  }, [onSnapshotChanged, mutate]);

  const conversation = data?.conversation;
  const activeSession = conversation?.active_session ?? null;
  const snapshotTurns = (conversation?.recent_turns ?? []).filter(
    (turn) =>
      !activeSession || !turn.session_id || turn.session_id === activeSession.id
  );
  const turns = (() => {
    const merged = new Map<string, ConversationTurn>();
    for (const turn of snapshotTurns) merged.set(turn.id, turn);
    for (const turn of resolvedTurns) {
      if (
        !activeSession ||
        !turn.session_id ||
        turn.session_id === activeSession.id
      ) {
        merged.set(turn.id, turn);
      }
    }
    return Array.from(merged.values());
  })();

  // 对话只展示一个“当前待答问题”，其余排队；同一问题只出现一次。
  const clarifications = useMemo(
    () =>
      dedupeClarifications(
        conversation?.open_clarifications ?? [],
        resolvedQuestionTexts
      ),
    [conversation, resolvedQuestionTexts]
  );
  const activeClarification = clarifications[0];

  // 聊天列表可滚动，并在消息/问题更新后滚动到底部。
  useEffect(() => {
    const node = turnsRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns.length, activeClarification?.id]);

  const startSession = async () => {
    setCreating(true);
    setActionError(null);
    try {
      await getClient().newConversationSession(projectId);
      await refresh();
    } catch (caught) {
      setActionError(friendlyError(caught));
    } finally {
      setCreating(false);
    }
  };

  const sendMessage = async () => {
    const content = message.trim();
    if (!content || busy) return;
    const sessionId = activeSession?.id;
    if (!sessionId) {
      setActionError("还没有活动对话，请先创建一个会话。");
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      await getClient().postConversationMessage(projectId, content, sessionId);
      setMessage("");
      await refresh();
    } catch (caught) {
      setActionError(friendlyError(caught));
    } finally {
      setBusy(false);
    }
  };

  const resolveClarification = async (
    clarification: ConversationClarification
  ) => {
    const response = (clarifyInputs[clarification.id] ?? "").trim();
    if (!response || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const result = await getClient().resolveConversationClarification(
        projectId,
        clarification.id,
        response
      );
      const payload = result.data;
      // 200 不保证信封完整：缺 user_turn 视为失败，给出可操作提示，不伪造成功。
      if (!payload || !payload.user_turn) {
        setActionError(
          "服务器返回了无法识别的应答（缺少确认结果）。请刷新页面后重试；若问题仍存在，请直接去工作台手动填写需求。"
        );
        return;
      }
      const { user_turn: userTurn, assistant_turn: assistantTurn } = payload;
      setClarifyInputs((current) => {
        const next = { ...current };
        delete next[clarification.id];
        return next;
      });
      setResolvedQuestionTexts((current) =>
        current.includes(clarification.question)
          ? current
          : [...current, clarification.question]
      );
      setResolvedTurns((current) => {
        const next = [...current];
        if (!next.some((turn) => turn.id === userTurn.id)) {
          next.push(userTurn);
        }
        if (
          assistantTurn &&
          !next.some((turn) => turn.id === assistantTurn.id)
        ) {
          next.push(assistantTurn);
        }
        return next;
      });
      toast.success("已收到你的回答。");
      await refresh();
    } catch (caught) {
      setActionError(friendlyError(caught));
    } finally {
      setBusy(false);
    }
  };

  const acceptProposal = async (proposal: ConversationActionProposal) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await getClient().acceptConversationActionProposal(
        projectId,
        proposal.id,
        projectRevision
      );
      const guidance = proposal.kind
        ? ACCEPT_GUIDANCE[proposal.kind]
        : undefined;
      if (guidance) {
        toast.success(guidance);
      } else {
        toast.success("提案已接受，项目事实已按建议更新。");
      }
      await refresh();
    } catch (caught) {
      setActionError(friendlyError(caught));
      toast.error(friendlyError(caught));
    } finally {
      setBusy(false);
    }
  };

  const rejectProposal = async (proposal: ConversationActionProposal) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await getClient().rejectConversationActionProposal(
        projectId,
        proposal.id,
        projectRevision
      );
      toast.success("已拒绝这条建议，不会写入任何项目事实。");
      await refresh();
    } catch (caught) {
      setActionError(friendlyError(caught));
      toast.error(friendlyError(caught));
    } finally {
      setBusy(false);
    }
  };

  if (isLoading && !data) {
    return (
      <section
        className="conversation-panel"
        aria-label="与 AI 对话"
      >
        <div className="console-loading">正在加载对话…</div>
      </section>
    );
  }

  if (error || !data) {
    return (
      <section
        className="conversation-panel"
        aria-label="与 AI 对话"
      >
        <div className="console-loading error-state">
          <Bot className="h-5 w-5" />
          <p>
            无法读取对话：
            {friendlyError(error)}
          </p>
          <Button onClick={() => void mutate()}>重试</Button>
        </div>
      </section>
    );
  }

  const openProposals = (conversation?.open_action_proposals ?? []).filter(
    (proposal) =>
      proposal.status === "proposed" &&
      // Once the user has confirmed the initial requirements form, the raw
      // initial draft remains in the audit trail but is no longer an action.
      !(hasApprovedRequirements && isInitialRequirementsDraft(proposal))
  );

  return (
    <section
      className="conversation-panel"
      aria-label="与 AI 自由对话"
    >
      <div className="panel-heading">
        <div>
          <small>FREE CONVERSATION</small>
          <h2>与 AI 对话</h2>
        </div>
        <Bot className="h-4 w-4" />
      </div>

      {actionError ? (
        <p
          className="conversation-error"
          role="alert"
        >
          {actionError}
        </p>
      ) : null}

      {!activeSession ? (
        <div className="action-block">
          <div className="action-title">
            <Bot className="h-5 w-5" />
            <div>
              <small>开始对话</small>
              <h2>还没有活动对话</h2>
            </div>
          </div>
          <p>
            AI
            会基于项目事实与你自由对话，澄清和行动建议也会出现在这里。
          </p>
          <Button
            disabled={creating}
            onClick={() => void startSession()}
          >
            {creating ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Plus className="h-4 w-4" />
            )}
            {creating ? "正在创建…" : "新建会话"}
          </Button>
        </div>
      ) : (
        <div className="conversation-body">
          <div
            className="conversation-turns"
            ref={turnsRef}
          >
            {turns.length ? (
              turns.map((turn) => (
                <article
                  className={`turn turn-${turn.role}`}
                  key={turn.id}
                >
                  <span>{turn.role === "user" ? "你" : "AI"}</span>
                  <p>{turn.content}</p>
                </article>
              ))
            ) : (
              <p className="empty-copy">还没有消息，先和 AI 打个招呼吧。</p>
            )}
            {activeClarification ? (
              <article
                className="turn turn-assistant turn-clarification"
                key={activeClarification.id}
              >
                <span>AI</span>
                <p>{activeClarification.question}</p>
                <Textarea
                  rows={2}
                  value={clarifyInputs[activeClarification.id] ?? ""}
                  onChange={(event) =>
                    setClarifyInputs((current) => ({
                      ...current,
                      [activeClarification.id]: event.target.value,
                    }))
                  }
                  placeholder="写下你的回答…"
                />
                <Button
                  disabled={
                    busy ||
                    !(clarifyInputs[activeClarification.id] ?? "").trim()
                  }
                  onClick={() => void resolveClarification(activeClarification)}
                >
                  <CornerDownLeft className="h-4 w-4" />
                  提交回答
                </Button>
              </article>
            ) : null}
          </div>

          {clarifications.length > 1 ? (
            <p className="conversation-more-questions" role="status">
              还有 {clarifications.length - 1}{" "}
              个问题待确认，回答完上面的问题后会依次出现。
            </p>
          ) : null}

          <div className="conversation-composer">
            <Textarea
              rows={2}
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void sendMessage();
                }
              }}
              placeholder="告诉 AI 你在这个项目上的想法…"
            />
            <Button
              disabled={busy || !message.trim()}
              onClick={() => void sendMessage()}
            >
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Send className="h-4 w-4" />
              )}
              {busy ? "发送中…" : "发送"}
            </Button>
          </div>
        </div>
      )}

      {activeSession &&
      !hasApprovedRequirements &&
      !clarifications.length &&
      !openProposals.length &&
      onOpenRequirementsForm ? (
        <div className="conversation-hint">
          <p>
            还没有可确认的需求草案。可以继续告诉 AI
            你的想法，它会整理成一份草案；也可以直接去工作台手动填写需求单。
          </p>
          <Button
            size="sm"
            variant="outline"
            onClick={onOpenRequirementsForm}
          >
            <ArrowRight className="h-4 w-4" />
            去工作台填写需求
          </Button>
        </div>
      ) : null}

      {openProposals.length ? (
        <div className="conversation-queue">
          <div className="panel-heading">
            <div>
              <small>ACTION PROPOSALS</small>
              <h2>AI 建议的行动</h2>
            </div>
            <span>{openProposals.length}</span>
          </div>
          {openProposals.map((proposal) => (
            <div className="action-block" key={proposal.id}>
              <div className="action-title">
                <Check className="h-5 w-5" />
                <div>
                  <small>
                    {proposal.kind
                      ? (PROPOSAL_KIND_LABELS[proposal.kind] ?? "行动提案")
                      : "行动提案"}
                  </small>
                  <h2>{proposal.summary ?? proposal.title ?? "未命名提案"}</h2>
                </div>
              </div>
              {proposal.explanation ? <p>{proposal.explanation}</p> : null}
              {isInitialRequirementsDraft(proposal) ? (
                <RequirementsDraftPreviewCard
                  payload={proposal.proposed_payload}
                />
              ) : null}
              {proposal.kind === "start_research" &&
              researchPlanPreview(proposal.proposed_payload).length ? (
                <p className="run-callout">
                  计划边界：{researchPlanPreview(proposal.proposed_payload).join("；")}
                </p>
              ) : null}
              <div className="proposal-actions">
                {isInitialRequirementsDraft(proposal) &&
                !hasApprovedRequirements ? (
                  onUseRequirementsDraft && proposal.proposed_payload ? (
                    <Button
                      disabled={busy}
                      onClick={() =>
                        onUseRequirementsDraft(proposal.proposed_payload!)
                      }
                    >
                      <FileCheck2 className="h-4 w-4" />
                      使用这份需求草案
                    </Button>
                  ) : (
                    <span className="run-callout">
                      草案需在工作台的需求单里确认后生效。
                    </span>
                  )
                ) : (
                  <Button
                    disabled={busy}
                    onClick={() => void acceptProposal(proposal)}
                  >
                    <Check className="h-4 w-4" />
                    接受提案
                  </Button>
                )}
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => void rejectProposal(proposal)}
                >
                  <X className="h-4 w-4" />
                  拒绝提案
                </Button>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
