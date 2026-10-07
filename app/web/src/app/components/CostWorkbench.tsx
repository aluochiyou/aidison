"use client";

import { BarChart3, CircleAlert, Coins } from "lucide-react";
import type { AgentRunBudgetOperation, ProjectSnapshot } from "@/app/types/types";

interface CostWorkbenchProps {
  snapshot: ProjectSnapshot;
}

interface CostGroup {
  key: string;
  provider: string;
  target: string;
  operationCount: number;
  tokenCount: number;
  toolCallCount: number;
  unsettledCount: number;
}

function groupOperations(operations: AgentRunBudgetOperation[]): CostGroup[] {
  const groups = new Map<string, CostGroup>();
  for (const operation of operations) {
    const key = `${operation.provider}:${operation.target}`;
    const current = groups.get(key) ?? {
      key,
      provider: operation.provider,
      target: operation.target,
      operationCount: 0,
      tokenCount: 0,
      toolCallCount: 0,
      unsettledCount: 0,
    };
    current.operationCount += 1;
    current.tokenCount += operation.consumed_tokens;
    current.toolCallCount += operation.consumed_tool_calls;
    if (operation.state !== "settled") current.unsettledCount += 1;
    groups.set(key, current);
  }
  return [...groups.values()].toSorted((left, right) => right.tokenCount - left.tokenCount);
}

/** Read-only cost projection. It never estimates a price from tokens or changes budget state. */
export function CostWorkbench({ snapshot }: CostWorkbenchProps) {
  const operations = snapshot.agent_run_budget_operations ?? [];
  const groups = groupOperations(operations);
  const totalTokens = operations.reduce((sum, item) => sum + item.consumed_tokens, 0);
  const totalToolCalls = operations.reduce((sum, item) => sum + item.consumed_tool_calls, 0);
  const unsettled = operations.filter((item) => item.state !== "settled").length;

  return (
    <section className="research-evidence-view" aria-label="成本工作台">
      <header className="research-evidence-header">
        <div>
          <small>COST WORKBENCH</small>
          <h2>成本工作台</h2>
          <p>按已记录的物理调用归因，不将 token 伪装成现金价格。</p>
        </div>
        <span className="evidence-count-badge"><Coins className="h-4 w-4" /> {operations.length} 次调用</span>
      </header>

      <div className="decision-option-meta">
        <span><BarChart3 className="h-4 w-4" /> 已消耗 Token：{totalTokens.toLocaleString()}</span>
        <span>已消耗工具调用：{totalToolCalls}</span>
        {unsettled > 0 ? <span><CircleAlert className="h-4 w-4" /> {unsettled} 笔仍待结算</span> : null}
      </div>

      {groups.length ? (
        <section className="research-section">
          <h3>Provider / 模型或工具归因</h3>
          <div className="decision-options-list">
            {groups.map((group) => (
              <article className="decision-option-card" key={group.key}>
                <div className="decision-option-header">
                  <strong>{group.provider} · {group.target}</strong>
                  <span className="status-tag">{group.operationCount} 次</span>
                </div>
                <div className="decision-option-meta">
                  <span>Token：{group.tokenCount.toLocaleString()}</span>
                  <span>工具：{group.toolCallCount}</span>
                  {group.unsettledCount ? <span>待结算：{group.unsettledCount}</span> : null}
                </div>
              </article>
            ))}
          </div>
        </section>
      ) : <p className="empty-copy">当前还没有已记录的物理模型或工具调用。</p>}
    </section>
  );
}
