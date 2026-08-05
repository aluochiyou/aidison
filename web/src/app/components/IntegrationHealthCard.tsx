"use client";

import useSWR from "swr";
import { AlertTriangle, CheckCircle2, RefreshCw, ServerOff, Settings2, SignalHigh, SignalLow, WifiOff, XCircle } from "lucide-react";
import { getClient } from "@/lib/api";

const HEALTH_REFRESH_MS = 60_000; // 1 minute

function SeverityIcon({ severity }: { severity: "info" | "warning" | "critical" }) {
  switch (severity) {
    case "critical":
      return <XCircle className="h-4 w-4 tone-bad" />;
    case "warning":
      return <AlertTriangle className="h-4 w-4 tone-live" />;
    case "info":
      return <CheckCircle2 className="h-4 w-4 tone-good" />;
  }
}

export function IntegrationHealthCard() {
  const { data, error, isLoading, mutate } = useSWR(
    "integration-health",
    () => getClient().getIntegrationHealth(),
    { refreshInterval: HEALTH_REFRESH_MS },
  );

  if (isLoading && !data) {
    return (
      <div className="health-card health-card--loading" aria-label="集成健康状态加载中">
        <RefreshCw className="h-4 w-4 animate-spin" />
        <span>检查集成状态中…</span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="health-card health-card--error" aria-label="集成健康状态检查失败">
        <WifiOff className="h-5 w-5" />
        <div>
          <strong>无法获取集成健康状态</strong>
          <p>{error instanceof Error ? error.message : "网络或后端不可达"}</p>
        </div>
      </div>
    );
  }

  const backendIcon = {
    reachable: <SignalHigh className="h-4 w-4 tone-good" />,
    degraded: <SignalLow className="h-4 w-4 tone-live" />,
    unreachable: <ServerOff className="h-4 w-4 tone-bad" />,
  }[data.backend];

  const configIcon = {
    ok: <CheckCircle2 className="h-4 w-4 tone-good" />,
    warning: <AlertTriangle className="h-4 w-4 tone-live" />,
    error: <XCircle className="h-4 w-4 tone-bad" />,
  }[data.config_status];

  const configLabel = {
    ok: "正常",
    warning: "警告",
    error: "异常",
  }[data.config_status];

  const backendLabel = {
    reachable: "可达",
    degraded: "受限",
    unreachable: "不可达",
  }[data.backend];

  return (
    <div className="health-card" role="region" aria-label="集成健康状态">
      <div className="health-card-header">
        <h3>
          <Settings2 className="h-4 w-4" />
          集成健康
        </h3>
        <button
          className="health-refresh-btn"
          onClick={() => void mutate()}
          aria-label="刷新健康状态"
        >
          <RefreshCw className="h-3 w-3" /> 刷新
        </button>
      </div>

      <div className="health-indicators">
        <div className="health-indicator">
          <div className="health-indicator-label">
            {configIcon}
            <span>配置状态</span>
          </div>
          <span className={`status-tag ${data.config_status === "ok" ? "tone-good" : data.config_status === "warning" ? "tone-live" : "tone-bad"}`}>
            {configLabel}
          </span>
        </div>

        <div className="health-indicator">
          <div className="health-indicator-label">
            {backendIcon}
            <span>后端连接</span>
          </div>
          <span className={`status-tag ${data.backend === "reachable" ? "tone-good" : data.backend === "degraded" ? "tone-live" : "tone-bad"}`}>
            {backendLabel}
          </span>
        </div>

        <div className="health-indicator">
          <div className="health-indicator-label">
            <ClockIcon />
            <span>最近检查</span>
          </div>
          <span className="health-timestamp">
            {new Date(data.last_checked_at).toLocaleString("zh-CN")}
          </span>
        </div>
      </div>

      {data.errors.length > 0 && (
        <div className="health-errors" aria-label="集成错误">
          <small>安全错误码</small>
          {data.errors.map((err) => (
            <div
              className={`health-error-item health-error-${err.severity}`}
              key={err.code}
            >
              <SeverityIcon severity={err.severity} />
              <div>
                <code>{err.code}</code>
                <p>{err.message}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {data.errors.length === 0 && (
        <div className="health-no-errors">
          <CheckCircle2 className="h-4 w-4 tone-good" />
          <span>无集成错误</span>
        </div>
      )}
    </div>
  );
}

function ClockIcon() {
  return (
    <svg
      className="h-4 w-4"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}
