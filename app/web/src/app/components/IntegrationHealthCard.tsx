"use client";

import useSWR from "swr";
import {
  AlertTriangle,
  CheckCircle2,
  RefreshCw,
  Settings2,
  SignalHigh,
  SignalLow,
  WifiOff,
  XCircle,
} from "lucide-react";
import { getClient } from "@/lib/api";

const HEALTH_REFRESH_MS = 60_000; // 1 minute

function SeverityIcon({
  severity,
}: {
  severity: "info" | "warning" | "critical";
}) {
  switch (severity) {
    case "critical":
      return <XCircle className="tone-bad h-4 w-4" />;
    case "warning":
      return <AlertTriangle className="tone-live h-4 w-4" />;
    case "info":
      return <CheckCircle2 className="tone-good h-4 w-4" />;
  }
}

export function IntegrationHealthCard() {
  const { data, error, isLoading, mutate } = useSWR(
    "integration-health",
    () => getClient().getIntegrationHealth(),
    { refreshInterval: HEALTH_REFRESH_MS }
  );

  if (isLoading && !data) {
    return (
      <div
        className="health-card health-card--loading"
        aria-label="集成健康状态加载中"
      >
        <RefreshCw className="h-4 w-4 animate-spin" />
        <span>检查集成状态中…</span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div
        className="health-card health-card--error"
        aria-label="集成健康状态检查失败"
      >
        <WifiOff className="h-5 w-5" />
        <div>
          <strong>无法获取集成健康状态</strong>
          <p>{error instanceof Error ? error.message : "网络或后端不可达"}</p>
        </div>
      </div>
    );
  }

  const health = data;

  // Backend returns { status, shopping: { provider, available } }.
  // Compute derived indicators client-side.
  const shoppingOk = health.shopping?.available ?? false;
  const configLabel = shoppingOk ? "正常" : "降级";
  const configStatus = shoppingOk ? ("ok" as const) : ("warning" as const);
  const configIcon = shoppingOk ? (
    <CheckCircle2 className="tone-good h-4 w-4" />
  ) : (
    <AlertTriangle className="tone-live h-4 w-4" />
  );

  const backendLabel = health.status === "ok" ? "可达" : "降级";
  const backendIcon =
    health.status === "ok" ? (
      <SignalHigh className="tone-good h-4 w-4" />
    ) : (
      <SignalLow className="tone-live h-4 w-4" />
    );

  const lastChecked = health.last_checked_at
    ? new Date(health.last_checked_at).toLocaleString("zh-CN")
    : "未知";
  const errors = health.errors ?? [];

  return (
    <div
      className="health-card"
      role="region"
      aria-label="集成健康状态"
    >
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
            <span>Shopping 集成</span>
          </div>
          <span
            className={`status-tag ${
              configStatus === "ok" ? "tone-good" : "tone-live"
            }`}
          >
            {configLabel}
          </span>
          <span
            className="health-timestamp ml-2"
            style={{ fontSize: "0.62rem", color: "var(--ink-soft)" }}
          >
            provider: {health.shopping?.provider ?? "N/A"}
          </span>
        </div>

        <div className="health-indicator">
          <div className="health-indicator-label">
            {backendIcon}
            <span>后端状态</span>
          </div>
          <span
            className={`status-tag ${
              health.status === "ok" ? "tone-good" : "tone-live"
            }`}
          >
            {backendLabel}
          </span>
        </div>

        {lastChecked !== "未知" && (
          <div className="health-indicator">
            <div className="health-indicator-label">
              <ClockIcon />
              <span>最近检查</span>
            </div>
            <span className="health-timestamp">{lastChecked}</span>
          </div>
        )}
      </div>

      {errors.length > 0 && (
        <div
          className="health-errors"
          aria-label="集成错误"
        >
          <small>安全错误码</small>
          {errors.map((err) => (
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

      {errors.length === 0 && (
        <div className="health-no-errors">
          <CheckCircle2 className="tone-good h-4 w-4" />
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
      <circle
        cx="12"
        cy="12"
        r="10"
      />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}
