"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  AlertTriangle,
  Building2,
  CheckCircle2,
  Clock,
  ExternalLink,
  Globe,
  Info,
  Package,
  Search,
  Shield,
  Tag,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { toast } from "sonner";
import { getClient } from "@/lib/api";
import type {
  BomItem,
  OfferSnapshot,
  ProjectSnapshot,
  ShoppingBudgetSummary,
  ShoppingOfferRecommendation,
  ShoppingOfferRecommendations,
  SolutionVersion,
} from "@/app/types/types";
import { Button } from "@/components/ui/button";

// ── helpers ──

function shortId(value: string): string {
  return value.slice(0, 8);
}

function bomLabel(item: BomItem): string {
  return item.name;
}

function bomKey(item: BomItem): string {
  return item.line_id;
}

function isExpired(expiresAt: string | null | undefined): boolean {
  return expiresAt ? new Date(expiresAt) < new Date() : false;
}

function classificationTone(
  classification: "within" | "over" | "unknown"
): string {
  if (classification === "within") return "tone-good";
  if (classification === "over") return "tone-bad";
  return "tone-live";
}

function classificationLabel(
  classification: "within" | "over" | "unknown"
): string {
  if (classification === "within") return "预算内";
  if (classification === "over") return "已超预算";
  return "无法判断";
}

// ── main component ──

interface ShoppingViewProps {
  snapshot: ProjectSnapshot;
}

type ShoppingPhase =
  | "browse-bom"
  | "searching-offers"
  | "comparing-offers"
  | "budget-summary";

