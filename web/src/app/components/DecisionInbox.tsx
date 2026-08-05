"use client";

import { useState } from "react";
import { GitBranch, ShieldCheck, AlertTriangle } from "lucide-react";
import type { DecisionRequest, ProjectSnapshot } from "@/app/types/types";
import { getClient } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

function normalizedOption(option: DecisionRequest["options"][number]): {
  option_id: string;
  label: string;
  summary: string;
  candidate_ids: string[];
  evidence_binding_ids: string[];
  risks: string[];
  legacy_unbound: boolean;
} {
  if (typeof option !== "string") return option;
  return {
    option_id: option,
    label: option,
    summary: "历史选项未绑定候选与证据，仅供只读审计。",
    candidate_ids: [],
    evidence_binding_ids: [],
    risks: [],
    legacy_unbound: true,
  };
}

interface DecisionInboxProps {
  snapshot: ProjectSnapshot;
  onResolved: () => void;
}

export function DecisionInbox({ snapshot, onResolved }: DecisionInboxProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const pendingDecisions = snapshot.decisions.filter((d) => d.status === "pending");
  const resolvedDecisions = snapshot.decisions.filter((d) => d.status !== "pending");

  const resolve = async (decision: DecisionRequest, optionId: string) => {
    if (busy) return;
    setBusy(optionId);
    try {
      await getClient().resolveDecision(
        decision.id,
        snapshot.project.revision,
        optionId,
        decision.basis_hash,
      );
      toast.success("决策已提交");
      onResolved();
    } catch (e) {
      const msg =
        e && typeof e === "object" && "error" in e
          ? (e as { error: { message: string } }).error.message
          : "决策提交失败";
      toast.error(msg);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="decision-inbox" role="region" aria-label="决策收件箱">
      <div className="decision-inbox-header">
        <div>
          <small>DECISION INBOX</small>
          <h2>决策收件箱</h2>
        </div>
        <span className="decision-count-badge">
          {pendingDecisions.length} 待决策
        </span>
      </div>

      {pendingDecisions.length === 0 && resolvedDecisions.length === 0 && (
        <div className="decision-empty">
          <GitBranch className="h-6 w-6" />
          <p>当前没有决策请求。决策在研究和评估阶段后由服务端创建。</p>
        </div>
      )}

      {/* Pending decisions */}
      {pendingDecisions.map((decision) => (
        <article
          className={`decision-card ${expandedId === decision.id ? "is-expanded" : ""}`}
          key={decision.id}
        >
          <header>
            <div className="decision-card-header">
              <ShieldCheck className="h-5 w-5" />
              <div>
                <strong>{decision.question}</strong>
                <code>basis: {decision.basis_hash.slice(0, 16)}…</code>
              </div>
            </div>
            <button
              className="decision-expand-toggle"
              onClick={() =>
                setExpandedId(expandedId === decision.id ? null : decision.id)
              }
              aria-label={expandedId === decision.id ? "收起" : "展开"}
            >
              {expandedId === decision.id ? "收起" : "展开选项"}
            </button>
          </header>

          <p className="decision-affected">
            影响模块:{" "}
            {decision.affected_module_ids
              .map((mid) => {
                const m = snapshot.modules.find((mod) => mod.id === mid);
                return m?.name ?? mid.slice(0, 8);
              })
              .join("、") || "无"}
          </p>

          <div className="decision-options-list">
            {decision.options.map((raw) => {
              const option = normalizedOption(raw);
              return (
                <div className="decision-option-card" key={option.option_id}>
                  <div className="decision-option-header">
                    <strong>{option.label}</strong>
                    {option.legacy_unbound && (
                      <span className="status-tag tone-bad">未绑定</span>
                    )}
                    <code>{option.option_id}</code>
                  </div>
                  <p>{option.summary}</p>
                  <div className="decision-option-meta">
                    <span>候选: {option.candidate_ids.length}</span>
                    <span>证据: {option.evidence_binding_ids.length}</span>
                    {option.risks.length > 0 && (
                      <span className="decision-option-risks">
                        风险: {option.risks.join("、")}
                      </span>
                    )}
                  </div>
                  <Button
                    size="sm"
                    disabled={busy === option.option_id || option.legacy_unbound}
                    onClick={() => resolve(decision, option.option_id)}
                  >
                    {busy === option.option_id
                      ? "提交中…"
                      : option.legacy_unbound
                        ? "审计选项不可选"
                        : "选择此方案"}
                  </Button>
                </div>
              );
            })}
          </div>
        </article>
      ))}

      {/* Resolved decisions — collapsed audit trail */}
      {resolvedDecisions.length > 0 && (
        <section className="decision-resolved-section">
          <h3>已决策历史</h3>
          {resolvedDecisions.map((decision) => {
            const selected = decision.options
              .map((o) => (typeof o === "string" ? null : o))
              .find(
                (o) =>
                  o && o.option_id === decision.selected_option_id,
              );
            return (
              <article className="decision-resolved-card" key={decision.id}>
                <div>
                  <span
                    className={`status-tag ${decision.status === "approved" ? "tone-good" : "tone-bad"}`}
                  >
                    {decision.status}
                  </span>
                  <strong>{decision.question}</strong>
                </div>
                <p>
                  已选: {selected?.label ?? decision.selected_option_id ?? "未选择"}
                </p>
                <code>
                  {decision.resolved_at
                    ? new Date(decision.resolved_at).toLocaleString("zh-CN")
                    : "未决议"}
                </code>
              </article>
            );
          })}
        </section>
      )}

      {/* Server-owned guard */}
      <div className="decision-server-guard">
        <AlertTriangle className="h-4 w-4" />
        <small>
          决策选项由服务端根据 bound evidence 生成。浏览器不可编造 canonical solution/patch。
        </small>
      </div>
    </div>
  );
}
