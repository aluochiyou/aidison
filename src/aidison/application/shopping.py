"""Shopping application service — offer discovery, purchase proposals, checkout handoffs.

Follows the same idempotent-command pattern as ProjectApplication with
If-Match-based optimistic concurrency on the project revision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from pydantic import Field

from aidison.application.ports import DomainStore
from aidison.application.service import (
    DomainConflictError,
    DomainNotFoundError,
    PreconditionFailedError,
    ProjectApplication,
    canonical_hash,
)
from aidison.config import AidisonSettings
from aidison.domain.models import (
    CheckoutHandoff,
    CheckoutHandoffStatus,
    EffectApproval,
    EffectApprovalStatus,
    HandoffKind,
    OfferSnapshot,
    Project,
    PurchaseProposal,
    PurchaseProposalStatus,
    SolutionVersion,
)
from aidison.providers.shopping import (
    CartLineInput,
    OfferAvailability,
    ShoppingConfigError,
    ShoppingOffer,
    ShoppingProvider,
    ShoppingProviderError,
)

_VALID_REGIONS = frozenset({"CN", "US", "GB", "DE", "FR", "JP", "KR", "AU", "CA", "SG"})
_CREATE_CART_EFFECT = "shopping.create_cart"

# Effect kind depends on handoff kind
_EFFECT_KIND_FOR_HANDOFF = {
    HandoffKind.CART_REDIRECT: "shopping.create_cart",
    HandoffKind.PRODUCT_REDIRECT: "shopping.create_redirect",
}


class ShoppingSettings(AidisonSettings):
    yaml_section = "shopping"

    effect_approval_ttl_seconds: int = Field(default=900, ge=60, le=86_400)


def _validate_region(region: str) -> str:
    upper = region.strip().upper()
    if upper not in _VALID_REGIONS:
        raise DomainConflictError(f"unsupported region: {region}")
    return upper


class OfferSearchError(RuntimeError):
    """Wraps upstream offer search failure details (safe for API)."""


class CartCreationError(RuntimeError):
    """Wraps policy-violating cart creation failures (not ambiguous — hard fail)."""


@dataclass(frozen=True)
class _CheckoutEffectScope:
    project: Project
    proposal: PurchaseProposal
    offer: OfferSnapshot
    solution: SolutionVersion
    handoff_kind: HandoffKind
    basis_hash: str
    scope_hash: str
    constraints: dict[str, object]


def _compute_offer_snapshot_hash(
    project_id: UUID,
    solution_version_id: UUID,
    bom_line_id: str,
    offer: ShoppingOffer,
    observed: datetime,
) -> str:
    """Canonical hash binding offer identity fields."""
    return canonical_hash(
        project_id,
        solution_version_id,
        bom_line_id,
        offer.provider,
        offer.provider_offer_id,
        offer.merchandise_id,
        offer.seller,
        offer.title,
        offer.condition,
        offer.availability.value,
        offer.unit_price,
        offer.currency,
        offer.shipping_estimate,
        offer.tax_estimate,
        offer.region,
        offer.quantity_available,
        offer.product_url,
        observed,
        offer.expires_at,
    )


def _compute_proposal_basis_hash(
    project_id: UUID,
    solution_version_id: UUID,
    offer_snapshot_hash: str,
    quantity: int,
    region: str,
    currency: str,
    shipping_estimate: str | None,
    tax_estimate: str | None,
    max_total: str,
) -> str:
    """Canonical hash binding proposal request to immutable offer."""
    return canonical_hash(
        project_id,
        solution_version_id,
        offer_snapshot_hash,
        quantity,
        region,
        currency,
        shipping_estimate,
        tax_estimate,
        max_total,
    )


class ShoppingApplication:
    """Command-query service for V1 shopping closed loop."""

    def __init__(
        self,
        store: DomainStore,
        shopping_provider: ShoppingProvider,
        project_app: ProjectApplication | None = None,
        effect_approval_ttl_seconds: int = 900,
    ) -> None:
        if not 1 <= effect_approval_ttl_seconds <= 86_400:
            raise ValueError("effect approval TTL must be between 1 and 86400 seconds")
        self._store = store
        self._provider = shopping_provider
        self._project_app = project_app or ProjectApplication(store)
        self._effect_approval_ttl_seconds = effect_approval_ttl_seconds

    # ── Offer search (persists snapshots; uses If-Match) ─────────────

    async def search_offers(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        query: str,
        bom_line_id: str,
        region: str,
        max_results: int = 10,
    ) -> Sequence[OfferSnapshot]:
        """Search provider for offers, persist snapshots, return them.

        Validates bom_line_id belongs to the active SolutionVersion's BOM.
        """
        if not self._provider.available:
            raise ShoppingConfigError(f"provider {self._provider.name} is unavailable")

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id is None:
            raise DomainConflictError("shopping requires an active solution version")

        solution = await self._store.get_solution_version(project.active_solution_version_id)
        if solution is None:
            raise DomainNotFoundError("active solution version not found")

        # Validate bom_line_id belongs to the active SolutionVersion BOM
        bom_line_ids = {item.get("line_id", "") for item in solution.bom}
        if bom_line_id not in bom_line_ids:
            raise DomainConflictError(
                f"bom_line_id '{bom_line_id}' not found in active solution BOM"
            )

        try:
            offers = await self._provider.search_offers(
                query=query,
                region=region,
                max_results=max_results,
            )
        except ShoppingProviderError as exc:
            raise OfferSearchError(str(exc)) from exc
        except ShoppingConfigError:
            raise

        if not offers:
            return ()

        # Re-assert the revision after the external call.  The no-op update is
        # an atomic CAS in the PostgreSQL store and prevents snapshots from
        # being attached to a solution that changed while the provider was
        # responding.
        await self._store.update_project(
            project,
            expected_revision=expected_project_revision,
        )

        snapshots: list[OfferSnapshot] = []
        for offer in offers:
            if offer.provider != self._provider.name:
                raise DomainConflictError("provider returned an offer owned by another provider")
            observed = offer.observed_at or datetime.now(UTC)
            snapshot_hash = _compute_offer_snapshot_hash(
                project_id,
                project.active_solution_version_id,
                bom_line_id,
                offer,
                observed,
            )
            snap = OfferSnapshot(
                project_id=project_id,
                solution_version_id=project.active_solution_version_id,
                bom_line_id=bom_line_id,
                provider=offer.provider,
                provider_offer_id=offer.provider_offer_id,
                merchandise_id=offer.merchandise_id,
                seller=offer.seller,
                title=offer.title,
                condition=offer.condition,
                availability=offer.availability.value,
                unit_price=offer.unit_price,
                currency=offer.currency,
                shipping_estimate=offer.shipping_estimate,
                tax_estimate=offer.tax_estimate,
                region=offer.region,
                quantity_available=offer.quantity_available,
                product_url=offer.product_url,
                observed_at=observed,
                expires_at=offer.expires_at,
                snapshot_hash=snapshot_hash,
                provenance=f"{offer.provider}:search:{query[:200]}",
            )
            await self._store.add_offer_snapshot(snap)
            snapshots.append(snap)

        await self._store.commit()
        return tuple(snapshots)

    # ── Purchase proposal creation ────────────────────────────────────

    async def create_purchase_proposal(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        solution_version_id: UUID,
        offer_snapshot: OfferSnapshot,
        quantity: int,
        region: str,
        currency: str,
        shipping_estimate: str | None,
        tax_estimate: str | None,
        max_total: str,
        idempotency_key: str,
    ) -> PurchaseProposal:
        """Create a Draft PurchaseProposal bound to an exact offer snapshot."""
        region = _validate_region(region)

        # Validate proposal request matches the immutable offer
        self._validate_proposal_matches_offer(
            offer_snapshot, region, currency, shipping_estimate, tax_estimate
        )

        # Enforce max_total via Decimal
        try:
            unit_d = Decimal(offer_snapshot.unit_price)
            max_d = Decimal(max_total)
            total = unit_d * quantity
        except InvalidOperation as exc:
            raise DomainConflictError("invalid numeric format in price or max_total") from exc
        if total > max_d:
            raise DomainConflictError(f"total {total} exceeds max_total {max_d}")

        # Compute basis_hash that binds the solution, offer, and request
        basis_hash = _compute_proposal_basis_hash(
            project_id,
            solution_version_id,
            offer_snapshot.snapshot_hash,
            quantity,
            region,
            currency,
            shipping_estimate,
            tax_estimate,
            max_total,
        )

        # Include price/availability from the snapshot in the payload hash for idempotency
        payload_hash = canonical_hash(
            "create_purchase_proposal",
            project_id,
            expected_project_revision,
            solution_version_id,
            offer_snapshot.snapshot_hash,
            quantity,
            region,
            currency,
            shipping_estimate,
            tax_estimate,
            max_total,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            proposal = await self._store.get_purchase_proposal(UUID(receipt))
            if proposal is None:
                raise DomainConflictError("command receipt references a missing proposal")
            return proposal

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id != solution_version_id:
            raise PreconditionFailedError("solution version is not the active version")
        if offer_snapshot.solution_version_id != solution_version_id:
            raise PreconditionFailedError("offer snapshot belongs to another solution version")
        if offer_snapshot.provider != self._provider.name:
            raise DomainConflictError("offer snapshot belongs to another shopping provider")
        if offer_snapshot.availability != OfferAvailability.IN_STOCK.value:
            raise DomainConflictError("offer snapshot is not in stock")
        if quantity > offer_snapshot.quantity_available:
            raise DomainConflictError("requested quantity exceeds offer availability")

        # Verify the offer is still valid (not expired)
        if offer_snapshot.expires_at is not None and offer_snapshot.expires_at < datetime.now(UTC):
            raise DomainConflictError("offer snapshot has expired")

        proposal = PurchaseProposal(
            project_id=project_id,
            solution_version_id=solution_version_id,
            offer_snapshot_id=offer_snapshot.id,
            quantity=quantity,
            region=region,
            currency=currency,
            shipping_estimate=shipping_estimate,
            tax_estimate=tax_estimate,
            max_total=max_total,
            unit_price=offer_snapshot.unit_price,
            status=PurchaseProposalStatus.DRAFT,
            basis_hash=basis_hash,
            expires_at=offer_snapshot.expires_at,
        )

        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.add_purchase_proposal(proposal)
        await self._store.append_event(
            project_id,
            "purchase.proposed",
            {"proposal_id": str(proposal.id), "basis_hash": basis_hash},
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return proposal

    # ── Confirm lines ─────────────────────────────────────────────────

    async def confirm_proposal_lines(
        self,
        *,
        proposal_id: UUID,
        expected_proposal_basis: str,
        confirmed_line_ids: Sequence[str],
        expected_project_revision: int,
        idempotency_key: str,
    ) -> PurchaseProposal:
        """Confirm line items on a DRAFT proposal, moving it to READY.

        Rejects if active solution changed, offer expired, or basis mismatches.
        """
        payload_hash = canonical_hash(
            "confirm_proposal_lines",
            proposal_id,
            expected_proposal_basis,
            confirmed_line_ids,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            proposal = await self._store.get_purchase_proposal(UUID(receipt))
            if proposal is None:
                raise DomainConflictError("command receipt references a missing proposal")
            return proposal

        proposal = await self._store.get_purchase_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("purchase proposal not found")
        if proposal.status is not PurchaseProposalStatus.DRAFT:
            raise DomainConflictError("only draft proposals can be confirmed")

        # Recompute basis hash from stored proposal fields and verify match
        offer = await self._store.get_offer_snapshot(proposal.offer_snapshot_id)
        if offer is None:
            raise DomainNotFoundError("offer snapshot not found")
        recomputed_basis = _compute_proposal_basis_hash(
            proposal.project_id,
            proposal.solution_version_id,
            offer.snapshot_hash,
            proposal.quantity,
            proposal.region,
            proposal.currency,
            proposal.shipping_estimate,
            proposal.tax_estimate,
            proposal.max_total,
        )
        if recomputed_basis != expected_proposal_basis:
            raise PreconditionFailedError("proposal basis is stale (recomputed mismatch)")

        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")

        # Reject if active solution changed
        if project.active_solution_version_id != proposal.solution_version_id:
            raise PreconditionFailedError("active solution has changed since proposal was created")

        # Reject if offer expired
        if offer.expires_at is not None and offer.expires_at < datetime.now(UTC):
            raise DomainConflictError("offer snapshot has expired")

        confirmed = tuple(dict.fromkeys(confirmed_line_ids))
        if not confirmed:
            raise DomainConflictError("at least one line must be confirmed")

        # Validate line_ids against the offer's BOM line
        if offer.bom_line_id not in confirmed:
            raise DomainConflictError(
                f"confirmed line IDs must include the offer's bom_line_id '{offer.bom_line_id}'"
            )

        updated = proposal.model_copy(
            update={
                "status": PurchaseProposalStatus.READY,
                "confirmed_line_ids": confirmed,
            }
        )

        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": datetime.now(UTC)}
            ),
            expected_revision=project.revision,
        )
        await self._store.update_purchase_proposal(updated)
        await self._store.append_event(
            project.id,
            "proposal.lines_confirmed",
            {
                "proposal_id": str(proposal.id),
                "confirmed_line_ids": list(confirmed),
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(proposal.id))
        await self._store.commit()
        return updated

    # ── Checkout handoff ──────────────────────────────────────────────

    async def request_effect_approval(
        self,
        *,
        proposal_id: UUID,
        expected_proposal_basis: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> EffectApproval:
        payload_hash = canonical_hash(
            "request_effect_approval",
            proposal_id,
            expected_proposal_basis,
            expected_project_revision,
            self._provider.name,
            self._effect_approval_ttl_seconds,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            approval = await self._store.get_effect_approval(UUID(receipt))
            if approval is None:
                raise DomainConflictError("command receipt references a missing effect approval")
            return approval

        scope = await self._checkout_effect_scope(
            proposal_id=proposal_id,
            expected_proposal_basis=expected_proposal_basis,
            expected_project_revision=expected_project_revision,
        )
        now = datetime.now(UTC)
        live = await self._store.find_live_effect_approval(
            scope.project.id,
            scope.scope_hash,
        )
        if live is not None and live.expires_at <= now:
            expired = live.model_copy(
                update={
                    "status": EffectApprovalStatus.EXPIRED,
                    "resolved_at": now,
                    "resolution_reason": "approval TTL elapsed",
                }
            )
            await self._store.update_effect_approval(
                expired,
                expected_status=live.status,
            )
            await self._store.append_event(
                scope.project.id,
                "effect.approval_expired",
                {"approval_id": str(expired.id), "scope_hash": expired.scope_hash},
            )
            live = None
        if live is not None:
            raise DomainConflictError("an effect approval is already live for this scope")

        approval = EffectApproval(
            project_id=scope.project.id,
            effect_kind=_EFFECT_KIND_FOR_HANDOFF[scope.handoff_kind],
            target_ref=scope.proposal.id,
            basis_hash=scope.basis_hash,
            scope_hash=scope.scope_hash,
            constraints=scope.constraints,
            requested_at=now,
            expires_at=now + timedelta(seconds=self._effect_approval_ttl_seconds),
        )
        await self._store.update_project(
            scope.project.model_copy(
                update={
                    "revision": scope.project.revision + 1,
                    "updated_at": now,
                }
            ),
            expected_revision=scope.project.revision,
        )
        await self._store.add_effect_approval(approval)
        await self._store.append_event(
            scope.project.id,
            "effect.approval_requested",
            {
                "approval_id": str(approval.id),
                "effect_kind": approval.effect_kind,
                "target_ref": str(approval.target_ref),
                "scope_hash": approval.scope_hash,
            },
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(approval.id),
        )
        await self._store.commit()
        return approval

    async def resolve_effect_approval(
        self,
        *,
        approval_id: UUID,
        decision: EffectApprovalStatus,
        scope_hash: str,
        reason: str | None,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> EffectApproval:
        if decision not in {EffectApprovalStatus.APPROVED, EffectApprovalStatus.DENIED}:
            raise DomainConflictError("effect approval decision must be approved or denied")
        if decision is EffectApprovalStatus.DENIED and not (reason and reason.strip()):
            raise DomainConflictError("denied effect approval requires a reason")
        payload_hash = canonical_hash(
            "resolve_effect_approval",
            approval_id,
            decision,
            scope_hash,
            reason,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            approval = await self._store.get_effect_approval(UUID(receipt))
            if approval is None:
                raise DomainConflictError("command receipt references a missing effect approval")
            return approval

        approval = await self._store.get_effect_approval(approval_id)
        if approval is None:
            raise DomainNotFoundError("effect approval not found")
        project = await self._required_project(approval.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if approval.scope_hash != scope_hash:
            raise PreconditionFailedError("effect approval scope hash is stale")
        if approval.status is not EffectApprovalStatus.REQUESTED:
            raise DomainConflictError("only requested effect approvals can be resolved")

        now = datetime.now(UTC)
        resolved_status = EffectApprovalStatus.EXPIRED if approval.expires_at <= now else decision
        resolved_reason = (
            "approval TTL elapsed" if resolved_status is EffectApprovalStatus.EXPIRED else reason
        )
        resolved = approval.model_copy(
            update={
                "status": resolved_status,
                "resolved_at": now,
                "resolution_reason": resolved_reason,
            }
        )
        await self._store.update_effect_approval(
            resolved,
            expected_status=EffectApprovalStatus.REQUESTED,
        )
        await self._store.update_project(
            project.model_copy(update={"revision": project.revision + 1, "updated_at": now}),
            expected_revision=project.revision,
        )
        await self._store.append_event(
            project.id,
            "effect.approval_resolved",
            {
                "approval_id": str(approval.id),
                "status": resolved.status.value,
                "scope_hash": resolved.scope_hash,
            },
        )
        await self._store.save_command_receipt(
            idempotency_key,
            payload_hash,
            str(resolved.id),
        )
        await self._store.commit()
        return resolved

    async def create_checkout_handoff(
        self,
        *,
        proposal_id: UUID,
        effect_approval_id: UUID,
        expected_proposal_basis: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> CheckoutHandoff:
        """Create a provider-hosted cart and return a checkout URL.

        Idempotency: PREPARED handoff + command receipt is committed BEFORE
        the provider create_cart call. On replay/crash the stored PREPARED
        is returned and provider is never called twice.
        """
        if not self._provider.available:
            raise ShoppingConfigError(f"provider {self._provider.name} is unavailable")

        payload_hash = canonical_hash(
            "create_checkout_handoff",
            proposal_id,
            effect_approval_id,
            expected_proposal_basis,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            handoff = await self._store.get_checkout_handoff(UUID(receipt))
            if handoff is None:
                raise DomainConflictError("command receipt references a missing handoff")
            return handoff

        scope = await self._checkout_effect_scope(
            proposal_id=proposal_id,
            expected_proposal_basis=expected_proposal_basis,
            expected_project_revision=expected_project_revision,
        )
        approval = await self._store.get_effect_approval(effect_approval_id)
        if approval is None:
            raise DomainNotFoundError("effect approval not found")
        expected_effect_kind = _EFFECT_KIND_FOR_HANDOFF[scope.handoff_kind]
        if (
            approval.project_id != scope.project.id
            or approval.effect_kind != expected_effect_kind
            or approval.target_ref != scope.proposal.id
            or approval.basis_hash != scope.basis_hash
            or approval.scope_hash != scope.scope_hash
            or approval.constraints != scope.constraints
        ):
            raise PreconditionFailedError("effect approval does not match the checkout scope")
        now = datetime.now(UTC)
        if approval.expires_at <= now:
            raise DomainConflictError("effect approval has expired")
        if approval.status is not EffectApprovalStatus.APPROVED:
            raise DomainConflictError("effect approval is not approved")

        consumed = approval.model_copy(
            update={
                "status": EffectApprovalStatus.CONSUMED,
                "consumed_at": now,
            }
        )
        handoff = CheckoutHandoff(
            project_id=scope.proposal.project_id,
            proposal_id=scope.proposal.id,
            basis_hash=scope.basis_hash,
            provider=self._provider.name,
            handoff_kind=scope.handoff_kind,
            status=CheckoutHandoffStatus.PREPARED,
        )
        project_after_consumption = scope.project.model_copy(
            update={"revision": scope.project.revision + 1, "updated_at": now}
        )

        # ── Consume approval + persist PREPARED/receipt BEFORE provider call ───
        await self._store.update_effect_approval(
            consumed,
            expected_status=EffectApprovalStatus.APPROVED,
        )
        await self._store.add_checkout_handoff(handoff)
        await self._store.update_project(
            project_after_consumption,
            expected_revision=scope.project.revision,
        )
        await self._store.append_event(
            scope.project.id,
            "effect.approval_consumed",
            {
                "approval_id": str(consumed.id),
                "handoff_id": str(handoff.id),
                "scope_hash": consumed.scope_hash,
            },
        )
        await self._store.save_command_receipt(idempotency_key, payload_hash, str(handoff.id))
        await self._store.commit()

        # ── Attempt handoff ─────────────────────────────────────
        # On replay/crash the PREPARED stored above is returned directly
        # and provider is never called twice.
        now = datetime.now(UTC)
        proposal_updated = False
        try:
            if scope.handoff_kind is HandoffKind.CART_REDIRECT:
                cart_inputs = [
                    CartLineInput(
                        merchandise_id=scope.offer.merchandise_id or scope.offer.provider_offer_id,
                        quantity=scope.proposal.quantity,
                    )
                ]
                result = await self._provider.create_handoff(
                    kind=HandoffKind.CART_REDIRECT,
                    lines=cart_inputs,
                    region=scope.proposal.region,
                )
                cart = result.cart
                if cart is None:
                    raise CartCreationError("provider CART_REDIRECT produced no cart")
                if not cart.checkout_url.startswith("https://"):
                    raise CartCreationError("provider returned a non-HTTPS checkout URL")

                handoff = handoff.model_copy(
                    update={
                        "status": CheckoutHandoffStatus.DISPATCHED,
                        "provider_cart_id": cart.provider_cart_id,
                        "checkout_url": cart.checkout_url,
                        "dispatched_at": now,
                    }
                )
            elif scope.handoff_kind is HandoffKind.PRODUCT_REDIRECT:
                # Build a ShoppingOffer from the stored OfferSnapshot for the redirect
                offer_input = ShoppingOffer(
                    provider=self._provider.name,
                    provider_offer_id=scope.offer.provider_offer_id,
                    merchandise_id=scope.offer.merchandise_id,
                    seller=scope.offer.seller,
                    title=scope.offer.title,
                    condition=scope.offer.condition,
                    availability=OfferAvailability(scope.offer.availability),
                    unit_price=scope.offer.unit_price,
                    currency=scope.offer.currency,
                    shipping_estimate=scope.offer.shipping_estimate,
                    tax_estimate=scope.offer.tax_estimate,
                    region=scope.offer.region,
                    quantity_available=scope.offer.quantity_available,
                    product_url=scope.offer.product_url,
                    observed_at=scope.offer.observed_at,
                    expires_at=scope.offer.expires_at,
                )
                result = await self._provider.create_handoff(
                    kind=HandoffKind.PRODUCT_REDIRECT,
                    offer=offer_input,
                    region=scope.proposal.region,
                    quantity=scope.proposal.quantity,
                )
                redirect = result.redirect
                if redirect is None:
                    raise CartCreationError("provider PRODUCT_REDIRECT produced no redirect")

                handoff = handoff.model_copy(
                    update={
                        "status": CheckoutHandoffStatus.DISPATCHED,
                        "checkout_url": redirect.product_url,
                        "dispatched_at": now,
                    }
                )
            else:
                raise DomainConflictError(
                    f"unsupported handoff kind: {scope.handoff_kind.value}"
                )

            proposal = scope.proposal.model_copy(
                update={
                    "status": PurchaseProposalStatus.HANDED_OFF,
                    "handed_off_at": now,
                }
            )
            proposal_updated = True
        except CartCreationError:
            raise
        except ShoppingProviderError:
            handoff = handoff.model_copy(
                update={
                    "status": CheckoutHandoffStatus.AMBIGUOUS,
                    "resolved_at": now,
                }
            )

        # ── Persist final outcome ─────────────────────────────────────
        await self._store.update_checkout_handoff(handoff)
        if proposal_updated:
            await self._store.update_purchase_proposal(proposal)

        # The externally visible revision was already advanced in the
        # pre-provider transaction. This no-op CAS rejects concurrent project
        # changes without claiming a second revision for the same command.
        await self._store.update_project(
            project=project_after_consumption,
            expected_revision=project_after_consumption.revision,
        )
        await self._store.append_event(
            scope.project.id,
            "checkout.handoff_created",
            {
                "handoff_id": str(handoff.id),
                "proposal_id": str(scope.proposal.id),
                "status": handoff.status.value,
            },
        )
        await self._store.commit()
        return handoff

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _validate_proposal_matches_offer(
        snapshot: OfferSnapshot,
        region: str,
        currency: str,
        shipping_estimate: str | None,
        tax_estimate: str | None,
    ) -> None:
        """Reject a proposal request whose fields contradict the immutable offer."""
        mismatches: list[str] = []
        if region != snapshot.region:
            mismatches.append(f"region: request={region} offer={snapshot.region}")
        if currency != snapshot.currency:
            mismatches.append(f"currency: request={currency} offer={snapshot.currency}")
        ad_shipping = shipping_estimate or ""
        ad_offer_shipping = snapshot.shipping_estimate or ""
        if ad_shipping != ad_offer_shipping:
            mismatches.append("shipping_estimate mismatch")
        ad_tax = tax_estimate or ""
        ad_offer_tax = snapshot.tax_estimate or ""
        if ad_tax != ad_offer_tax:
            mismatches.append("tax_estimate mismatch")
        if mismatches:
            raise DomainConflictError(
                "proposal request does not match the offer snapshot: " + "; ".join(mismatches)
            )

    async def _required_project(self, project_id: UUID) -> Project:
        project = await self._store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        return project

    async def _checkout_effect_scope(
        self,
        *,
        proposal_id: UUID,
        expected_proposal_basis: str,
        expected_project_revision: int,
    ) -> _CheckoutEffectScope:
        proposal = await self._store.get_purchase_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("purchase proposal not found")
        if proposal.status is not PurchaseProposalStatus.READY:
            raise DomainConflictError("only ready proposals can create a checkout")
        offer = await self._store.get_offer_snapshot(proposal.offer_snapshot_id)
        if offer is None:
            raise DomainNotFoundError("offer snapshot not found")
        recomputed_basis = _compute_proposal_basis_hash(
            proposal.project_id,
            proposal.solution_version_id,
            offer.snapshot_hash,
            proposal.quantity,
            proposal.region,
            proposal.currency,
            proposal.shipping_estimate,
            proposal.tax_estimate,
            proposal.max_total,
        )
        if recomputed_basis != expected_proposal_basis:
            raise PreconditionFailedError("proposal basis is stale (recomputed mismatch)")
        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id != proposal.solution_version_id:
            raise PreconditionFailedError("active solution has changed since proposal was created")
        solution = await self._store.get_solution_version(proposal.solution_version_id)
        if solution is None:
            raise DomainNotFoundError("active solution version not found")
        if offer.solution_version_id != solution.id:
            raise PreconditionFailedError("offer snapshot belongs to another solution version")
        if offer.provider != self._provider.name:
            raise PreconditionFailedError("offer snapshot belongs to another shopping provider")
        if offer.expires_at is not None and offer.expires_at < datetime.now(UTC):
            raise DomainConflictError("offer snapshot has expired")

        # Determine handoff kind from provider capabilities.
        # A provider that supports only CART_REDIRECT uses that; one that
        # supports only PRODUCT_REDIRECT uses that.  Ambiguous or missing
        # capabilities default to CART_REDIRECT for backward compat.
        caps = self._provider.capabilities
        if HandoffKind.CART_REDIRECT in caps.handoff_kinds:
            handoff_kind = HandoffKind.CART_REDIRECT
        elif HandoffKind.PRODUCT_REDIRECT in caps.handoff_kinds:
            handoff_kind = HandoffKind.PRODUCT_REDIRECT
        else:
            raise DomainConflictError(
                f"provider {self._provider.name} supports no recognised handoff kind"
            )

        effect_kind = _EFFECT_KIND_FOR_HANDOFF[handoff_kind]

        constraints: dict[str, object] = {
            "provider": self._provider.name,
            "handoff_kind": handoff_kind.value,
            "effect_kind": effect_kind,
            "solution_version_id": str(solution.id),
            "solution_basis_hash": solution.basis_hash,
            "offer_snapshot_id": str(offer.id),
            "offer_snapshot_hash": offer.snapshot_hash,
            "provider_offer_id": offer.provider_offer_id,
            "merchandise_id": offer.merchandise_id,
            "quantity": proposal.quantity,
            "region": proposal.region,
            "currency": proposal.currency,
            "max_total": proposal.max_total,
            "confirmed_line_ids": list(proposal.confirmed_line_ids),
        }
        basis_hash = canonical_hash(
            proposal.basis_hash,
            offer.snapshot_hash,
            solution.basis_hash,
            self._provider.name,
            handoff_kind.value,
        )
        scope_hash = canonical_hash(
            effect_kind,
            project.id,
            proposal.id,
            handoff_kind.value,
            basis_hash,
            constraints,
        )
        return _CheckoutEffectScope(
            project=project,
            proposal=proposal,
            offer=offer,
            solution=solution,
            handoff_kind=handoff_kind,
            basis_hash=basis_hash,
            scope_hash=scope_hash,
            constraints=constraints,
        )
