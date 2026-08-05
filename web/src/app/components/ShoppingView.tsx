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

  // Carry the server-returned project revision across shopping steps
  // so confirm/checkout use the freshest value (not stale snapshot.revision).
  const [currentRevision, setCurrentRevision] = useState<number>(snapshot.project.revision);
  // ... existing state below
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
      // Backend 3cfd23a: single bom_line_id, query REQUIRED non-empty.
      // Send BOM item name as the query (bounded, non-empty search term).
      const revision = currentRevision;
      const allOffers: OfferSnapshot[] = [];
      for (const bomLineId of lineIds) {
        const bomItem = bomItems.find((b) => b.line_id === bomLineId);
        const query = bomItem?.name ?? bomLineId;
        try {
          const offersResp = await getClient().searchOffers(snapshot.project.id, revision, {
            query,
            bom_line_id: bomLineId,
            region: "CN",
            max_results: 10,
          });
          // Backend returns OfferSnapshot[] directly — not an envelope.
          allOffers.push(...offersResp);
        } catch {
          // skip individual line errors; continue with remaining
        }
      }
      setOffers(allOffers);
      // Group by bom_line_id
      const grouped = new Map<string, OfferSnapshot[]>();
      for (const offer of allOffers) {
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

  // Create purchase proposal from a single offer (backend 3cfd23a: single-offer, max_total required string)
  const createProposal = async (offer: OfferSnapshot, bomItem: BomItem) => {
    setPhase("creating-proposal");
    setError(null);
    try {
      const revision = currentRevision;
      const solutionVersionId = activeSolution?.id ?? "";
      // max_total: string required. Compute a safe ceiling (unit_price * quantity * 2).
      const unitD = parseFloat(offer.unit_price);
      const maxTotal = String((unitD * bomItem.quantity * 2).toFixed(2));
      const { data: proposalData, etag: nextRevision } = await getClient().createPurchaseProposal(
        snapshot.project.id,
        revision,
        {
          solution_version_id: solutionVersionId,
          offer_snapshot_id: offer.id,
          quantity: Math.ceil(bomItem.quantity),
          region: offer.region,
          currency: offer.currency,
          shipping_estimate: offer.shipping_estimate,
          tax_estimate: offer.tax_estimate,
          max_total: maxTotal,
        },
      );
      setProposal(proposalData);
      setCurrentRevision(nextRevision);
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

  // Confirm lines in a proposal (backend 3cfd23a: If-Match project revision)
  const confirmLines = async (lineIds: string[]) => {
    if (!proposal) return;
    try {
      const { data: updated, etag: nextRevision } = await getClient().confirmPurchaseLines(
        proposal.id,
        currentRevision,
        { confirmed_line_ids: lineIds },
      );
      setProposal(updated);
      setConfirmedLines(new Set(updated.confirmed_line_ids));
      setCurrentRevision(nextRevision);
      toast.success("已确认所选行");
    } catch (e) {
      const msg =
        e && typeof e === "object" && "error" in e
          ? (e as { error: { message: string } }).error.message
          : "确认行失败";
      toast.error(msg);
    }
  };

  // Handoff to checkout (backend 3cfd23a: If-Match project revision)
  const requestHandoff = async () => {
    if (!proposal) return;
    setPhase("handing-off");
    try {
      const { data: result } = await getClient().requestCheckoutHandoff(
        proposal.id,
        currentRevision,
      );
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

  // Reset shopping state and goto canonical snapshot data for phase
  const resetShopping = () => {
    // Restore phase from snapshot presence
    const snapOffers = snapshot.offer_snapshots;
    const snapProposals = snapshot.purchase_proposals;
    const snapHandoffs = snapshot.checkout_handoffs;

    if (snapHandoffs && snapHandoffs.length > 0) {
      setHandoff(snapHandoffs[0]);
      setProposal(snapProposals?.[0] ?? null);
      setPhase("proposal-ready");
    } else if (snapProposals && snapProposals.length > 0) {
      setProposal(snapProposals[0]);
      setPhase("proposal-ready");
    } else if (snapOffers && snapOffers.length > 0) {
      setOffers(snapOffers);
      const grouped = new Map<string, OfferSnapshot[]>();
      for (const o of snapOffers) {
        const existing = grouped.get(o.bom_line_id) ?? [];
        existing.push(o);
        grouped.set(o.bom_line_id, existing);
      }
      setOffersByLine(grouped);
      setPhase("comparing-offers");
    } else {
      setPhase("browse-bom");
      setSelectedBomLines(new Set());
      setOffers([]);
      setOffersByLine(new Map());
      setProposal(null);
      setConfirmedLines(new Set());
      setHandoff(null);
    }
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
      const sellerKey = offer.seller ?? offer.provider ?? "Unknown";
      const existing = groupedBySeller.get(sellerKey) ?? [];
      existing.push(offer);
      groupedBySeller.set(sellerKey, existing);
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
                          <strong>{offer.seller ?? offer.provider}</strong>
                          {expired && (
                            <span className="status-tag tone-bad">已过期</span>
                          )}
                        </header>

                        <div className="offer-details">
                          <div className="offer-price">
                            <span className="offer-price-value">
                              {offer.unit_price} {offer.currency}
                            </span>
                            <small>/ {offer.condition}</small>
                          </div>

                          <div className="offer-meta">
                            <div className="offer-meta-item">
                              <Package className="h-3 w-3" />
                              <span>库存: {offer.quantity_available}</span>
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
                          href={offer.product_url}
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
                            createProposal(offer, bomItem ?? bomItems[0])
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

        {/* Multi-seller: each offer is its own proposal, so no combined section */}
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

    const offerForProposal = offers.find((o) => o.id === proposal.offer_snapshot_id);

    return (
      <div className="shopping-view" role="region" aria-label="购买提案">
        <header className="shopping-header">
          <h2>
            <ShoppingCart className="h-5 w-5" />
            Purchase Proposal
          </h2>
          <div className="shopping-header-actions">
            <span
              className={`status-tag ${proposal.status === "ready" ? "tone-good" : proposal.status === "handed_off" ? "tone-good" : "tone-live"}`}
            >
              {proposal.status}
            </span>
            <Button variant="ghost" size="sm" onClick={resetShopping}>
              ← 返回 BOM
            </Button>
          </div>
        </header>

        <div className="proposal-meta">
          {offerForProposal && (
            <div className="proposal-meta-item">
              <small>卖家</small>
              <strong>{offerForProposal.seller}</strong>
            </div>
          )}
          <div className="proposal-meta-item">
            <small>总价</small>
            <strong>
              {proposal.unit_price} × {proposal.quantity} {proposal.currency}
            </strong>
            <small style={{ display: 'block', marginTop: '0.15rem', fontSize: '0.6rem', color: 'var(--ink-soft)' }}>
              max_total: {proposal.max_total}
            </small>
          </div>
          <div className="proposal-meta-item">
            <small>Solution Version</small>
            <code>{proposal.solution_version_id.slice(0, 16)}…</code>
          </div>
          <div className="proposal-meta-item">
            <small>创建时间</small>
            <span>
              {new Date(proposal.created_at).toLocaleString("zh-CN")}
            </span>
          </div>
        </div>

        {/* Single-offer detail */}
        <div className="proposal-lines">
          <div className="proposal-lines-header">
            <h3>明细</h3>
            {proposal.status === "draft" && confirmedLines.size === 0 && (
              <Button
                size="sm"
                onClick={() =>
                  confirmLines(
                    bomItems.map((b) => b.line_id)
                  )
                }
              >
                <CheckCircle2 className="h-3 w-3" />
                确认全部 BOM 行
              </Button>
            )}
          </div>

          <div className="proposal-lines-grid">
            {bomItems.map((item) => {
              const isConfirmed = confirmedLines.has(item.line_id);
              return (
                <div
                  className={`proposal-line-card ${isConfirmed ? "is-confirmed" : ""}`}
                  key={item.line_id}
                >
                  <div className="proposal-line-header">
                    <span
                      className={`status-tag ${isConfirmed ? "tone-good" : "tone-live"}`}
                    >
                      {isConfirmed ? "已确认" : "待确认"}
                    </span>
                    {!isConfirmed && proposal.status === "draft" && (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => confirmLines([item.line_id])}
                      >
                        确认此行
                      </Button>
                    )}
                  </div>

                  <strong>{bomLabel(item)}</strong>

                  <div className="proposal-line-detail">
                    <span>
                      {proposal.quantity} × {proposal.unit_price}{" "}
                      {proposal.currency}
                    </span>
                    {offerForProposal && (
                      <small>
                        {offerForProposal.seller} · {offerForProposal.condition} · {offerForProposal.region}
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
          confirmedLines.size === bomItems.length &&
          bomItems.length > 0 && (
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

        {/* Handoff result — handle ambiguous / null url (M2) */}
        {handoff && (
          <div className="checkout-handoff-card">
            {handoff.status === "dispatched" && handoff.checkout_url ? (
              <>
                <CheckCircle2 className="h-6 w-6 tone-good" />
                <div>
                  <strong>Checkout 已就绪</strong>
                  <p>Provider: {handoff.provider}</p>
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
              </>
            ) : (
              <>
                <AlertTriangle className="h-6 w-6 tone-live" />
                <div>
                  <strong>Checkout 状态异常</strong>
                  <p>
                    Provider: {handoff.provider} · Status: {handoff.status}
                  </p>
                  <p>
                    {handoff.status === "ambiguous"
                      ? "Provider 返回 ambiguous 状态，未提供可用的 checkout URL。请联系管理员或稍后重试。"
                      : "Checkout URL 尚未就绪。当前状态为 " + handoff.status + "。"}
                  </p>
                  <code>ID: {handoff.id}</code>
                </div>
                <Button variant="outline" size="sm" onClick={resetShopping}>
                  返回 BOM
                </Button>
              </>
            )}
          </div>
        )}
      </div>
    );
  }

  return null;
}