export function ShoppingView({ snapshot }: ShoppingViewProps) {
  const { data: integrationHealth, error: integrationHealthError } = useSWR(
    "integration-health",
    () => getClient().getIntegrationHealth()
  );
  const searchEnabled = Boolean(
    integrationHealth?.shopping.available && integrationHealth.shopping.search
  );

  // Extract BOM from latest solution
  const activeSolution: SolutionVersion | null = useMemo(() => {
    if (snapshot.project.active_solution_version_id) {
      return (
        snapshot.solutions.find(
          (s) => s.id === snapshot.project.active_solution_version_id
        ) ??
        snapshot.solutions.at(-1) ??
        null
      );
    }
    return snapshot.solutions.at(-1) ?? null;
  }, [snapshot.solutions, snapshot.project.active_solution_version_id]);

  const bomItems: BomItem[] = useMemo(
    () =>
      (activeSolution?.bom ?? [])
        .filter((item): item is BomItem => "line_id" in item)
        .map((item) => item as BomItem),
    [activeSolution]
  );

  const [phase, setPhase] = useState<ShoppingPhase>("browse-bom");
  const [selectedBomLines, setSelectedBomLines] = useState<Set<string>>(
    new Set()
  );
  const [offersByLine, setOffersByLine] = useState<
    Map<string, OfferSnapshot[]>
  >(new Map());
  const [searchErrorsByLine, setSearchErrorsByLine] = useState<
    Map<string, string>
  >(new Map());
  const [searching, setSearching] = useState(false);
  const [recommendationsByLine, setRecommendationsByLine] = useState<
    Map<string, ShoppingOfferRecommendations>
  >(new Map());
  const [recommendationErrors, setRecommendationErrors] = useState<
    Map<string, string>
  >(new Map());
  const [recommendationsLoading, setRecommendationsLoading] = useState<
    Set<string>
  >(new Set());
  // One selected offer per BOM line for the read-only budget preview.
  const [budgetSelection, setBudgetSelection] = useState<
    Map<string, { offerId: string; quantity: number }>
  >(new Map());
  const [budgetSummary, setBudgetSummary] =
    useState<ShoppingBudgetSummary | null>(null);
  const [budgetLoading, setBudgetLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Toggle BOM line selection
  const toggleBomLine = (lineId: string) => {
    setSelectedBomLines((prev) => {
      const next = new Set(prev);
      if (next.has(lineId)) next.delete(lineId);
      else next.add(lineId);
      return next;
    });
  };

  // Search offers for selected BOM lines; failures are kept per BOM line so
  // a single failed line never hides the results of the remaining lines.
  const searchOffers = async () => {
    const lineIds = Array.from(selectedBomLines);
    if (lineIds.length === 0) {
      setError("请至少选择一条 BOM 行");
      return;
    }
    setSearching(true);
    setError(null);
    setPhase("searching-offers");
    setOffersByLine(new Map());
    setSearchErrorsByLine(new Map());
    setRecommendationsByLine(new Map());
    setRecommendationErrors(new Map());
    const nextOffers = new Map<string, OfferSnapshot[]>();
    const nextErrors = new Map<string, string>();
    for (const bomLineId of lineIds) {
      const bomItem = bomItems.find((b) => b.line_id === bomLineId);
      const query = bomItem?.name ?? bomLineId;
      try {
        // Backend 3cfd23a: single bom_line_id, query REQUIRED non-empty.
        const offersResp = await getClient().searchOffers(
          snapshot.project.id,
          snapshot.project.revision,
          {
            query,
            bom_line_id: bomLineId,
            region: "CN",
            max_results: 10,
          }
        );
        nextOffers.set(bomLineId, offersResp);
      } catch (e) {
        const msg =
          e && typeof e === "object" && "error" in e
            ? (e as { error: { message: string } }).error.message
            : "报价搜索失败";
        nextErrors.set(bomLineId, msg);
      }
    }
    setOffersByLine(nextOffers);
    setSearchErrorsByLine(nextErrors);
    setBudgetSelection(new Map());
    setBudgetSummary(null);
    setRecommendationsByLine(new Map());
    setRecommendationErrors(new Map());
    setPhase("comparing-offers");
    setSearching(false);
    void loadRecommendations(lineIds);
  };

  // Read-only, explainable ranking of saved offer snapshots per BOM line.
  const loadRecommendations = async (lineIds: string[]) => {
    setRecommendationsLoading((prev) => {
      const next = new Set(prev);
      for (const id of lineIds) next.add(id);
      return next;
    });
    const nextRecommendations = new Map<string, ShoppingOfferRecommendations>();
    const nextErrors = new Map<string, string>();
    for (const bomLineId of lineIds) {
      try {
        const recs = await getClient().getShoppingRecommendations(
          snapshot.project.id,
          bomLineId
        );
        nextRecommendations.set(bomLineId, recs);
      } catch (e) {
        const msg =
          e && typeof e === "object" && "error" in e
            ? (e as { error: { message: string } }).error.message
            : "推荐加载失败";
        nextErrors.set(bomLineId, msg);
      }
    }
    setRecommendationsByLine(nextRecommendations);
    setRecommendationErrors(nextErrors);
    setRecommendationsLoading((prev) => {
      const next = new Set(prev);
      for (const id of lineIds) next.delete(id);
      return next;
    });
  };

  // Pick one offer per BOM line for the read-only budget preview.
  const toggleBudgetOffer = (
    lineId: string,
    offerId: string,
    quantity: number
  ) => {
    setBudgetSelection((prev) => {
      const next = new Map(prev);
      if (next.get(lineId)?.offerId === offerId) next.delete(lineId);
      else next.set(lineId, { offerId, quantity });
      return next;
    });
  };

  const buildBudgetSummary = async () => {
    if (budgetSelection.size === 0) {
      toast.error("请至少为一条 BOM 行选择一个报价");
      return;
    }
    setBudgetLoading(true);
    setError(null);
    try {
      const selections = Array.from(budgetSelection.entries()).map(
        ([, sel]) => ({
          offer_snapshot_id: sel.offerId,
          quantity: sel.quantity,
        })
      );
      const summary = await getClient().previewShoppingBudget(
        snapshot.project.id,
        selections
      );
      setBudgetSummary(summary);
      setPhase("budget-summary");
    } catch (e) {
      const msg =
        e && typeof e === "object" && "error" in e
          ? (e as { error: { message: string } }).error.message
          : "预算汇总失败";
      setError(msg);
    } finally {
      setBudgetLoading(false);
    }
  };

  const resetShopping = () => {
    setPhase("browse-bom");
    setSelectedBomLines(new Set());
    setOffersByLine(new Map());
    setSearchErrorsByLine(new Map());
    setRecommendationsByLine(new Map());
    setRecommendationErrors(new Map());
    setBudgetSelection(new Map());
    setBudgetSummary(null);
    setError(null);
  };

  // Return to the offer/recommendation list without discarding the selection.
  const backToComparing = () => {
    setPhase("comparing-offers");
  };

  // ── Render phases ──

  if (!activeSolution || bomItems.length === 0) {
    return (
      <div className="shopping-empty">
        <Package className="h-8 w-8" />
        <p>
          当前没有活跃的解决方案。BOM 在方案冻结后可用，之后才能启动 Shopping。
        </p>
      </div>
    );
  }

  // Browse BOM
  if (phase === "browse-bom") {
    return (
      <div className="shopping-view" role="region" aria-label="Shopping">
        <header className="shopping-header">
          <h2>
            <Tag className="h-5 w-5" />
            Shopping
          </h2>
          <span className="shopping-phase-badge">BOM 浏览</span>
        </header>

        <div className="shopping-safety-notice">
          <Shield className="h-4 w-4" />
          <small>
            仅提供只读商品推荐与预算汇总。不会创建购物车、PurchaseProposal、授权、订单或支付。
          </small>
        </div>

        <p className="shopping-intro">
          选择需要采购的 BOM
          行来搜索报价。搜索后展示逐行报价、可解释推荐，并可就选中的报价生成只读预算汇总。
        </p>

        <div className="bom-selection-list">
          <div className="bom-selection-header">
            <span>BOM 行 ({bomItems.length})</span>
            <button
              className="bom-select-all"
              onClick={() => {
                if (selectedBomLines.size === bomItems.length) {
                  setSelectedBomLines(new Set());
                } else {
                  setSelectedBomLines(new Set(bomItems.map((b) => b.line_id)));
                }
              }}
            >
              {selectedBomLines.size === bomItems.length ? "取消全选" : "全选"}
            </button>
          </div>
          {bomItems.map((item) => (
            <label
              className="bom-selection-row"
              key={bomKey(item)}
            >
              <input
                type="checkbox"
                checked={selectedBomLines.has(item.line_id)}
                onChange={() => toggleBomLine(item.line_id)}
              />
              <div className="bom-selection-info">
                <strong>{bomLabel(item)}</strong>
                <span>
                  {item.quantity} {item.unit}
                </span>
              </div>
              <code>{shortId(item.line_id)}</code>
            </label>
          ))}
        </div>

        {error && <p className="shopping-error">{error}</p>}

        <div className="shopping-actions">
          <Button
            disabled={selectedBomLines.size === 0 || searching || !searchEnabled}
            onClick={() => void searchOffers()}
          >
            <Search className="h-4 w-4" />
            {searching ? "搜索中…" : searchEnabled ? "搜索报价" : "搜索当前不可用"}
          </Button>
        </div>
        {integrationHealthError ? (
          <p className="shopping-error">无法读取商品能力，已安全禁用搜索。</p>
        ) : null}
      </div>
    );
  }

  // Comparing offers (after search) — offers + explainable recommendations
  if (phase === "searching-offers" || phase === "comparing-offers") {
    return (
      <div className="shopping-view" role="region" aria-label="报价比较">
        <header className="shopping-header">
          <h2>
            <Tag className="h-5 w-5" />
            报价与推荐
          </h2>
          <div className="shopping-header-actions">
            <span className="shopping-phase-badge">
              {selectedBomLines.size} 行
            </span>
            <Button variant="ghost" size="sm" onClick={resetShopping}>
              ← 返回 BOM
            </Button>
          </div>
        </header>

        {error && (
          <div className="shopping-error-banner">
            <AlertTriangle className="h-4 w-4" />
            <p>{error}</p>
          </div>
        )}

        {/* Group by BOM line */}
        {Array.from(selectedBomLines).map((bomLineId) => {
          const bomItem = bomItems.find((b) => b.line_id === bomLineId);
          const lineOffers = offersByLine.get(bomLineId) ?? [];
          const lineError = searchErrorsByLine.get(bomLineId);
          const recommendations = recommendationsByLine.get(bomLineId);
          const recError = recommendationErrors.get(bomLineId);
          const recLoading = recommendationsLoading.has(bomLineId);

          return (
            <section className="offer-group" key={bomLineId}>
              <h3>
                <Package className="h-4 w-4" />
                {bomItem ? bomLabel(bomItem) : shortId(bomLineId)}
                {bomItem && (
                  <span className="offer-bom-qty">
                    {bomItem.quantity} {bomItem.unit}
                  </span>
                )}
              </h3>

              {phase === "searching-offers" ? (
                <p className="shopping-loading-note">正在搜索该行报价…</p>
              ) : null}

              {lineError && (
                <div className="shopping-error-banner">
                  <AlertTriangle className="h-4 w-4" />
                  <p>该行报价搜索失败：{lineError}</p>
                </div>
              )}

              {!lineError && lineOffers.length === 0 && !searching && (
                <p className="shopping-loading-note">该行未返回报价快照。</p>
              )}

              {lineOffers.length > 0 && (
                <div className="offer-grid">
                  {lineOffers.map((offer) => {
                    const expired = isExpired(offer.expires_at);
                    const selected = budgetSelection.get(bomLineId)?.offerId;
                    const isSelected = selected === offer.id;
                    return (
                      <article
                        className={`offer-card ${
                          expired ? "offer-card--expired" : ""
                        }`}
                        key={offer.id}
                      >
                        <header>
                          <Building2 className="h-4 w-4" />
                          <strong>{offer.seller ?? offer.provider}</strong>
                          {expired && (
                            <span className="status-tag tone-bad">已过期</span>
                          )}
                          {isSelected && (
                            <span className="status-tag tone-good">已选</span>
                          )}
                        </header>

                        <div className="offer-details">
                          <div className="offer-price">
                            <span className="offer-price-value">
                              {offer.unit_price} {offer.currency}
                            </span>
                            <small>/ {offer.condition ?? "规格待核"}</small>
                          </div>

                          <div className="offer-meta">
                            <div className="offer-meta-item">
                              <Package className="h-3 w-3" />
                              <span>
                                库存:{" "}
                                {offer.availability === "unknown"
                                  ? "未知"
                                  : offer.quantity_available}
                              </span>
                            </div>
                            <div className="offer-meta-item">
                              <Globe className="h-3 w-3" />
                              <span>{offer.region}</span>
                            </div>
                            {offer.expires_at && (
                              <div className="offer-meta-item">
                                <Clock className="h-3 w-3" />
                                <span>
                                  截至{" "}
                                  {new Date(offer.expires_at).toLocaleDateString(
                                    "zh-CN"
                                  )}
                                </span>
                              </div>
                            )}
                          </div>
                        </div>

                        <a
                          href={offer.product_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="offer-listing-link"
                        >
                          <ExternalLink className="h-3 w-3" /> 查看商品清单
                        </a>

                        <small className="shopping-read-only-note">
                          只读推荐；请在平台页面核对当前价格、库存与规格后再决定。
                        </small>

                        <Button
                          size="sm"
                          variant={isSelected ? "default" : "outline"}
                          disabled={expired}
                          onClick={() =>
                            toggleBudgetOffer(
                              bomLineId,
                              offer.id,
                              Math.ceil(bomItem?.quantity ?? 1)
                            )
                          }
                        >
                          <CheckCircle2 className="h-3 w-3" />
                          {isSelected ? "取消选用" : "选用并计入预算"}
                        </Button>
                      </article>
                    );
                  })}
                </div>
              )}

              {/* Explainable, read-only recommendation ranking */}
              <div className="recommendation-section">
                <h4>
                  <TrendingUp className="h-4 w-4" />
                  已保存报价的可解释推荐
                </h4>
                {recLoading && (
                  <p className="shopping-loading-note">正在加载推荐…</p>
                )}
                {recError && (
                  <p className="shopping-loading-note">
                    推荐加载失败：{recError}
                  </p>
                )}
                {!recLoading && !recError && !recommendations && (
                  <p className="shopping-loading-note">
                    该行暂无推荐（搜索后自动生成）。
                  </p>
                )}
                {!recLoading && recommendations && (
                  <>
                    {recommendations.budget_amount && (
                      <p className="recommendation-budget">
                        项目预算：{recommendations.budget_amount}{" "}
                        {recommendations.budget_currency ?? ""}
                      </p>
                    )}
                    {recommendations.items.length === 0 ? (
                      <p className="shopping-loading-note">
                        该行暂无已保存报价快照。
                      </p>
                    ) : (
                      <ol className="recommendation-list">
                        {recommendations.items.map((rec) => (
                          <RecommendationItem
                            key={rec.offer_snapshot_id}
                            rec={rec}
                          />
                        ))}
                      </ol>
                    )}
                  </>
                )}
              </div>
            </section>
          );
        })}

        {/* Budget selection bar */}
        {phase === "comparing-offers" && (
          <div className="budget-action-bar">
            <span>
              已选 {budgetSelection.size} 行报价计入预算
              {error ? <span className="shopping-error"> · {error}</span> : null}
            </span>
            <Button
              disabled={budgetSelection.size === 0 || budgetLoading}
              onClick={() => void buildBudgetSummary()}
            >
              <Wallet className="h-4 w-4" />
              {budgetLoading ? "汇总中…" : "生成预算汇总"}
            </Button>
          </div>
        )}
      </div>
    );
  }

  // Budget summary
  if (phase === "budget-summary") {
    if (!budgetSummary) {
      return <div className="shopping-loading">正在汇总预算…</div>;
    }
    const summary = budgetSummary;
    const statusTone = classificationTone(summary.classification);
    const statusLabel = classificationLabel(summary.classification);

    return (
      <div className="shopping-view" role="region" aria-label="预算汇总">
        <header className="shopping-header">
          <h2>
            <Wallet className="h-5 w-5" />
            预算汇总
          </h2>
          <div className="shopping-header-actions">
            <span className={`status-tag ${statusTone}`}>{statusLabel}</span>
            <Button variant="ghost" size="sm" onClick={backToComparing}>
              ← 返回报价
            </Button>
            <Button variant="ghost" size="sm" onClick={resetShopping}>
              返回 BOM
            </Button>
          </div>
        </header>

        {summary.classification === "over" && (
          <div className="shopping-error-banner">
            <AlertTriangle className="h-4 w-4" />
            <p>已选中报价的预计总价超出项目预算。</p>
          </div>
        )}
        {summary.classification === "unknown" && (
          <div className="shopping-safety-notice">
            <Info className="h-4 w-4" />
            <small>
              部分报价的运费/税费缺失或币种不一致，无法给出最终总价；请以淘宝页面为准。
            </small>
          </div>
        )}

        <div className="budget-meta">
          <div className="budget-meta-item">
            <small>项目预算</small>
            <strong>
              {summary.budget_amount} {summary.budget_currency}
            </strong>
          </div>
          <div className="budget-meta-item">
            <small>商品小计</small>
            <strong>{summary.merchandise_subtotal ?? "未知"}</strong>
          </div>
          <div className="budget-meta-item">
            <small>预计总价</small>
            <strong>{summary.total_amount ?? "未知"}</strong>
          </div>
          <div className="budget-meta-item">
            <small>预算剩余</small>
            <strong>{summary.remaining_amount ?? "未知"}</strong>
          </div>
        </div>

        <div className="budget-lines">
          <div className="budget-lines-header">
            <h3>明细与核验依据</h3>
          </div>
          <div className="budget-lines-grid">
            {summary.lines.map((line) => {
              const tone = classificationTone(line.classification);
              return (
                <div className="budget-line-card" key={line.offer_snapshot_id}>
                  <div className="budget-line-header">
                    <span className={`status-tag ${tone}`}>
                      {classificationLabel(line.classification)}
                    </span>
                    <code>{shortId(line.bom_line_id)}</code>
                  </div>
                  <strong>{line.title}</strong>
                  <div className="budget-line-detail">
                    <span>
                      {line.quantity} × {line.unit_price} {line.currency}
                    </span>
                    <span>小计: {line.merchandise_subtotal ?? "未知"}</span>
                  </div>
                  <small className="budget-line-reason">{line.reason}</small>
                </div>
              );
            })}
          </div>
        </div>

        <div className="shopping-safety-notice">
          <Shield className="h-4 w-4" />
          <small>
            以上为只读预算汇总，不创建购物车、PurchaseProposal、订单或支付。运费与税费未确认时总价为未知，最终价格请以商品页为准。
          </small>
        </div>
      </div>
    );
  }

  return null;
}

// ── sub-component: one explainable recommendation ──

function RecommendationItem({
  rec,
}: {
  rec: ShoppingOfferRecommendation;
}) {
  const expired = isExpired(rec.expires_at);
  const tone = classificationTone(rec.listed_price_classification);
  return (
    <li className="recommendation-item">
      <div className="recommendation-item-head">
        <span className="recommendation-rank">#{rec.rank}</span>
        <strong>{rec.seller ?? rec.title}</strong>
        <span className={`status-tag ${tone}`}>
          {classificationLabel(rec.listed_price_classification)}
        </span>
        {expired && <span className="status-tag tone-bad">已过期</span>}
      </div>
      <div className="recommendation-item-title">{rec.title}</div>
      <div className="recommendation-item-meta">
        <span className="recommendation-price">
          {rec.unit_price} {rec.currency}
        </span>
        {rec.expires_at && (
          <span>
            <Clock className="h-3 w-3" />
            截至 {new Date(rec.expires_at).toLocaleDateString("zh-CN")}
          </span>
        )}
        {!rec.final_total_known && (
          <span className="status-tag tone-live">最终总价待核</span>
        )}
      </div>
      <p className="recommendation-rationale">{rec.rationale}</p>
      <a
        href={rec.product_url}
        target="_blank"
        rel="noopener noreferrer"
        className="offer-listing-link"
      >
        <ExternalLink className="h-3 w-3" /> 查看商品清单
      </a>
    </li>
  );
}
