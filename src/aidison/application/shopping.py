"""Shopping application service — offer discovery, purchase proposals, checkout handoffs.

Follows the same idempotent-command pattern as ProjectApplication with
If-Match-based optimistic concurrency on the project revision.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from aidison.application.ports import DomainStore
from aidison.application.service import (
    DomainConflictError,
    DomainNotFoundError,
    PreconditionFailedError,
    ProjectApplication,
    canonical_hash,
)
from aidison.domain.models import (
    CheckoutHandoff,
    CheckoutHandoffStatus,
    OfferSnapshot,
    Project,
    PurchaseProposal,
    PurchaseProposalStatus,
)
from aidison.providers.shopping import (
    CartLineInput,
    ShoppingConfigError,
    ShoppingOffer,
    ShoppingProvider,
    ShoppingProviderError,
)


class OfferSearchError(RuntimeError):
    """Wraps upstream offer search failure details (safe for API)."""


class CartCreationError(RuntimeError):
    """Wraps upstream cart creation failure details (safe for API)."""


class ShoppingApplication:
    """Command-query service for V1 shopping closed loop."""

    def __init__(
        self,
        store: DomainStore,
        shopping_provider: ShoppingProvider,
        project_app: ProjectApplication | None = None,
    ) -> None:
        self._store = store
        self._provider = shopping_provider
        self._project_app = project_app or ProjectApplication(store)

    # ── Offer search (query — not idempotent but read-only) ──────────

    async def search_offers(
        self,
        *,
        project_id: UUID,
        expected_project_revision: int,
        query: str,
        bom_line_id: str,
        region: str,
        max_results: int = 10,
    ) -> tuple[Sequence[OfferSnapshot], Sequence[ShoppingOffer]]:
        """Search provider for offers and return immutable OfferSnapshots.

        The provider result is an append-only observation; users pick an offer
        snapshot to anchor a PurchaseProposal.
        """
        if not self._provider.available:
            raise ShoppingConfigError(f"provider {self._provider.name} is unavailable")

        project = await self._required_project(project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")
        if project.active_solution_version_id is None:
            raise DomainConflictError("shopping requires an active solution version")

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
            return (), ()

        snapshots: list[OfferSnapshot] = []
        for offer in offers:
            observed = offer.observed_at or datetime.now(UTC)
            raw = offer.raw_provider_payload
            snapshot_hash = canonical_hash(
                project_id,
                project.active_solution_version_id,
                bom_line_id,
                offer.provider,
                offer.provider_offer_id,
                offer.title,
                offer.availability.value,
                offer.unit_price,
                offer.currency,
                offer.region,
                offer.quantity_available,
                offer.product_url,
                observed,
                raw,
            )
            snapshots.append(
                OfferSnapshot(
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
            )
        return tuple(snapshots), offers

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
        """Snapshot an offer and create a Draft PurchaseProposal."""
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

        solution = await self._store.get_solution_version(solution_version_id)
        if solution is None:
            raise DomainNotFoundError("solution version not found")

        # Verify the offer is still valid (not expired)
        if offer_snapshot.expires_at is not None and offer_snapshot.expires_at < datetime.now(UTC):
            raise DomainConflictError("offer snapshot has expired")

        # Persist the offer snapshot
        await self._store.add_offer_snapshot(offer_snapshot)

        basis_hash = canonical_hash(
            project_id,
            solution_version_id,
            offer_snapshot.snapshot_hash,
            quantity,
        )
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
        await self._store.save_command_receipt(
            idempotency_key, payload_hash, str(proposal.id)
        )
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

        confirmed_line_ids must be a non-empty subset of the BOM line IDs
        associated with this proposal (via the module/bom structure).
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
        if proposal.basis_hash != expected_proposal_basis:
            raise PreconditionFailedError("proposal basis is stale")

        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")

        confirmed = tuple(dict.fromkeys(confirmed_line_ids))
        if not confirmed:
            raise DomainConflictError("at least one line must be confirmed")

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
        await self._store.save_command_receipt(
            idempotency_key, payload_hash, str(proposal.id)
        )
        await self._store.commit()
        return updated

    # ── Checkout handoff ──────────────────────────────────────────────

    async def create_checkout_handoff(
        self,
        *,
        proposal_id: UUID,
        expected_proposal_basis: str,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> CheckoutHandoff:
        """Create a provider-hosted cart and return a checkout URL.

        The proposal must be READY.  The handoff moves through:
        PREPARED → DISPATCHED (cart created successfully)
        PREPARED → AMBIGUOUS (provider response is indeterminate)
        """
        if not self._provider.available:
            raise ShoppingConfigError(f"provider {self._provider.name} is unavailable")

        payload_hash = canonical_hash(
            "create_checkout_handoff",
            proposal_id,
            expected_proposal_basis,
            expected_project_revision,
        )
        receipt = await self._store.claim_command(idempotency_key, payload_hash)
        if receipt is not None:
            handoff = await self._store.get_checkout_handoff(UUID(receipt))
            if handoff is None:
                raise DomainConflictError("command receipt references a missing handoff")
            return handoff

        proposal = await self._store.get_purchase_proposal(proposal_id)
        if proposal is None:
            raise DomainNotFoundError("purchase proposal not found")
        if proposal.status is not PurchaseProposalStatus.READY:
            raise DomainConflictError("only ready proposals can create a checkout")
        if proposal.basis_hash != expected_proposal_basis:
            raise PreconditionFailedError("proposal basis is stale")

        project = await self._required_project(proposal.project_id)
        if project.revision != expected_project_revision:
            raise PreconditionFailedError("project revision is stale")

        offer = await self._store.get_offer_snapshot(proposal.offer_snapshot_id)
        if offer is None:
            raise DomainNotFoundError("offer snapshot not found")

        basis_hash = canonical_hash(proposal.id, proposal.basis_hash, offer.snapshot_hash)
        handoff = CheckoutHandoff(
            project_id=proposal.project_id,
            proposal_id=proposal.id,
            basis_hash=basis_hash,
            provider=self._provider.name,
            status=CheckoutHandoffStatus.PREPARED,
        )

        # Attempt cart creation
        now = datetime.now(UTC)
        try:
            cart_inputs = [
                CartLineInput(
                    merchandise_id=offer.merchandise_id or offer.provider_offer_id,
                    quantity=proposal.quantity,
                )
            ]
            cart = await self._provider.create_cart(lines=cart_inputs, region=proposal.region)

            # Validate checkout URL is HTTPS (defence-in-depth)
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
        except (ShoppingProviderError, CartCreationError):
            # Ambiguous: the cart may or may not have been created —
            # do NOT retry automatically; let the user/agent assess.
            handoff = handoff.model_copy(
                update={
                    "status": CheckoutHandoffStatus.AMBIGUOUS,
                    "resolved_at": now,
                }
            )
            # Still persist the handoff — fail-safe, no automatic retry
        except ShoppingConfigError:
            raise

        updated_proposal = proposal.model_copy(
            update={
                "status": PurchaseProposalStatus.HANDED_OFF,
                "handed_off_at": now,
            }
        )

        await self._store.update_project(
            project=project.model_copy(
                update={"revision": project.revision + 1, "updated_at": now}
            ),
            expected_revision=project.revision,
        )
        await self._store.add_checkout_handoff(handoff)
        await self._store.update_purchase_proposal(updated_proposal)
        await self._store.append_event(
            project.id,
            "checkout.handoff_created",
            {
                "handoff_id": str(handoff.id),
                "proposal_id": str(proposal.id),
                "status": handoff.status.value,
            },
        )
        await self._store.save_command_receipt(
            idempotency_key, payload_hash, str(handoff.id)
        )
        await self._store.commit()
        return handoff

    # ── helpers ────────────────────────────────────────────────────────

    async def _required_project(self, project_id: UUID) -> Project:
        project = await self._store.get_project(project_id)
        if project is None:
            raise DomainNotFoundError("project not found")
        return project
