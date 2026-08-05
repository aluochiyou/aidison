"use client";

import { X } from "lucide-react";
import type { FreshnessInfo } from "@/app/types/types";
import { freshLabel, freshTone } from "@/app/utils/freshness";

interface EvidenceDrawerProps {
  evidence: FreshnessInfo;
  onClose: () => void;
}

export function EvidenceDrawer({ evidence, onClose }: EvidenceDrawerProps) {
  return (
    <div className="evidence-drawer" role="dialog" aria-label={`证据详情: ${evidence.claim.slice(0, 60)}`}>
      <div className="evidence-drawer-header">
        <div>
          <small>EVIDENCE BINDING</small>
          <h3>{evidence.claim}</h3>
        </div>
        <button onClick={onClose} aria-label="关闭证据抽屉">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="evidence-drawer-body">
        <div className="evidence-drawer-grid">
          <div className="evidence-field">
            <small>来源 URL</small>
            {evidence.sourceUrl ? (
              <a
                href={evidence.sourceUrl}
                target="_blank"
                rel="noreferrer"
                className="evidence-link"
              >
                {evidence.sourceUrl}
              </a>
            ) : (
              <span className="evidence-empty">无来源链接</span>
            )}
          </div>

          <div className="evidence-field">
            <small>原文摘录 (span)</small>
            <blockquote>{evidence.spanText || "（无摘录）"}</blockquote>
          </div>

          <div className="evidence-field">
            <small>快照哈希 (hash)</small>
            <code>{evidence.hash}</code>
          </div>

          <div className="evidence-field">
            <small>状态</small>
            <span className={`status-tag ${evidence.status === "supported" ? "tone-good" : "tone-bad"}`}>
              {evidence.status}
            </span>
          </div>

          <div className="evidence-field">
            <small>时效性</small>
            <span className={`status-tag ${freshTone(evidence.freshness)}`}>
              {freshLabel(evidence.freshness)}
            </span>
          </div>

          <div className="evidence-field">
            <small>观察时间</small>
            <span>{new Date(evidence.observedAt).toLocaleString("zh-CN")}</span>
          </div>

          {evidence.applicability.length > 0 && (
            <div className="evidence-field evidence-field--wide">
              <small>适用范围</small>
              <div className="evidence-tags">
                {evidence.applicability.map((tag) => (
                  <span className="evidence-applicability-tag" key={tag}>
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
