"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Download,
  Eye,
  EyeOff,
  FileLock2,
  FileQuestion,
  HardDrive,
} from "lucide-react";
import { getClient } from "@/lib/api";
import type { ArtifactMeta, DisplayDisposition } from "@/app/types/types";
import { Button } from "@/components/ui/button";
import { MarkdownContent } from "@/app/components/MarkdownContent";

/**
 * Derive display-disposition from status + kind + media_type.
 * - only "present" artifacts can be shown
 * - text/markdown → inline
 * - image → download_only (safe default; no render)
 * - binary → download_only
 * - anything else → blocked
 */
function deriveDisposition(meta: ArtifactMeta): DisplayDisposition {
  if (meta.status !== "present") return "blocked";
  const mime = meta.media_type;
  if (
    mime.startsWith("text/") ||
    mime === "text/markdown" ||
    mime.includes("markdown")
  ) {
    return "inline";
  }
  if (mime.startsWith("image/")) return "download_only";
  if (mime.startsWith("application/") || mime === "application/octet-stream")
    return "download_only";
  return "blocked";
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function statusBadge(status: ArtifactMeta["status"]) {
  switch (status) {
    case "present":
      return { label: "就绪", tone: "tone-good" as const };
    case "missing":
      return { label: "缺失", tone: "tone-bad" as const };
    case "corrupt":
      return { label: "损坏", tone: "tone-bad" as const };
    case "quarantined":
      return { label: "已隔离", tone: "tone-live" as const };
  }
}

function dispositionLabel(disp: DisplayDisposition): string {
  switch (disp) {
    case "inline":
      return "可直接显示";
    case "download_only":
      return "仅可下载";
    case "blocked":
      return "已阻止显示";
  }
}

interface ArtifactViewerProps {
  artifactId: string;
  onClose: () => void;
}

export function ArtifactViewer({ artifactId, onClose }: ArtifactViewerProps) {
  const [meta, setMeta] = useState<ArtifactMeta | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [showContent, setShowContent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      setContent(null);
      setShowContent(false);
      try {
        const m = await getClient().getArtifactMeta(artifactId);
        if (cancelled) return;
        setMeta(m);
        // Only attempt content fetch for inline-safe kinds. 404 is expected if
        // the backend doesn't supply /content yet — surface meta regardless.
        const disp = deriveDisposition(m);
        if (disp === "inline") {
          try {
            const text = await getClient().getArtifactContent(artifactId);
            if (!cancelled) setContent(text);
          } catch {
            // /content may not exist yet; not a hard error
          }
        }
      } catch (e) {
        if (!cancelled) {
          setError(
            e && typeof e === "object" && "error" in e
              ? (e as { error: { message: string } }).error.message
              : "无法加载 Artifact"
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [artifactId]);

  return (
    <div
      className="artifact-viewer"
      role="dialog"
      aria-label="Artifact 查看器"
    >
      <div className="artifact-viewer-header">
        <h2>Artifact 查看器</h2>
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
        >
          关闭
        </Button>
      </div>

      {loading ? (
        <p className="artifact-loading">正在加载 Artifact 元数据…</p>
      ) : error ? (
        <div className="artifact-error-state">
          <AlertTriangle className="h-5 w-5" />
          <p>{error}</p>
        </div>
      ) : meta ? (
        <div className="artifact-body">
          {/* Metadata grid */}
          <div
            className="artifact-meta-grid"
            aria-label="Artifact 元数据"
          >
            <div className="artifact-meta-item">
              <small>状态</small>
              <span className={`status-tag ${statusBadge(meta.status).tone}`}>
                {statusBadge(meta.status).label}
              </span>
            </div>
            <div className="artifact-meta-item">
              <small>类型</small>
              <span>{meta.kind}</span>
            </div>
            <div className="artifact-meta-item">
              <small>Media Type</small>
              <code>{meta.media_type}</code>
            </div>
            <div className="artifact-meta-item artifact-meta-item--wide">
              <small>Content Hash</small>
              <code className="artifact-hash">{meta.content_hash}</code>
            </div>
            <div className="artifact-meta-item">
              <small>显示策略</small>
              <span>{dispositionLabel(deriveDisposition(meta))}</span>
            </div>
            <div className="artifact-meta-item">
              <small>创建时间</small>
              <span>{new Date(meta.created_at).toLocaleString("zh-CN")}</span>
            </div>
            {meta.source_url && (
              <div className="artifact-meta-item artifact-meta-item--wide">
                <small>来源 URL</small>
                <a
                  href={meta.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="evidence-link"
                >
                  {meta.source_url}
                </a>
              </div>
            )}
          </div>

          {/* Status-specific messages */}
          {meta.status === "missing" && (
            <div className="artifact-notice artifact-notice--bad">
              <FileQuestion className="h-5 w-5" />
              <div>
                <strong>Artifact 内容丢失</strong>
                <p>
                  元数据存在，但后端报告内容已丢失。请联系项目管理员判断是否重新生成。
                </p>
              </div>
            </div>
          )}

          {meta.status === "corrupt" && (
            <div className="artifact-notice artifact-notice--bad">
              <AlertTriangle className="h-5 w-5" />
              <div>
                <strong>内容校验失败</strong>
                <p>SHA-256 哈希不匹配，内容已损坏。不可信任当前内容。</p>
              </div>
            </div>
          )}

          {meta.status === "quarantined" && (
            <div className="artifact-notice artifact-notice--warn">
              <FileLock2 className="h-5 w-5" />
              <div>
                <strong>Artifact 已隔离</strong>
                <p>出于安全策略，此内容已被隔离，不显示也不下载。</p>
              </div>
            </div>
          )}

          {/* Content display — only for safe inline types */}
          {(() => {
            const disp = deriveDisposition(meta);
            if (disp !== "inline") return null;
            return (
              <div className="artifact-content-section">
                <div className="artifact-content-toolbar">
                  <strong>内容预览</strong>
                  <div className="artifact-content-actions">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setShowContent((v) => !v)}
                    >
                      {showContent ? (
                        <>
                          <EyeOff className="h-4 w-4" /> 隐藏内容
                        </>
                      ) : (
                        <>
                          <Eye className="h-4 w-4" /> 显示内容
                        </>
                      )}
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      asChild
                    >
                      <a
                        href={getClient().getArtifactContentUrl(meta.id)}
                        download
                        rel="noreferrer"
                      >
                        <Download className="h-4 w-4" /> 下载
                      </a>
                    </Button>
                  </div>
                </div>
                {showContent && content !== null && (
                  <div className="artifact-content-body">
                    {meta.kind === "markdown" ? (
                      <MarkdownContent content={content} />
                    ) : (
                      <pre>{content}</pre>
                    )}
                  </div>
                )}
                {showContent && content === null && (
                  <p className="artifact-content-error">
                    无法读取内容。后端 /content 端点可能未就绪。
                  </p>
                )}
              </div>
            );
          })()}

          {/* Non-inline disposition messages */}
          {meta.status === "present" &&
            (() => {
              const disp = deriveDisposition(meta);
              if (disp === "download_only") {
                return (
                  <div className="artifact-notice artifact-notice--info">
                    <HardDrive className="h-5 w-5" />
                    <div>
                      <strong>仅可下载</strong>
                      <p>
                        此 Artifact 为{meta.kind}
                        类型，仅提供下载链接。浏览器不执行或渲染该内容。
                      </p>
                      <Button
                        variant="outline"
                        size="sm"
                        asChild
                        className="artifact-download-btn"
                      >
                        <a
                          href={getClient().getArtifactContentUrl(meta.id)}
                          download
                          rel="noreferrer"
                        >
                          <Download className="h-4 w-4" /> 下载文件
                        </a>
                      </Button>
                    </div>
                  </div>
                );
              }
              if (disp === "blocked") {
                return (
                  <div className="artifact-notice artifact-notice--warn">
                    <FileLock2 className="h-5 w-5" />
                    <div>
                      <strong>内容已阻止</strong>
                      <p>
                        安全策略禁止显示此文件（{meta.media_type}
                        ）。如确需查看，请通过其他安全渠道。
                      </p>
                    </div>
                  </div>
                );
              }
              return null;
            })()}
        </div>
      ) : null}
    </div>
  );
}
