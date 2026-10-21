import type { EvidenceBinding, FreshnessInfo } from "@/app/types/types";

const FRESH_WINDOW_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
const AGING_WINDOW_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

/**
 * Compute freshness bucket for a single EvidenceBinding.
 * - fresh: observed within the last 7 days
 * - aging: observed 7–30 days ago
 * - stale: older than 30 days, or status is "stale"/"retracted"
 */
export function computeFreshness(binding: EvidenceBinding): FreshnessInfo {
  const now = Date.now();
  const observedMs = new Date(binding.observed_at).getTime();
  const age = now - observedMs;

  let freshness: FreshnessInfo["freshness"];
  if (binding.status === "stale" || binding.status === "retracted") {
    freshness = "stale";
  } else if (age <= FRESH_WINDOW_MS) {
    freshness = "fresh";
  } else if (age <= AGING_WINDOW_MS) {
    freshness = "aging";
  } else {
    freshness = "stale";
  }

  return {
    bindingId: binding.id,
    claim: binding.claim,
    sourceUrl: binding.source_url,
    spanText: binding.span_text,
    hash: binding.snapshot_hash,
    status: binding.status,
    observedAt: binding.observed_at,
    freshness,
    applicability: binding.applicability,
  };
}

export function freshLabel(freshness: FreshnessInfo["freshness"]): string {
  switch (freshness) {
    case "fresh":
      return "新鲜";
    case "aging":
      return "老化中";
    case "stale":
      return "已过时";
  }
}

export function freshTone(freshness: FreshnessInfo["freshness"]): string {
  switch (freshness) {
    case "fresh":
      return "tone-good";
    case "aging":
      return "tone-live";
    case "stale":
      return "tone-bad";
  }
}
