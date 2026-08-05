"use client";

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Building2,
  CheckCircle2,
  Clock,
  ExternalLink,
  Globe,
  Package,
  Search,
  Shield,
  ShoppingCart,
  Tag,
} from "lucide-react";
import { toast } from "sonner";
import { getClient } from "@/lib/api";
import type {
  BomItem,
  CheckoutHandoff,
  OfferSnapshot,
  ProjectSnapshot,
  PurchaseProposal,
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

// ── main component ──

interface ShoppingViewProps {
  snapshot: ProjectSnapshot;
}

type ShoppingPhase =
  | "browse-bom"
  | "searching-offers"
  | "comparing-offers"
  | "creating-proposal"
  | "proposal-ready"
  | "handing-off";

export function ShoppingView({ snapshot }: ShoppingViewProps) {
  // Extract BOM from latest solution
  const activeSolution: SolutionVersion | null = useMemo(() => {
    if (snapshot.project.active_solution_version_id) {
      return (
        snapshot.solutions.find(
          (s) => s.id === snapshot.project.active_solution_version_id,
        ) ?? snapshot.solutions.at(-1) ?? null
      );
    }
    return snapshot.solutions.at(-1) ?? null;
  }, [snapshot.solutions, snapshot.project.active_solution_version_id]);

  const bomItems: BomItem[] = useMemo(
    () =>
      (activeSolution?.bom ?? [])
        .filter((item): item is BomItem => "line_id" in item)
        .map((item) => item as BomItem),
    [activeSolution],
  );

  // State
  const [phase, setPhase] = useState<ShoppingPhase>("browse-bom");
  const [selectedBomLines, setSelectedBomLines] = useState<Set<string>>(
    new Set(),
  );
  const [offers, setOffers] = useState<OfferSnapshot[]>([]);
  const [offersByLine, setOffersByLine] = useState<
    Map<string, OfferSnapshot[]>
  >(new Map());
  const [searching, setSearching] = useState(false);
  const [proposal, setProposal] = useState<PurchaseProposal | null>(null);
  const [confirmedLines, setConfirmedLines] = useState<Set<string>>(new Set());
  const [handoff, setHandoff] = useState<CheckoutHandoff | null>(null);
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

  // Search offers for selected BOM lines
  const searchOffers = async () => {
    const lineIds = Array.from(selectedBomLines);
    if (lineIds.length === 0) {
      setError("请至少选择一条 BOM 行");
      return;
    }
    setSearching(true);
    setError(null);
    setPhase("searching-offers");
    try {
      const results = await getClient().searchOffers(snapshot.project.id, {
        bom_line_ids: lineIds,
      });
      setOffers(results);
      // Group by bom_line_id
      const grouped = new Map<string, OfferSnapshot[]>();
      for (const offer of results) {
        const existing = grouped.get(offer.bom_line_id) ?? [];
        existing.push(offer);
        grouped.set(offer.bom_line_id, existing);
      }
      setOffersByLine(grouped);
      setPhase("comparing-offers");
    } catch (e) {
      const msg =
        e && typeof e === "object" && "error" in e
          ? (e as { error: { message: string } }).error.message
          : "报价搜索失败";
      setError(msg);
      setPhase("browse-bom");
    } finally {
      setSearching(false);
    }
  };

  // Create purchase proposal from selected offers
  const createProposal = async (seller: string, lineOffers: {
    bomLineId: string;
    offerId: string;
    quantity: number;
    unitPrice: number;
    currency: string;
  }[]) => {
    setPhase("creating-proposal");
    setError(null);
    try {
      const basisHash =
        activeSolution?.basis_hash ?? snapshot.project.id;
      const result = await getClient().createPurchaseProposal(
        snapshot.project.id,
        {
          basis_hash: basisHash,
          seller,
          lines: lineOffers.map((lo) => ({
            bom_line_id: lo.bomLineId,
            offer_snapshot_id: lo.offerId,
            quantity: lo.quantity,
            unit_price: lo.unitPrice,
            currency: lo.currency,
          })),
        },
      );
      setProposal(result);
      setPhase("proposal-ready");
      toast.success("PurchaseProposal 已创建");
    } catch (e) {
      const msg =
        e && typeof e === "object" && "error" in e
          ? (e as { error: { message: string } }).error.message
          : "创建 PurchaseProposal 失败";
      setError(msg);
      setPhase("comparing-offers");
    }
  };

  // Confirm lines in a proposal
  const confirmLines = async (lineIds: string[]) => {
    if (!proposal) return;
    try {
      const updated = await getClient().confirmPurchaseLines(proposal.id, {
        line_ids: lineIds,
      });
      setProposal(updated);
      const confirmed = new Set(
        updated.lines.filter((l) => l.confirmed).map((l) => l.line_id),
      );
      setConfirmedLines(confirmed);
      toast.success("已确认所选行");
    } catch (e) {
      const msg =
        e && typeof e === "object" && "error" in e
          ? (e as { error: { message: string } }).error.message
          : "确认行失败";
      toast.error(msg);
    }
  };

  // Handoff to checkout
  const requestHandoff = async () => {
    if (!proposal) return;
    setPhase("handing-off");
    try {
      const result = await getClient().requestCheckoutHandoff(proposal.id);
      setHandoff(result);
      toast.success("Checkout handoff 已就绪");
    } catch (e) {
      const msg =
        e && typeof e === "object" && "error" in e
          ? (e as { error: { message: string } }).error.message
          : "Handoff 请求失败";
      toast.error(msg);
      setPhase("proposal-ready");
    }
  };

  // Reset to BOM browse
  const resetShopping = () => {
    setPhase("browse-bom");
    setSelectedBomLines(new Set());
    setOffers([]);
    setOffersByLine(new Map());
    setProposal(null);
    setConfirmedLines(new Set());
    setHandoff(null);
    setError(null);
  };

  // ── Render phases ──

  if (!activeSolution || bomItems.length === 0) {
    return (
      <div className="shopping-empty">
        <ShoppingCart className="h-8 w-8" />
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
            <ShoppingCart className="h-5 w-5" />
            Shopping
          </h2>
          <span className="shopping-phase-badge">BOM 浏览</span>
        </header>

        <div className="shopping-safety-notice">
          <Shield className="h-4 w-4" />
          <small>
            Shopping 只搜索报价和生成 PurchaseProposal。不保存或请求支付信息、地址、email。
          </small>
        </div>

        <p className="shopping-intro">
          选择需要采购的 BOM 行来搜索报价。你可以选择特定行或全选后逐行比较卖家、价格和库存。
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
            <label className="bom-selection-row" key={bomKey(item)}>
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
            disabled={selectedBomLines.size === 0 || searching}
            onClick={() => void searchOffers()}
          >
            <Search className="h-4 w-4" />
            {searching ? "搜索中…" : "搜索报价"}
          </Button>
        </div>
      </div>
    );
  }

  // Comparing Offers (after search)
  if (phase === "comparing-offers" || phase === "searching-offers") {
    const groupedBySeller = new Map<string, OfferSnapshot[]>();
    for (const offer of offers) {
      const existing = groupedBySeller.get(offer.seller) ?? [];
      existing.push(offer);
      groupedBySeller.set(offer.seller, existing);
    }

    return (
      <div className="shopping-view" role="region" aria-label="报价比较">
        <header className="shopping-header">
          <h2>
            <Tag className="h-5 w-5" />
            报价比较
          </h2>
          <div className="shopping-header-actions">
            <span className="shopping-phase-badge">
              {offers.length} 条报价
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
        {Array.from(offersByLine.entries()).map(
          ([bomLineId, lineOffers]) => {
            const bomItem = bomItems.find((b) => b.line_id === bomLineId);
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
                <div className="offer-grid">
                  {lineOffers.map((offer) => {
                    const expired = offer.expires_at
                      ? new Date(offer.expires_at) < new Date()
                      : false;
                    return (
                      <article
                        className={`offer-card ${expired ? "offer-card--expired" : ""}`}
                        key={offer.id}
                      >
                        <header>
                          <Building2 className="h-4 w-4" />
                          <strong>{offer.seller}</strong>
                          {expired && (
                            <span className="status-tag tone-bad">已过期</span>
                          )}
                        </header>

                        <div className="offer-details">
                          <div className="offer-price">
                            <span className="offer-price-value">
                              {offer.price.toLocaleString()} {offer.currency}
                            </span>
                            <small>/ {offer.condition}</small>
                          </div>

                          <div className="offer-meta">
                            <div className="offer-meta-item">
                              <Package className="h-3 w-3" />
                              <span>库存: {offer.stock}</span>
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
                                  {new Date(
                                    offer.expires_at,
                                  ).toLocaleDateString("zh-CN")}
                                </span>
                              </div>
                            )}
                          </div>
                        </div>

                        <a
                          href={offer.listing_url}
                          target="_blank"
                          rel="noreferrer"
                          className="offer-listing-link"
                        >
                          <ExternalLink className="h-3 w-3" /> 查看清单
                        </a>

                        <Button
                          size="sm"
                          disabled={expired}
                          onClick={() =>
                            createProposal(offer.seller, [
                              {
                                bomLineId: offer.bom_line_id,
                                offerId: offer.id,
                                quantity: bomItem?.quantity ?? 1,
                                unitPrice: offer.price,
                                currency: offer.currency,
                              },
                            ])
                          }
                        >
                          <ShoppingCart className="h-3 w-3" />
                          从此卖家创建 Proposal
                        </Button>
                      </article>
                    );
                  })}
                </div>
              </section>
            );
          },
        )}

        {/* Multi-seller proposal: group by seller and create combined */}
        {groupedBySeller.size > 1 && (
          <div className="shopping-combined-proposal">
            <h3>按卖家汇总创建 Proposal</h3>
            {Array.from(groupedBySeller.entries()).map(
              ([seller, sellerOffers]) => (
                <Button
                  key={seller}
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    createProposal(
                      seller,
                      sellerOffers.map((offer) => ({
                        bomLineId: offer.bom_line_id,
                        offerId: offer.id,
                        quantity:
                          bomItems.find(
                            (b) => b.line_id === offer.bom_line_id,
                          )?.quantity ?? 1,
                        unitPrice: offer.price,
                        currency: offer.currency,
                      })),
                    )
                  }
                >
                  <Building2 className="h-4 w-4" />
                  {seller} ({sellerOffers.length} 行)
                </Button>
              ),
            )}
          </div>
        )}
      </div>
    );
  }

  // Proposal ready / handing off
  if (
    phase === "proposal-ready" ||
    phase === "creating-proposal" ||
    phase === "handing-off"
  ) {
    if (!proposal) {
      return (
        <div className="shopping-loading">
          正在创建 PurchaseProposal…
        </div>
      );
    }

    return (
      <div className="shopping-view" role="region" aria-label="购买提案">
        <header className="shopping-header">
          <h2>
            <ShoppingCart className="h-5 w-5" />
            Purchase Proposal
          </h2>
          <div className="shopping-header-actions">
            <span
              className={`status-tag ${proposal.status === "confirmed" ? "tone-good" : proposal.status === "handed_off" ? "tone-good" : "tone-live"}`}
            >
              {proposal.status}
            </span>
            <Button variant="ghost" size="sm" onClick={resetShopping}>
              ← 返回 BOM
            </Button>
          </div>
        </header>

        <div className="proposal-meta">
          <div className="proposal-meta-item">
            <small>卖家</small>
            <strong>{proposal.seller}</strong>
          </div>
          <div className="proposal-meta-item">
            <small>总价</small>
            <strong>
              {proposal.total_price.toLocaleString()} {proposal.currency}
            </strong>
          </div>
          <div className="proposal-meta-item">
            <small>Basis</small>
            <code>{proposal.basis_hash.slice(0, 16)}…</code>
          </div>
          <div className="proposal-meta-item">
            <small>创建时间</small>
            <span>
              {new Date(proposal.created_at).toLocaleString("zh-CN")}
            </span>
          </div>
        </div>

        {/* Lines */}
        <div className="proposal-lines">
          <div className="proposal-lines-header">
            <h3>明细 ({proposal.lines.length})</h3>
            {proposal.status === "draft" && (
              <Button
                size="sm"
                disabled={
                  confirmedLines.size === proposal.lines.length ||
                  proposal.lines.length === 0
                }
                onClick={() =>
                  confirmLines(
                    proposal.lines
                      .filter((l) => !l.confirmed)
                      .map((l) => l.line_id),
                  )
                }
              >
                <CheckCircle2 className="h-3 w-3" />
                确认全部未确认行
              </Button>
            )}
          </div>

          <div className="proposal-lines-grid">
            {proposal.lines.map((line) => {
              const bomItem = bomItems.find(
                (b) => b.line_id === line.bom_line_id,
              );
              const offer = offers.find(
                (o) => o.id === line.offer_snapshot_id,
              );
              return (
                <div
                  className={`proposal-line-card ${line.confirmed ? "is-confirmed" : ""}`}
                  key={line.line_id}
                >
                  <div className="proposal-line-header">
                    <span
                      className={`status-tag ${line.confirmed ? "tone-good" : "tone-live"}`}
                    >
                      {line.confirmed ? "已确认" : "待确认"}
                    </span>
                    {proposal.status === "draft" && !line.confirmed && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => confirmLines([line.line_id])}
                      >
                        确认此行
                      </Button>
                    )}
                  </div>

                  <strong>
                    {bomItem ? bomLabel(bomItem) : shortId(line.bom_line_id)}
                  </strong>

                  <div className="proposal-line-detail">
                    <span>
                      {line.quantity} × {line.unit_price.toLocaleString()}{" "}
                      {line.currency}
                    </span>
                    {offer && (
                      <small>
                        {offer.seller} · {offer.condition} · {offer.region}
                      </small>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Handoff action — only after user-initiated click */}
        {!handoff &&
          proposal.status !== "handed_off" &&
          proposal.lines.every((l) => l.confirmed) && (
            <div className="proposal-handoff-section">
              <div className="shopping-safety-notice">
                <Shield className="h-4 w-4" />
                <small>
                  点击后请求 checkout handoff。不自动跳转、不保存支付/地址/email 信息。
                </small>
              </div>
              <Button
                disabled={phase === "handing-off"}
                onClick={() => void requestHandoff()}
                size="lg"
              >
                <ExternalLink className="h-4 w-4" />
                {phase === "handing-off"
                  ? "正在请求 Handoff…"
                  : "请求 Checkout Handoff"}
              </Button>
            </div>
          )}

        {/* Handoff result */}
        {handoff && (
          <div className="checkout-handoff-card">
            <CheckCircle2 className="h-6 w-6 tone-good" />
            <div>
              <strong>Checkout 已就绪</strong>
              <p>Provider: {handoff.provider}</p>
              <p>
                截止:{" "}
                {new Date(handoff.expires_at).toLocaleString("zh-CN")}
              </p>
              <code>ID: {handoff.id}</code>
            </div>
            <Button asChild size="lg">
              <a
                href={handoff.checkout_url}
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink className="h-4 w-4" />
                在新窗口打开{handoff.provider} Checkout
              </a>
            </Button>
            <p className="checkout-handoff-note">
              此链接指向 {handoff.provider} 的托管 checkout 页面。支付和配送详情由该平台处理。
              Aidison 不保存或请求支付信息、地址、email。
            </p>
          </div>
        )}
      </div>
    );
  }

  return null;
}
