"""Project-scoped conversation application layer.

Wiring-free business logic that coordinates:
- ConversationSession / Turn lifecycle
- Bounded context assembly (with total char budget + all active context)
- No-tool, low-budget JSON-mode model call
- Fallback assistant turn on model failure
- Idempotent user turn replay (deterministic fallback on crash)
- Action proposal acceptance with secondary schema validation + Command dispatch
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, cast
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from aidison.application.ports import DomainStore
from aidison.domain.models import (
    ContextSummary,
    ConversationActionProposal,
    ConversationActionProposalKind,
    ConversationActionProposalStatus,
    ConversationClarification,
    ConversationClarificationCoverageItem,
    ConversationClarificationKind,
    ConversationClarificationStatus,
    ConversationSession,
    ConversationTurn,
    ConversationTurnRole,
    ConversationTurnType,
    ProposedModule,
    ResearchStrategyProposal,
)

_logger = logging.getLogger(__name__)

FALLBACK_REPLY = "抱歉，我暂时无法回复。请稍后再试。"
_MAX_CONTEXT_CHARS = 8_000  # Hard budget per spec; future slice may derive from config
_MAX_CONTEXT_CANDIDATES = 80  # Bounded candidate ids so payloads reference real ids
# Two independent questions keep intake conversational while avoiding an
# artificial five-turn script. More than two reads like a form and can make a
# later answer obsolete after the first answer is received.
_MAX_INITIAL_CLARIFICATIONS_PER_RESPONSE = 2
_CONTEXT_SUMMARY_POLICY_VERSION = "context-summary-v1"
_INITIAL_REQUIREMENTS_DRAFT_MARKER = "_aidison_initial_requirements_draft"
_CLARIFICATION_KIND_ALIASES = {
    "acceptance_criteria": ConversationClarificationKind.REQUIREMENT.value,
    "context": ConversationClarificationKind.OTHER.value,
    "existing_resources": ConversationClarificationKind.RESOURCE.value,
    "feature": ConversationClarificationKind.REQUIREMENT.value,
    "knowledge": ConversationClarificationKind.SKILL.value,
    "purpose": ConversationClarificationKind.USAGE.value,
    "resources": ConversationClarificationKind.RESOURCE.value,
    "safety": ConversationClarificationKind.CONSTRAINT.value,
    "skill": ConversationClarificationKind.SKILL.value,
    "skill_and_time": ConversationClarificationKind.SKILL.value,
    "skills": ConversationClarificationKind.SKILL.value,
    "technical_constraint": ConversationClarificationKind.CONSTRAINT.value,
    "use_case": ConversationClarificationKind.USAGE.value,
    "usage_scenario": ConversationClarificationKind.USAGE.value,
}

# The five requirement facts used to judge whether an initial requirements
# draft has enough user-provided context. They are a coverage contract, not a
# fixed interview script: the model may infer several from the initial
# description and may ask two independent high-value questions in one turn.
_COVERAGE_ITEMS = (
    ConversationClarificationCoverageItem.USAGE,
    ConversationClarificationCoverageItem.BUDGET,
    ConversationClarificationCoverageItem.RESOURCES,
    ConversationClarificationCoverageItem.SKILL,
    ConversationClarificationCoverageItem.CONSTRAINTS,
)
_REQUIRED_INITIAL_COVERAGE_ITEMS = (
    ConversationClarificationCoverageItem.USAGE,
    ConversationClarificationCoverageItem.CONSTRAINTS,
)
_OPTIONAL_INITIAL_UNKNOWN_LABELS = {
    ConversationClarificationCoverageItem.BUDGET: "预算范围尚未确认",
    ConversationClarificationCoverageItem.RESOURCES: "可用资源尚未确认",
    ConversationClarificationCoverageItem.SKILL: "相关技能与能力边界尚未确认",
}
# Each coverage item maps to exactly one clarification kind, so a persisted
# clarification (pending/resolved) is the durable evidence for that item.
_COVERAGE_ITEM_KIND = {
    ConversationClarificationCoverageItem.USAGE: ConversationClarificationKind.USAGE,
    ConversationClarificationCoverageItem.BUDGET: ConversationClarificationKind.BUDGET,
    ConversationClarificationCoverageItem.RESOURCES: ConversationClarificationKind.RESOURCE,
    ConversationClarificationCoverageItem.SKILL: ConversationClarificationKind.SKILL,
    ConversationClarificationCoverageItem.CONSTRAINTS: ConversationClarificationKind.CONSTRAINT,
}
_KIND_TO_COVERAGE_ITEM = {kind: item for item, kind in _COVERAGE_ITEM_KIND.items()}
# The initial requirements draft is a form prefill of user-confirmed facts only.
# Module skeletons, candidate solutions and open research questions are research
# phase output and must never enter the draft. The clarified usage/budget/skill
# context fields are the explicit counterpart of the coverage items and are
# preserved; budget is only a requirement constraint here and never creates a
# SpendBudgetRevision or any other side effect.
_ALLOWED_INITIAL_DRAFT_KEYS = {
    "goal",
    "hard_constraints",
    "preferences",
    "available_resources",
    "usage_context",
    "budget_context",
    "skill_context",
    "unknowns",
    "summary",
}


def _command_hash(*parts: object) -> str:
    """Hash a canonical command basis without importing the project service layer."""
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


# ── Pydantic contracts ───────────────────────────────────────────────────────


class ConversationCoverageStatus(StrEnum):
    """A model-declared determination for one of the five requirement facts."""

    COVERED = "covered"
    MISSING = "missing"


class ConversationCoverageDeclaration(BaseModel):
    """Controlled, structured verdict of the five clarification coverage items.

    The model fills this in every pre-approval clarification round: covered when
    the item is explicit in the project description or the user already answered
    it, missing otherwise. It is the deterministic signal the server uses to
    decide whether the clarification rounds have converged, so a draft can never
    be emitted while a coverage item is still missing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage: ConversationCoverageStatus = ConversationCoverageStatus.MISSING
    budget: ConversationCoverageStatus = ConversationCoverageStatus.MISSING
    resources: ConversationCoverageStatus = ConversationCoverageStatus.MISSING
    skill: ConversationCoverageStatus = ConversationCoverageStatus.MISSING
    constraints: ConversationCoverageStatus = ConversationCoverageStatus.MISSING


class ConversationModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reply: str = Field(min_length=1, max_length=8_000)
    coverage: ConversationCoverageDeclaration | None = None
    clarifications: tuple[ConversationClarificationDraft, ...] = ()
    action_proposals: tuple[ConversationActionProposalDraft, ...] = ()


class ConversationClarificationDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)
    kind: ConversationClarificationKind = ConversationClarificationKind.OTHER


class ConversationActionProposalDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ConversationActionProposalKind
    summary: str = Field(min_length=1, max_length=4_000)
    proposed_payload: dict[str, Any] = Field(default_factory=dict)


def _normalize_clarification_kind(value: object) -> object:
    """Collapse model-only question labels into the frozen, non-authoritative vocabulary."""
    if not isinstance(value, str):
        return value
    normalized = "_".join(value.strip().lower().replace("-", " ").split())
    if normalized in {kind.value for kind in ConversationClarificationKind}:
        return normalized
    return _CLARIFICATION_KIND_ALIASES.get(
        normalized, ConversationClarificationKind.OTHER.value
    )


def _normalize_clarification_question(question: str) -> str:
    """Canonical form used to detect repeated clarification questions.

    NFKC folds full-width and compatible characters, then case-folds ASCII and
    drops every non-word character (spaces, punctuation, emoji), so semantically
    equivalent Chinese/English questions collapse onto one key. An all-punctuation
    or empty question normalizes to ``""`` and is treated as unaskable.
    """
    text = unicodedata.normalize("NFKC", question).strip().lower()
    return re.sub(r"\W+", "", text)


def _normalize_model_response_payload(payload: object) -> object:
    """Normalize only known clarification kind aliases and the coverage object.

    Both are pre-normalized because a model may emit free-form labels that the
    strict, frozen schema should not have to absorb: an unknown clarification
    kind collapses to ``other`` and an unknown/missing coverage item or value
    collapses to ``missing``.
    """
    if isinstance(payload, dict) and isinstance(payload.get("clarifications"), list):
        normalized_clarifications: list[object] = []
        for clarification in payload["clarifications"]:
            if isinstance(clarification, dict) and "kind" in clarification:
                normalized_clarifications.append(
                    {
                        **clarification,
                        "kind": _normalize_clarification_kind(clarification.get("kind")),
                    }
                )
            else:
                normalized_clarifications.append(clarification)
        payload = {**payload, "clarifications": normalized_clarifications}

    if isinstance(payload, dict) and isinstance(payload.get("coverage"), dict):
        normalized_coverage: dict[str, str] = {}
        for item in _COVERAGE_ITEMS:
            value = payload["coverage"].get(item.value)
            normalized_coverage[item.value] = (
                value
                if value
                in {
                    ConversationCoverageStatus.COVERED.value,
                    ConversationCoverageStatus.MISSING.value,
                }
                else ConversationCoverageStatus.MISSING.value
            )
        payload = {**payload, "coverage": normalized_coverage}

    return payload


def _parse_conversation_model_response(raw: str) -> ConversationModelResponse:
    """Validate model JSON after a narrow clarification-kind/coverage normalization."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ConversationModelResponse.model_validate_json(raw)
    return ConversationModelResponse.model_validate(_normalize_model_response_payload(payload))


# ── Bounded Context Composer ─────────────────────────────────────────────────


def _truncate_parts(parts: list[str], max_chars: int) -> list[str]:
    """Keep ordered context parts within an exact character budget."""
    kept: list[str] = []
    total = 0
    for part in parts:
        separator_chars = 2 if kept else 0
        remaining = max_chars - total - separator_chars
        if remaining <= 0:
            break
        if len(part) <= remaining:
            kept.append(part)
            total += separator_chars + len(part)
        else:
            if remaining == 1:
                kept.append("…")
            else:
                kept.append(part[: remaining - 1] + "…")
            break
    return kept


async def _build_conversation_context(
    store: DomainStore,
    project_id: UUID,
    session_id: UUID,
    max_turns: int = 20,
    max_chars: int = _MAX_CONTEXT_CHARS,
) -> str:
    """Assemble bounded context: domain + recent turns. Never fetches full history.

    Uses _MAX_CONTEXT_CHARS hard budget with truncation.
    Includes locks/modules even when blueprint is absent (the user has active state).
    """
    project = await store.get_project(project_id)
    req_id = project.active_requirement_revision_id if project else None
    blueprint_id = project.active_blueprint_id if project else None

    parts: list[str] = []

    if project is not None:
        parts.append(
            f"Project: {project.name} (stage={project.stage.value}, rev={project.revision})\n"
            f"Goal: {project.goal}"
        )

    if req_id is not None:
        requirement = await store.get_requirement_revision(req_id)
        if requirement is not None:
            parts.append(
                f"Requirements: goal={requirement.goal}\n"
                f"  hard_constraints={requirement.hard_constraints}\n"
                f"  preferences={requirement.preferences}\n"
                f"  available_resources={requirement.available_resources}\n"
                f"  usage_context={requirement.usage_context or '未记录'}\n"
                f"  budget_context={requirement.budget_context or '未记录'}\n"
                f"  skill_context={requirement.skill_context or '未记录'}\n"
                f"  unknowns={requirement.unknowns}"
            )
    else:
        # This is an important state distinction for the conversation model:
        # an initial requirements draft may be useful, but it is never an
        # approved requirement revision until the user submits the form.
        parts.append(
            "Requirements: none approved yet; continue clarification or propose a draft only."
        )
        # Durable coverage projection over the five requirement facts. The model
        # combines it with the project description to decide what still needs a
        # question and when a user-confirmed draft is safe to propose.
        persisted_coverage = await _coverage_from_persisted(store, session_id)
        coverage_lines = [
            f"  - {item.value}: {status}" for item, status in persisted_coverage.items()
        ]
        parts.append("Clarification coverage (persisted):\n" + "\n".join(coverage_lines))

    # Always pull modules + locks, regardless of blueprint presence
    modules = await store.list_modules(project_id)
    if modules:
        mod_summaries = [
            f"  - {m.name}({m.key})[{m.id}]: {m.responsibility} "
            f"(stage={m.stage.value}, deps={len(m.dependency_ids)})"
            for m in modules
        ]
        parts.append(f"Modules ({len(modules)}):\n" + "\n".join(mod_summaries))

    # Candidates carry stable ids so the model can reference change_selection
    # and scoped start_research targets without fabricating identifiers. They
    # are model input only — never a second source of truth.
    candidates = await store.list_candidates(project_id)
    if candidates:
        module_key_by_id = {module.id: module.key for module in modules}
        capped = candidates[:_MAX_CONTEXT_CANDIDATES]
        candidate_lines = [
            f"  - {module_key_by_id.get(candidate.module_id, '?')}::{candidate.id}: "
            f"{candidate.name}"
            for candidate in capped
        ]
        parts.append(f"Candidates ({len(capped)}):\n" + "\n".join(candidate_lines))

    active_locks = [lock for lock in await store.list_selection_locks(project_id) if lock.active]
    if active_locks:
        lock_strs = [
            f"  - module={lock.module_id} candidate={lock.candidate_id}: {lock.reason}"
            for lock in active_locks
        ]
        parts.append(f"Active SelectionLocks ({len(active_locks)}):\n" + "\n".join(lock_strs))

    if blueprint_id is not None:
        blueprint = await store.get_project_blueprint(blueprint_id)
        if blueprint is not None:
            parts.append(
                f"Blueprint: v{blueprint.version}, {len(blueprint.module_ids)} modules, "
                f"{len(blueprint.dependency_edges)} edges"
            )

    # Recent solution snapshots (max 5, bounded summary only)
    snapshots = await store.list_solution_snapshots(project_id)
    if snapshots:
        recent_snaps = sorted(
            snapshots,
            key=lambda snapshot: (snapshot.created_at, str(snapshot.id)),
            reverse=True,
        )[:5]
        lines = [
            f"  - {s.id}: {s.label}"
            f"（{s.created_at.strftime('%Y-%m-%d %H:%M UTC')}"
            f"{', 恢复目标' if s.blueprint_id == blueprint_id else ''}）"
            for s in recent_snaps
        ]
        parts.append(f"Recent snapshots ({len(recent_snaps)}):\n" + "\n".join(lines))

    # Active spend budget (minimal: one line, no injection of secrets or state)
    active_budget = await store.get_active_spend_budget(project_id)
    if active_budget is not None:
        parts.append(
            f"Budget: {active_budget.currency} {active_budget.amount}"
            f" (rev {active_budget.revision})"
        )
    else:
        parts.append("Budget: 未设预算")

    # Derived summaries replace only their explicitly covered, immutable turn
    # ranges.  They are project/session constrained by the store and remain
    # derived model input rather than canonical project facts.
    summaries = await store.list_active_context_summaries(project_id, session_id)
    covered_sequences: set[int] = set()
    for summary in summaries:
        covered_sequences.update(
            range(summary.source_start_sequence, summary.source_end_sequence + 1)
        )
        parts.append(
            "Context summary "
            f"(turns {summary.source_start_sequence}-{summary.source_end_sequence}): "
            f"{summary.content}"
        )

    # Recent N un-covered turns.  Original rows remain in PostgreSQL and can
    # always be reassembled if a summary is tombstoned or invalidated.
    all_turns = await store.list_conversation_turns(session_id, limit=max_turns * 2)
    recent = all_turns[-max_turns:]
    for t in recent:
        if t.sequence in covered_sequences:
            continue
        role_label = "User" if t.role is ConversationTurnRole.USER else "Assistant"
        parts.append(f"{role_label}: {t.content}")

    # Truncate to char budget
    truncated = _truncate_parts(parts, max_chars)
    return "\n\n".join(truncated)


# ── Clarification coverage ───────────────────────────────────────────────────


def _coverage_item_for_kind(
    kind: ConversationClarificationKind,
) -> ConversationClarificationCoverageItem | None:
    return _KIND_TO_COVERAGE_ITEM.get(kind)


async def _coverage_from_persisted(
    store: DomainStore, session_id: UUID
) -> dict[ConversationClarificationCoverageItem, str]:
    """Derive coverage status for the five requirement facts from persisted state.

    The durable evidence lives in the clarification rows: a pending clarification
    makes the item ``pending``, a resolved one marks it ``covered``. Items with no
    clarification stay ``missing`` until the model declares them covered from the
    project description.
    """
    items = {item: "missing" for item in _COVERAGE_ITEMS}
    clarifications = await store.list_clarifications(session_id)
    for clarification in clarifications:
        item = _coverage_item_for_kind(clarification.kind)
        if item is None:
            continue
        if clarification.status is ConversationClarificationStatus.PENDING:
            items[item] = "pending"
        elif (
            clarification.status is ConversationClarificationStatus.RESOLVED
            and items[item] != "pending"
        ):
            items[item] = "covered"
    return items


def _merge_coverage(
    declared: ConversationCoverageDeclaration | None,
    persisted: dict[ConversationClarificationCoverageItem, str],
) -> dict[ConversationClarificationCoverageItem, str]:
    """Combine the model's declaration with the persisted evidence.

    A pending question always blocks the item (unanswered evidence wins), a
    resolved question or a covered declaration satisfies it, otherwise it stays
    missing. The ``covered by project description`` case is exactly the one where
    the declaration satisfies an item that has no resolved clarification yet.
    """
    merged: dict[ConversationClarificationCoverageItem, str] = {}
    for item in _COVERAGE_ITEMS:
        persisted_status = persisted.get(item, "missing")
        if persisted_status == "pending":
            merged[item] = "pending"
            continue
        declared_status = getattr(declared, item.value, None) if declared is not None else None
        if persisted_status == "covered" or declared_status is ConversationCoverageStatus.COVERED:
            merged[item] = "covered"
        else:
            merged[item] = "missing"
    return merged


def _missing_coverage_items(
    merged: dict[ConversationClarificationCoverageItem, str],
) -> set[ConversationClarificationCoverageItem]:
    return {item for item, status in merged.items() if status == "missing"}


def _required_initial_coverage_is_complete(
    merged: dict[ConversationClarificationCoverageItem, str],
) -> bool:
    """Keep intake useful without forcing every project through a fixed form."""

    return all(merged[item] == "covered" for item in _REQUIRED_INITIAL_COVERAGE_ITEMS)


def _initial_optional_unknowns(
    merged: dict[ConversationClarificationCoverageItem, str],
) -> tuple[str, ...]:
    """Derive optional intake unknowns from coverage, not model prose."""

    return tuple(
        label
        for item, label in _OPTIONAL_INITIAL_UNKNOWN_LABELS.items()
        if merged[item] == "missing"
    )


def _sanitize_initial_draft(
    drafts: Sequence[ConversationActionProposalDraft],
    *,
    optional_unknowns: tuple[str, ...],
) -> tuple[ConversationActionProposalDraft, ...]:
    """Strip research-phase content from an initial requirements draft.

    The initial draft is a form prefill of user-confirmed facts only. Module
    skeletons, candidate solutions and open research questions are produced by
    the research phase after plan approval, so this whitelist removes them
    server-side even when a model emits them.
    """
    sanitized: list[ConversationActionProposalDraft] = []
    for draft in drafts:
        if draft.kind is not ConversationActionProposalKind.REWRITE_REQUIREMENTS:
            continue
        payload = {
            key: value
            for key, value in draft.proposed_payload.items()
            if key in _ALLOWED_INITIAL_DRAFT_KEYS
        }
        payload.setdefault("goal", "")
        payload.setdefault("hard_constraints", [])
        payload.setdefault("preferences", [])
        payload.setdefault("available_resources", [])
        payload.setdefault("usage_context", "")
        payload.setdefault("budget_context", "")
        payload.setdefault("skill_context", "")
        # Unknowns are a server-derived coverage projection, not unverified
        # model content.  The user can still edit them in the requirements form.
        payload["unknowns"] = optional_unknowns
        sanitized.append(draft.model_copy(update={"proposed_payload": payload}))
    return tuple(sanitized)


# ── Model invocation ─────────────────────────────────────────────────────────


def _build_conversation_system_prompt() -> str:
    return (
        "You are Aidison's project conversation assistant. You have no tools, no write access, "
        "and no domain authority. Your job is to help the user clarify requirements, explore "
        "options, and surface unknowns.\n\n"
        "Return exactly ONE JSON object matching the ConversationModelResponse schema. "
        "reply is your natural-language response (required). "
        "In the pre-approval clarification rounds, coverage is required: an object with "
        "usage/budget/resources/skill/constraints each set to 'covered' or 'missing'; you "
        "decide it deterministically BEFORE composing the question. "
        "clarifications is a list of questions you need answered (each has question + kind); "
        "in the pre-approval clarification rounds, list at most two independent, "
        "high-information questions. "
        "action_proposals is a list of concrete actions you suggest the user take "
        "(each has kind, summary, and proposed_payload which is the payload the user would"
        "need to confirm for the action to execute). "
        "Do not include chain-of-thought, thinking, or reasoning text anywhere; reply with "
        "the JSON object only.\n\n"
        "For add_module, proposed_payload must contain module={key, name, responsibility, "
        "dependency_keys?, acceptance?, open_questions?}; it creates a separate reshape "
        "proposal for later review. For reshape_project, proposed_payload must contain "
        "target_goal, summary, affected_module_ids, and new_modules with the same module "
        "shape. It may additionally contain dependency_edges: a complete list of "
        "{source_module_id, target_module_id} UUID pairs copied from the Modules context. "
        "Use dependency_edges only when the user explicitly asks to change module order or "
        "relationships. It also creates a separate reshape proposal for later review.\n\n"
        "For start_research, proposed_payload may contain objective, an optional "
        "module_ids (a list of module UUID strings copied verbatim from the "
        "context Modules section). Omit module_ids to research the whole project; "
        "include only the specific module ids when the user asks to re-research one "
        "or more modules. It may also contain max_concurrency (an integer from 1 to "
        "16), max_token_budget (a positive integer no greater than 1000000000), "
        "max_duration_seconds (an integer from 60 to 604800), "
        "requires_independent_verification (a boolean), research_depth "
        "(focused, standard, or deep), and source_strategy "
        "(primary, independent, official, or mixed). Include these only when the "
        "user explicitly asks to change research depth, cost, speed, or verification. "
        "Accepting it creates an approval-gated execution plan; it "
        "NEVER starts a runtime job, tool call, or model research run by itself.\n\n"
        "For rewrite_requirements, proposed_payload must contain goal, hard_constraints?, "
        "preferences?, available_resources?, unknowns?, and modules=[{key, name, "
        "responsibility, dependency_keys?, acceptance?, open_questions?}]. When there are "
        "already approved requirements, accepting it creates a separate, still-unapplied "
        "requirements-change proposal for the user to review and apply. It NEVER approves "
        "requirements directly. In the pre-approval clarification rounds the initial draft "
        "is the exception: its payload must contain ONLY goal, hard_constraints, "
        "preferences, available_resources, usage_context, budget_context and skill_context "
        "— never modules, module skeletons, candidate solutions, or open research "
        "questions. Module boundaries are decided "
        "during the research phase after the plan is approved. For change_selection, "
        "proposed_payload must contain module_id and candidate_id; accepting it records the "
        "user's intent as an adjustment and produces a read-only impact preview.\n\n"
        "When the project has no approved requirements yet, you are in the clarification "
        "rounds. Track five requirement facts, each with "
        "its own clarification kind: usage (用途/使用场景, kind=usage), budget (预算或成本范围, "
        "kind=budget), resources (已有资源, kind=resource), skill (相关知识/技能水平, "
        "kind=skill), constraints (硬性约束与安全边界, kind=constraint). Usage and constraints "
        "are required before an initial draft; budget, resources and skill are optional unknowns "
        "unless they materially change feasibility for THIS project. FIRST "
        "determine coverage by re-reading the Clarification coverage (persisted) section and "
        "the project description: mark an item 'covered' when the user already answered it "
        "or the project description already states it explicitly, otherwise mark it "
        "'missing'; emit that verdict as the coverage object. THEN ask one natural-language "
        "question by default; ask two only when both missing items are independent and answering "
        "one would not change the other. Do not ask an optional category merely because it is "
        "blank. Choose the highest-information-value items for THIS "
        "project, with the matching kinds. Never ask about an item already "
        "covered by the description or an earlier answer, and never repeat a question that "
        "was already asked or answered in earlier turns. When usage and constraints are required "
        "and covered, you may stop asking and instead return exactly one rewrite_requirements "
        "draft as the only action_proposal, containing ONLY "
        "user-confirmed goal, hard_constraints, preferences and available_resources. "
        "The user will edit and explicitly confirm that draft in the requirements form "
        "before any project fact is written. Never combine a clarification and an "
        "initial rewrite_requirements draft in the same reply.\n\n"
        "NEVER generate action_proposals that bypass the user's explicit approval. "
        "NEVER propose to overwrite selection locks or final solution versions. "
        "If the user asks to do something that requires budget/plan authorization, "
        "suggest an execution plan proposal instead.\n\n"
        "For change_spend_budget, proposed_payload MUST contain only amount "
        '(a decimal string like "800.00"), currency (3-letter ISO 4217 code), '
        "and summary (a short explanation). Accepting this proposal creates a "
        "SpendBudgetProposal — it NEVER applies the budget, triggers research, "
        "recommends products, starts a job, or rewrites any project fact. "
        "The user must separately approve the proposal before the budget takes effect.\n\n"
        "For restore_solution_snapshot, proposed_payload MUST contain only "
        "snapshot_id (the UUID of a project snapshot from the context). "
        "Accepting it restores the module configuration to that snapshot's state. "
        "Only suggest this when the user explicitly requests it AND a matching "
        "snapshot exists in context; if the user is vague or no snapshot matches, "
        "ask a clarification instead. NEVER invent snapshot IDs.\n\n"
        "Intent mapping (always produce the governed proposal, never a direct "
        "Project edit):\n"
        "- 're-research one or more modules' (e.g. '重新研究 X 模块', '再调研一次') "
        "-> start_research with module_ids containing exactly those module ids and "
        "an optional objective.\n"
        "- 'adjust the project spend budget' (e.g. '预算改为 800 元') "
        "-> change_spend_budget.\n"
        "- 'adjust constraints or preferences' (e.g. '加一条硬约束', '放宽技术限制') "
        "-> rewrite_requirements with the full goal + modules list and the updated "
        "hard_constraints / preferences. It still creates a reviewable change "
        "proposal, never a direct requirement rewrite.\n"
        "- 'change a module selection' (e.g. '改用候选 X') -> change_selection with "
        "module_id and candidate_id copied verbatim from the context Candidates "
        "section.\n"
        "- 'make module A depend on module B / remove a module dependency' -> "
        "reshape_project with the complete dependency_edges list using only module UUIDs "
        "from context.\n"
        "- 'research the whole project' -> start_research without module_ids.\n"
        "- 'use 12000 tokens / run two tasks concurrently / cross-check the result / "
        "perform deep research / prefer official sources' "
        "-> start_research with max_token_budget / max_duration_seconds / max_concurrency / "
        "requires_independent_verification / research_depth / source_strategy respectively.\n\n"
        "Never fabricate UUIDs: use only module ids and candidate ids listed in the "
        "context. If an intent cannot be mapped to a supported action — for example "
        "directly rewriting a frozen solution, deleting a module, purchasing, "
        "starting actual execution without an approved plan, or bypassing an active "
        "selection lock — do NOT emit an action_proposal. Instead return a "
        "clarification that explains the governed alternative and what the user "
        "must approve. Never reply with a silent no-op: every reply either proposes "
        "a concrete governed action or asks a specific clarifying question."
    )


async def _invoke_conversation_model(
    model: BaseChatModel,
    system_prompt: str,
    context: str,
    user_message: str,
) -> ConversationModelResponse:
    json_model = model.bind(response_format={"type": "json_object"})
    response = await json_model.ainvoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{context}\n\nUser says: {user_message}"},
        ]
    )
    content = response.content
    if isinstance(content, list):
        raw = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    else:
        raw = str(content)
    return _parse_conversation_model_response(raw.strip())


# ── Application ──────────────────────────────────────────────────────────────

StoreFactory = Callable[[], DomainStore]
ModelFactory = Callable[[], BaseChatModel]
ResearchStrategyPlanner = Callable[..., Awaitable[ResearchStrategyProposal]]


class ConversationApplication:
    def __init__(
        self,
        *,
        store_factory: StoreFactory,
        model_factory: ModelFactory,
        research_strategy_planner: ResearchStrategyPlanner | None = None,
        max_turns: int = 20,
    ) -> None:
        self._store_factory = store_factory
        self._model_factory = model_factory
        self._research_strategy_planner = research_strategy_planner
        self._max_turns = max_turns

    async def ensure_active_session(self, project_id: UUID) -> ConversationSession:
        store = self._store_factory()
        active = await store.get_active_conversation_session(project_id)
        if active is not None:
            return active
        session = ConversationSession(project_id=project_id)
        try:
            await store.add_conversation_session(session)
            return session
        except IntegrityError:
            # PostgreSQL's partial unique index is the concurrency authority.
            # A competing request may have created the active session after our
            # read; clear this failed transaction and reuse the winner.
            await store.rollback()
            active = await store.get_active_conversation_session(project_id)
            if active is not None:
                return active
            raise

    async def record_context_summary(
        self,
        *,
        project_id: UUID,
        session_id: UUID,
        source_start_sequence: int,
        source_end_sequence: int,
        content: str,
        summarizer_profile: str,
        idempotency_key: str,
    ) -> ContextSummary:
        """Persist a bounded, append-only summary over one verified turn range.

        This is an internal application seam for an approved summarization
        worker.  It intentionally does not expose a free-form browser write
        endpoint: a summary is derived context, not a user-editable Project
        fact.  The source turn rows are never changed or deleted.
        """

        store = self._store_factory()
        if await store.get_project(project_id) is None:
            raise ConversationNotFoundError("project not found")
        session = await store.get_conversation_session(session_id)
        if session is None or session.project_id != project_id:
            raise ConversationNotFoundError("conversation session not found")
        if source_start_sequence < 1 or source_end_sequence < source_start_sequence:
            raise ConversationConflictError("context summary source range is invalid")

        source_turns = await store.list_conversation_turns(
            session_id,
            after=source_start_sequence - 1,
            limit=source_end_sequence - source_start_sequence + 1,
        )
        expected_sequences = tuple(range(source_start_sequence, source_end_sequence + 1))
        if tuple(turn.sequence for turn in source_turns) != expected_sequences:
            raise ConversationConflictError("context summary source turns are unavailable")

        basis_hash = _command_hash(
            _CONTEXT_SUMMARY_POLICY_VERSION,
            project_id,
            session_id,
            tuple((turn.id, turn.sequence, turn.content) for turn in source_turns),
        )
        payload_hash = _command_hash(
            "record_context_summary",
            project_id,
            session_id,
            source_start_sequence,
            source_end_sequence,
            basis_hash,
            content,
            summarizer_profile,
        )
        replay = await store.claim_command(idempotency_key, payload_hash)
        if replay is not None:
            summary = await store.get_context_summary(UUID(replay.removeprefix("context_summary:")))
            if summary is None:
                raise ConversationConflictError("context summary receipt is missing its summary")
            return summary

        existing = await store.find_context_summary(
            session_id=session_id,
            source_start_sequence=source_start_sequence,
            source_end_sequence=source_end_sequence,
            basis_hash=basis_hash,
        )
        if existing is not None:
            if existing.content != content or existing.summarizer_profile != summarizer_profile:
                raise ConversationConflictError(
                    "context summary source range is already summarized"
                )
            await store.save_command_receipt(
                idempotency_key,
                payload_hash,
                f"context_summary:{existing.id}",
            )
            await store.commit()
            return existing

        source_event_cursor = await store.get_project_event_sequence(project_id)
        summary = ContextSummary(
            project_id=project_id,
            session_id=session_id,
            source_start_sequence=source_start_sequence,
            source_end_sequence=source_end_sequence,
            source_event_cursor=source_event_cursor,
            basis_hash=basis_hash,
            content=content,
            summarizer_profile=summarizer_profile,
        )
        await store.add_context_summary(summary)
        await store.append_event(
            project_id,
            "conversation.context_summary_recorded",
            {
                "summary_id": str(summary.id),
                "session_id": str(session_id),
                "source_start_sequence": source_start_sequence,
                "source_end_sequence": source_end_sequence,
                "source_event_cursor": source_event_cursor,
            },
        )
        await store.save_command_receipt(
            idempotency_key,
            payload_hash,
            f"context_summary:{summary.id}",
        )
        await store.commit()
        return summary

    # ── post_message ──────────────────────────────────────────────────────

    async def post_message(
        self,
        *,
        project_id: UUID,
        content: str,
        idempotency_key: str,
        session_id: UUID | None = None,
    ) -> dict[str, Any]:
        store = self._store_factory()
        if await store.get_project(project_id) is None:
            raise ConversationNotFoundError("project not found")
        if session_id is None:
            session = await self.ensure_active_session(project_id)
        else:
            requested_session = await store.get_conversation_session(session_id)
            if requested_session is None or requested_session.project_id != project_id:
                raise ConversationNotFoundError("conversation session not found")
            if requested_session.status.value != "active":
                raise ConversationConflictError("conversation session is closed")
            session = requested_session

        # Serialize the durable user-turn write. The lock is intentionally
        # released before the model call, so a slow provider never blocks a
        # whole conversation; each assistant write is independently serialized
        # and linked to the turn it answers.
        await store.lock_conversation_session(session.id)

        # Idempotency must be checked *after* acquiring the session lock. Two
        # same-key requests can otherwise both observe an empty row and race on
        # the unique index.
        existing_turn = await store.get_conversation_turn_by_idempotency(
            session.id, idempotency_key
        )
        if existing_turn is not None:
            return await self._replay_existing(store, project_id, session.id, existing_turn)

        # Write user turn
        next_seq = (await store.get_latest_turn_sequence(session.id)) + 1
        user_turn = ConversationTurn(
            session_id=session.id,
            sequence=next_seq,
            role=ConversationTurnRole.USER,
            content=content,
            idempotency_key=idempotency_key,
        )
        await store.add_conversation_turn(user_turn)
        await store.append_event(
            project_id,
            "conversation.user_message_recorded",
            {"session_id": str(session.id), "turn_id": str(user_turn.id)},
        )
        # Phase one is durable before any provider call. This releases the
        # request transaction and preserves a replayable user fact if a model
        # provider is slow, unavailable, or the process crashes afterwards.
        await store.commit()

        # A clarification batch is an explicit conversation contract. A free
        # message must not silently create a second model turn while its
        # questions remain unanswered: it would spend tokens and could change
        # direction before the user has supplied the requested facts. The UI
        # exposes the pending question cards, so return a deterministic notice
        # instead of treating arbitrary free text as an answer.
        pending = await store.list_active_clarifications(session.id)
        if pending:
            return await self._write_pending_clarification_notice(
                store,
                project_id=project_id,
                session_id=session.id,
                user_turn=user_turn,
                pending_count=len(pending),
            )

        # Read a bounded context in its own short transaction, then release it
        # before awaiting the model provider.
        try:
            context_text = await _build_conversation_context(
                store, project_id, session.id, max_turns=self._max_turns
            )
            await store.commit()
            response = await _invoke_conversation_model(
                self._model_factory(),
                _build_conversation_system_prompt(),
                context_text,
                content,
            )
        except Exception:
            _logger.warning("conversation model call failed", exc_info=True)
            return await self._write_fallback(store, project_id, session.id, user_turn)

        assistant_turn = await self._persist_model_response(
            store,
            project_id,
            session.id,
            response,
            in_reply_to_turn_id=user_turn.id,
        )
        return {
            "user_turn": user_turn,
            "assistant_turn": assistant_turn,
            "session_id": str(session.id),
        }

    async def _replay_existing(
        self,
        store: DomainStore,
        project_id: UUID,
        session_id: UUID,
        user_turn: ConversationTurn,
    ) -> dict[str, Any]:
        """Replay an existing user turn.

        If there is already an assistant turn after it → return both.
        Otherwise (crash / model failure window) → write deterministic fallback.
        """
        assistant_turn = await store.get_conversation_reply_to_turn(user_turn.id)
        if assistant_turn is not None:
            return {
                "user_turn": user_turn,
                "assistant_turn": assistant_turn,
                "session_id": str(session_id),
            }
        # crash window: user wrote, model failed → write fallback now
        return await self._write_fallback(store, project_id, session_id, user_turn)

    async def _write_fallback(
        self,
        store: DomainStore,
        project_id: UUID,
        session_id: UUID,
        user_turn: ConversationTurn,
    ) -> dict[str, Any]:
        await store.lock_conversation_session(session_id)
        existing = await store.get_conversation_reply_to_turn(user_turn.id)
        if existing is not None:
            return {
                "user_turn": user_turn,
                "assistant_turn": existing,
                "session_id": str(session_id),
            }
        next_seq = (await store.get_latest_turn_sequence(session_id)) + 1
        fallback = ConversationTurn(
            session_id=session_id,
            sequence=next_seq,
            role=ConversationTurnRole.ASSISTANT,
            content=FALLBACK_REPLY,
            model_profile="fallback",
            turn_type=ConversationTurnType.MESSAGE,
            in_reply_to_turn_id=user_turn.id,
            is_fallback=True,
        )
        await store.add_conversation_turn(fallback)
        await store.append_event(
            project_id,
            "conversation.assistant_reply_ready",
            {
                "session_id": str(session_id),
                "turn_id": str(fallback.id),
                "fallback": True,
            },
        )
        return {"user_turn": user_turn, "assistant_turn": fallback, "session_id": str(session_id)}

    async def _write_pending_clarification_acknowledgement(
        self,
        store: DomainStore,
        *,
        project_id: UUID,
        session_id: UUID,
        user_turn: ConversationTurn,
        pending_count: int,
    ) -> dict[str, Any]:
        """Acknowledge one answer in a clarification batch without another LLM call.

        The remaining questions were already proposed from the same bounded
        context. Calling the model after each answer both costs a turn and can
        create a misleading new direction before the batch is complete. The
        final answer still resumes the normal model path, where it can create a
        draft or ask a newly-informed next question.
        """

        if pending_count < 1:
            raise ValueError("clarification acknowledgement requires a pending question")
        await store.lock_conversation_session(session_id)
        existing = await store.get_conversation_reply_to_turn(user_turn.id)
        if existing is not None:
            return {
                "user_turn": user_turn,
                "assistant_turn": existing,
                "session_id": str(session_id),
            }
        next_seq = (await store.get_latest_turn_sequence(session_id)) + 1
        acknowledgement = ConversationTurn(
            session_id=session_id,
            sequence=next_seq,
            role=ConversationTurnRole.ASSISTANT,
            content=(
                "已记录这项信息。"
                f"还有 {pending_count} 个已提出的问题待你确认；回答完成后我会再整理下一步。"
            ),
            model_profile="clarification-batch-ack",
            turn_type=ConversationTurnType.MESSAGE,
            in_reply_to_turn_id=user_turn.id,
        )
        await store.add_conversation_turn(acknowledgement)
        await store.append_event(
            project_id,
            "conversation.assistant_reply_ready",
            {
                "session_id": str(session_id),
                "turn_id": str(acknowledgement.id),
                "clarification_batch_pending": pending_count,
            },
        )
        return {
            "user_turn": user_turn,
            "assistant_turn": acknowledgement,
            "session_id": str(session_id),
        }

    async def _write_pending_clarification_notice(
        self,
        store: DomainStore,
        *,
        project_id: UUID,
        session_id: UUID,
        user_turn: ConversationTurn,
        pending_count: int,
    ) -> dict[str, Any]:
        """Keep an explicit clarification batch as the only active input path.

        This differs from the acknowledgement written after a structured
        clarification answer: the free message has not resolved any question,
        so it is recorded for audit/context but cannot advance the model turn.
        """

        if pending_count < 1:
            raise ValueError("clarification notice requires a pending question")
        await store.lock_conversation_session(session_id)
        existing = await store.get_conversation_reply_to_turn(user_turn.id)
        if existing is not None:
            return {
                "user_turn": user_turn,
                "assistant_turn": existing,
                "session_id": str(session_id),
            }
        next_seq = (await store.get_latest_turn_sequence(session_id)) + 1
        notice = ConversationTurn(
            session_id=session_id,
            sequence=next_seq,
            role=ConversationTurnRole.ASSISTANT,
            content=(
                f"当前还有 {pending_count} 个待确认的问题。"
                "请先在问题卡片中逐项回答；这一批完成后我会继续分析。"
            ),
            model_profile="clarification-batch-notice",
            turn_type=ConversationTurnType.MESSAGE,
            in_reply_to_turn_id=user_turn.id,
        )
        await store.add_conversation_turn(notice)
        await store.append_event(
            project_id,
            "conversation.assistant_reply_ready",
            {
                "session_id": str(session_id),
                "turn_id": str(notice.id),
                "clarification_batch_pending": pending_count,
                "clarification_batch_notice": True,
            },
        )
        return {
            "user_turn": user_turn,
            "assistant_turn": notice,
            "session_id": str(session_id),
        }

    async def _persist_model_response(
        self,
        store: DomainStore,
        project_id: UUID,
        session_id: UUID,
        response: ConversationModelResponse,
        *,
        in_reply_to_turn_id: UUID | None = None,
    ) -> ConversationTurn:
        if in_reply_to_turn_id is not None:
            await store.lock_conversation_session(session_id)
            existing = await store.get_conversation_reply_to_turn(in_reply_to_turn_id)
            if existing is not None:
                return existing
        next_seq = (await store.get_latest_turn_sequence(session_id)) + 1
        assistant_turn = ConversationTurn(
            session_id=session_id,
            sequence=next_seq,
            role=ConversationTurnRole.ASSISTANT,
            content=response.reply,
            model_profile="deepseek-v4-pro",
            turn_type=ConversationTurnType.MESSAGE,
            in_reply_to_turn_id=in_reply_to_turn_id,
        )
        await store.add_conversation_turn(assistant_turn)

        project = await store.get_project(project_id)
        is_initial_requirements_phase = (
            project is not None and project.active_requirement_revision_id is None
        )
        clarifications = response.clarifications
        action_proposals = response.action_proposals
        if is_initial_requirements_phase:
            clarifications, action_proposals = (
                await self._gate_initial_clarification_output(store, session_id, response)
            )

        for c_draft in clarifications:
            clarification = ConversationClarification(
                turn_id=assistant_turn.id,
                question=c_draft.question,
                kind=c_draft.kind,
            )
            await store.add_conversation_clarification(clarification)

        for a_draft in action_proposals:
            payload = dict(a_draft.proposed_payload)
            # The same rewrite_requirements shape has two different user paths:
            # before any requirement exists it is only a form-prefill draft;
            # afterwards it is a proposal that may enter the change-review path.
            # Set this server-owned marker instead of trusting a model-provided
            # field, so an initial draft can never later be accepted as a second
            # requirements rewrite.
            if (
                is_initial_requirements_phase
                and a_draft.kind is ConversationActionProposalKind.REWRITE_REQUIREMENTS
            ):
                payload[_INITIAL_REQUIREMENTS_DRAFT_MARKER] = True
            proposal = ConversationActionProposal(
                turn_id=assistant_turn.id,
                kind=a_draft.kind,
                summary=a_draft.summary,
                proposed_payload=payload,
            )
            await store.add_conversation_action_proposal(proposal)

        await store.append_event(
            project_id,
            "conversation.assistant_reply_ready",
            {
                "session_id": str(session_id),
                "turn_id": str(assistant_turn.id),
                "clarification_count": len(clarifications),
                "proposal_count": len(action_proposals),
            },
        )

        return assistant_turn

    @staticmethod
    async def _gate_initial_clarification_output(
        store: DomainStore,
        session_id: UUID,
        response: ConversationModelResponse,
    ) -> tuple[
        tuple[ConversationClarificationDraft, ...],
        tuple[ConversationActionProposalDraft, ...],
    ]:
        """Defensive server-side rules for the pre-approval clarification stage.

        The system prompt drives coverage-based, small-batch clarification
        rounds; these rules make that behavior robust regardless of what the
        model emits:

        1. At most two independent clarifications are persisted per model
           response. Each must target a distinct, still-missing coverage item;
           questions about an already-covered item never survive.
        2. In the initial requirements phase, no model action proposal is
           persisted until coverage has converged. While any clarification is
           pending, neither another question nor any action can be appended.
        3. The initial draft is persisted only when every one of the five
           coverage items is covered (persisted evidence or the model's
           controlled coverage declaration) and the model emitted no question at
           all. The draft payload is then sanitized to user-confirmed facts:
           modules, unknowns, module skeletons, candidate solutions and open
           research questions are always stripped server-side.
        4. A question whose normalized form was already asked in the session
           (pending or resolved) is never persisted again, and a repeated
           question within one response is collapsed to a single one. A response
           that only repeats already-asked questions persists nothing and still
           holds back the initial draft, because the model's remaining questions
           mean the rounds have not converged.
        """
        pending = await store.list_active_clarifications(session_id)

        def _initial_draft_only(
            drafts: Sequence[ConversationActionProposalDraft],
        ) -> tuple[ConversationActionProposalDraft, ...]:
            return _sanitize_initial_draft(
                drafts,
                optional_unknowns=_initial_optional_unknowns(merged),
            )

        if pending:
            return (), ()

        history = await store.list_clarifications(session_id)
        seen = {_normalize_clarification_question(item.question) for item in history}
        seen.discard("")

        merged = _merge_coverage(
            response.coverage, await _coverage_from_persisted(store, session_id)
        )
        missing_items = _missing_coverage_items(merged)

        selected: list[ConversationClarificationDraft] = []
        selected_items: set[ConversationClarificationCoverageItem] = set()
        for draft in response.clarifications:
            normalized = _normalize_clarification_question(draft.question)
            if not normalized or normalized in seen:
                continue
            item = _coverage_item_for_kind(draft.kind)
            if item is None or item not in missing_items or item in selected_items:
                # Pre-approval questions must make measurable progress.  An
                # unclassified question, a covered item, or a duplicate item
                # cannot unlock the initial draft and is therefore omitted.
                continue
            selected.append(draft)
            seen.add(normalized)
            selected_items.add(item)
            if len(selected) == _MAX_INITIAL_CLARIFICATIONS_PER_RESPONSE:
                break

        if (
            not selected
            and not response.clarifications
            and _required_initial_coverage_is_complete(merged)
        ):
            # Usage and hard constraints form the minimum feasibility boundary.
            # Optional blank context stays visible as an editable unknown instead
            # of forcing every project through the same five-field interview.
            return (), _initial_draft_only(response.action_proposals)
        # Not converged yet: persist a small, deduplicated batch of questions
        # for distinct missing items and hold back the initial draft.
        return tuple(selected), ()

    # ── resolve_clarification_and_continue ────────────────────────────────

    async def resolve_clarification_and_continue(
        self,
        *,
        project_id: UUID,
        clarification_id: UUID,
        response: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Resolve one pending clarification, then continue one controlled AI turn.

        Phase 1 persists the user's answer as a user turn plus the
        ``conversation.clarification_resolved`` event and commits, so the
        answer is durable before any provider call.  If another question from
        the same small clarification batch remains pending, Phase 2 writes a
        deterministic acknowledgement instead of spending a model call.  Once
        the batch is exhausted, it runs the bounded-context, no-tool, JSON-mode
        conversation model and persists the assistant reply together with any
        new clarifications / action proposals. A provider failure writes only
        the deterministic fallback — never engineering facts.

        Replaying the same Idempotency-Key returns the already-committed
        user answer and its follow-up assistant turn without creating
        duplicates (the crash window is completed with the deterministic
        fallback, exactly like post_message).
        """
        store = self._store_factory()
        clarification = await store.get_conversation_clarification(clarification_id)
        if clarification is None:
            raise ConversationNotFoundError("clarification not found")
        turn = await store.get_conversation_turn(clarification.turn_id)
        if turn is None:
            raise ConversationNotFoundError("clarification turn not found")
        session = await store.get_conversation_session(turn.session_id)
        if session is None or session.project_id != project_id:
            raise ConversationNotFoundError("clarification not in this project")
        if session.status.value != "active":
            raise ConversationConflictError("conversation session is closed")

        # Idempotent replay of an already-recorded user answer.  The answer
        # turn is the authority: when it already exists under this key and it
        # is the recorded resolution for this clarification, never append a
        # second answer — replay the follow-up (or finish a crashed window
        # with the deterministic fallback).
        await store.lock_conversation_session(turn.session_id)
        existing_answer = await store.get_conversation_turn_by_idempotency(
            turn.session_id, idempotency_key
        )
        if existing_answer is not None:
            if clarification.resolved_by_turn_id != existing_answer.id:
                raise ConversationConflictError(
                    "idempotency key is already bound to another conversation turn"
                )
            return await self._replay_existing(
                store, project_id, turn.session_id, existing_answer
            )

        if clarification.status is not ConversationClarificationStatus.PENDING:
            raise ConversationConflictError("clarification is not pending")

        # Phase 1: persist the user's answer before any provider call. This
        # releases the request transaction and preserves a replayable user
        # fact if the follow-up model is slow, unavailable, or the process
        # crashes afterwards.
        next_seq = (await store.get_latest_turn_sequence(turn.session_id)) + 1
        resolution_turn = ConversationTurn(
            session_id=turn.session_id,
            sequence=next_seq,
            role=ConversationTurnRole.USER,
            content=response,
            idempotency_key=idempotency_key,
        )
        await store.add_conversation_turn(resolution_turn)
        await store.resolve_conversation_clarification(clarification_id, resolution_turn.id)
        await store.append_event(
            project_id,
            "conversation.clarification_resolved",
            {
                "session_id": str(turn.session_id),
                "clarification_id": str(clarification_id),
                "turn_id": str(resolution_turn.id),
            },
        )
        await store.commit()

        pending = await store.list_active_clarifications(turn.session_id)
        if pending:
            return await self._write_pending_clarification_acknowledgement(
                store,
                project_id=project_id,
                session_id=turn.session_id,
                user_turn=resolution_turn,
                pending_count=len(pending),
            )

        # Phase 2 only after the small pending batch is exhausted: one
        # controlled follow-up AI turn in the same active session.
        try:
            context_text = await _build_conversation_context(
                store, project_id, turn.session_id, max_turns=self._max_turns
            )
            await store.commit()
            model_response = await _invoke_conversation_model(
                self._model_factory(),
                _build_conversation_system_prompt(),
                context_text,
                response,
            )
        except Exception:
            _logger.warning("conversation follow-up model call failed", exc_info=True)
            return await self._write_fallback(
                store, project_id, turn.session_id, resolution_turn
            )

        assistant_turn = await self._persist_model_response(
            store,
            project_id,
            turn.session_id,
            model_response,
            in_reply_to_turn_id=resolution_turn.id,
        )
        return {
            "user_turn": resolution_turn,
            "assistant_turn": assistant_turn,
            "session_id": str(turn.session_id),
        }

    # ── sessions / turns ──────────────────────────────────────────────────

    async def list_sessions(self, project_id: UUID) -> Sequence[ConversationSession]:
        return await self._store_factory().list_conversation_sessions(project_id)

    async def list_turns(
        self, session_id: UUID, *, after: int = 0, limit: int = 50
    ) -> Sequence[ConversationTurn]:
        return await self._store_factory().list_conversation_turns(
            session_id, after=after, limit=limit
        )

    async def new_session(self, project_id: UUID) -> ConversationSession:
        store = self._store_factory()
        active = await store.get_active_conversation_session(project_id)
        if active is not None:
            await store.close_conversation_session(active.id)
        session = ConversationSession(project_id=project_id)
        await store.add_conversation_session(session)
        await store.append_event(
            project_id,
            "conversation.session_started",
            {"session_id": str(session.id)},
        )
        return session

    async def close_session(self, session_id: UUID) -> ConversationSession:
        store = self._store_factory()
        existing = await store.get_conversation_session(session_id)
        if existing is None:
            raise ConversationNotFoundError("conversation session not found")
        await store.close_conversation_session(session_id)
        s = await store.get_conversation_session(session_id)
        if s is None:
            raise ConversationConflictError("session deleted after close")
        await store.append_event(
            existing.project_id,
            "conversation.session_closed",
            {"session_id": str(session_id)},
        )
        return s

    # ── accept_action_proposal ────────────────────────────────────────────

    async def accept_action_proposal(
        self,
        *,
        proposal_id: UUID,
        project_id: UUID,
        expected_project_revision: int,
    ) -> dict[str, Any]:
        """Accept and execute an action proposal.

        Returns {"proposal": ..., "result_ref": ...}.  If the proposal kind
        cannot be auto-executed safely, raises ConversationConflictError
        (the proposal stays proposed).
        """
        store = self._store_factory()
        proposal = await store.get_conversation_action_proposal(proposal_id)
        if proposal is None:
            raise ConversationNotFoundError("action proposal not found")

        # Verify proposal belongs to project
        turn = await store.get_conversation_turn(proposal.turn_id)
        if turn is None:
            raise ConversationConflictError("action proposal turn not found")
        session = await store.get_conversation_session(turn.session_id)
        if session is None or session.project_id != project_id:
            raise ConversationConflictError("action proposal does not belong to project")

        # An initial requirements draft is a form prefill with a restricted
        # payload (no modules/unknowns by construction), so it is rejected here
        # BEFORE generic payload validation, which would otherwise fail on the
        # missing modules list. Drafts are confirmed through the requirements
        # form, never through accept.
        if proposal.proposed_payload.get(_INITIAL_REQUIREMENTS_DRAFT_MARKER) is True:
            raise ConversationConflictError(
                "initial requirements drafts must be confirmed through the requirements form"
            )

        # Validate before claiming an idempotency command. In particular, a
        # snapshot restore must never let a proposal from project A operate on
        # project B merely because both projects happen to share a revision.
        self._validate_proposal_payload(proposal)
        if proposal.kind is ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT:
            snapshot_id = UUID(str(proposal.proposed_payload["snapshot_id"]).strip())
            snapshot = await store.get_solution_snapshot(snapshot_id)
            if snapshot is None or snapshot.project_id != project_id:
                # The same response intentionally covers missing and foreign
                # snapshots so this endpoint does not disclose another
                # project's history.
                raise ConversationConflictError("solution snapshot is unavailable for this project")

        command_key = f"conversation.accept:{proposal_id}"
        command_hash = _command_hash(
            "conversation.accept", proposal_id, project_id, expected_project_revision
        )
        replay_result_ref = await store.claim_command(command_key, command_hash)
        if replay_result_ref is not None:
            replayed = await store.get_conversation_action_proposal(proposal_id)
            if replayed is None or (
                replayed.status is not ConversationActionProposalStatus.ACCEPTED
            ):
                raise ConversationConflictError(
                    "conversation action receipt has no accepted proposal"
                )
            return {"proposal": replayed, "result_ref": replay_result_ref}

        if proposal.status is not ConversationActionProposalStatus.PROPOSED:
            raise ConversationConflictError("action proposal is not in proposed state")

        # Stale project revision guard
        project = await store.get_project(project_id)
        if project is None or project.revision != expected_project_revision:
            raise ConversationPreconditionFailedError(
                f"project revision {expected_project_revision} is stale "
                f"(current: {project.revision if project else '?'})"
            )

        # SelectionLock guard. A requirements rewrite or reshape can replace the
        # whole module graph, so it is blocked by any active lock. A selection
        # change is blocked only for the explicitly targeted module.
        active_locks = [
            lock for lock in await store.list_selection_locks(project_id) if lock.active
        ]
        if (
            proposal.kind
            in (
                ConversationActionProposalKind.REWRITE_REQUIREMENTS,
                ConversationActionProposalKind.RESHAPE_PROJECT,
            )
            and active_locks
        ):
            raise ConversationConflictError("selection locks prevent rewriting the current project")
        if proposal.kind is ConversationActionProposalKind.CHANGE_SELECTION:
            target = str(proposal.proposed_payload.get("module_id", ""))
            if any(str(lock.module_id) == target for lock in active_locks):
                raise ConversationConflictError("selection lock prevents changing this module")

        # Execute → raises ConversationConflictError if not safely executable
        result_ref = await self._execute_accepted(
            store, proposal, project_id, expected_project_revision
        )

        # Mark resolved
        now = datetime.now(UTC)
        session_id = turn.session_id
        await store.lock_conversation_session(session_id)
        next_seq = (await store.get_latest_turn_sequence(session_id)) + 1

        if proposal.kind is ConversationActionProposalKind.CHANGE_SELECTION:
            resolution_content = f"Action proposal '{proposal.summary}' accepted and executed."
        elif proposal.kind is ConversationActionProposalKind.START_RESEARCH:
            resolution_content = (
                f"Action proposal '{proposal.summary}' accepted. "
                "Research has not started yet — an approval-gated execution plan "
                "has been created and awaits separate review. No tools, model calls, "
                "or runtime work have been triggered."
            )
        elif proposal.kind in (
            ConversationActionProposalKind.REWRITE_REQUIREMENTS,
            ConversationActionProposalKind.ADD_MODULE,
            ConversationActionProposalKind.RESHAPE_PROJECT,
        ):
            resolution_content = (
                f"Action proposal '{proposal.summary}' accepted. "
                "A separate proposal has been created and awaits your review "
                "before any changes take effect."
            )
        elif proposal.kind is ConversationActionProposalKind.CHANGE_SPEND_BUDGET:
            resolution_content = (
                f"Action proposal '{proposal.summary}' accepted. "
                "A SpendBudgetProposal has been created and awaits your separate "
                "approval before the budget takes effect. No project facts, "
                "selections, or research have been modified."
            )
        elif proposal.kind is ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT:
            resolution_content = (
                f"Action proposal '{proposal.summary}' accepted. "
                "Module configuration has been restored to the snapshot's state. "
                "A new configuration revision was created; all active selection "
                "locks were respected during the restore."
            )
        else:
            resolution_content = f"Action proposal '{proposal.summary}' accepted and executed."

        resolution_turn = ConversationTurn(
            session_id=session_id,
            sequence=next_seq,
            role=ConversationTurnRole.ASSISTANT,
            content=resolution_content,
            model_profile="system",
            turn_type=ConversationTurnType.PROPOSAL,
            in_reply_to_turn_id=turn.id,
        )
        await store.add_conversation_turn(resolution_turn)

        await store.resolve_conversation_action_proposal(
            proposal_id,
            status=ConversationActionProposalStatus.ACCEPTED.value,
            resolved_at=now,
            resolution_turn_id=resolution_turn.id,
        )
        await store.append_event(
            project_id,
            "conversation.action_proposal_accepted",
            {
                "proposal_id": str(proposal_id),
                "resolution_turn_id": str(resolution_turn.id),
                "result_ref": result_ref,
            },
        )
        await store.save_command_receipt(command_key, command_hash, result_ref)
        return {
            "proposal": await store.get_conversation_action_proposal(proposal_id),
            "result_ref": result_ref,
        }

    async def reject_action_proposal(
        self,
        *,
        proposal_id: UUID,
        project_id: UUID,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reject an action proposal without executing its command.

        Rejection is deliberately a no-op at the Domain layer: it never runs a
        command, creates a job, consumes budget, or rewrites project facts, so
        the SelectionLock / budget controls used by accept have nothing to
        bypass here.  It enforces the same project scope, proposed-state and
        If-Match revision guards as accept, and replays through an idempotency
        receipt so a stale or repeated reject cannot record duplicate turns.

        The command key is scoped to the client's Idempotency-Key, so the same
        key replays the existing REJECTED result without a second resolution
        turn or event, while a different key on a settled proposal fails the
        proposed-state guard with a conflict.

        Returns {"proposal": ..., "result_ref": ...} mirroring accept.
        """
        store = self._store_factory()
        proposal = await store.get_conversation_action_proposal(proposal_id)
        if proposal is None:
            raise ConversationNotFoundError("action proposal not found")

        # Verify proposal belongs to project
        turn = await store.get_conversation_turn(proposal.turn_id)
        if turn is None:
            raise ConversationConflictError("action proposal turn not found")
        session = await store.get_conversation_session(turn.session_id)
        if session is None or session.project_id != project_id:
            raise ConversationConflictError("action proposal does not belong to project")

        command_key = f"conversation.reject:{project_id}:{proposal_id}:{idempotency_key}"
        command_hash = _command_hash(
            "conversation.reject", project_id, proposal_id, idempotency_key
        )
        replay_result_ref = await store.claim_command(command_key, command_hash)
        if replay_result_ref is not None:
            replayed = await store.get_conversation_action_proposal(proposal_id)
            if replayed is None or (
                str(replayed.status) != ConversationActionProposalStatus.REJECTED.value
            ):
                raise ConversationConflictError(
                    "conversation action receipt has no rejected proposal"
                )
            return {"proposal": replayed, "result_ref": replay_result_ref}

        if proposal.status is not ConversationActionProposalStatus.PROPOSED:
            raise ConversationConflictError("action proposal is not in proposed state")

        # Stale project revision guard (If-Match).  Rejection changes no
        # project fact, but the client must still prove it is acting against
        # the revision that proposed this action.
        project = await store.get_project(project_id)
        if project is None or project.revision != expected_project_revision:
            raise ConversationPreconditionFailedError(
                f"project revision {expected_project_revision} is stale "
                f"(current: {project.revision if project else '?'})"
            )

        # No command is dispatched.  The reject is durable through the
        # REJECTED status, a resolution turn and an audit event only.
        result_ref = f"rejected:{proposal_id}"

        now = datetime.now(UTC)
        session_id = turn.session_id
        await store.lock_conversation_session(session_id)
        next_seq = (await store.get_latest_turn_sequence(session_id)) + 1
        resolution_content = (
            f"Action proposal '{proposal.summary}' was rejected. "
            "No project facts, selections, budget, or jobs were changed."
        )
        resolution_turn = ConversationTurn(
            session_id=session_id,
            sequence=next_seq,
            role=ConversationTurnRole.ASSISTANT,
            content=resolution_content,
            model_profile="system",
            turn_type=ConversationTurnType.PROPOSAL,
            in_reply_to_turn_id=turn.id,
        )
        await store.add_conversation_turn(resolution_turn)

        await store.resolve_conversation_action_proposal(
            proposal_id,
            status=ConversationActionProposalStatus.REJECTED.value,
            resolved_at=now,
            resolution_turn_id=resolution_turn.id,
        )
        await store.append_event(
            project_id,
            "conversation.action_proposal_rejected",
            {
                "proposal_id": str(proposal_id),
                "resolution_turn_id": str(resolution_turn.id),
                "result_ref": result_ref,
            },
        )
        await store.save_command_receipt(command_key, command_hash, result_ref)
        return {
            "proposal": await store.get_conversation_action_proposal(proposal_id),
            "result_ref": result_ref,
        }

    async def _execute_accepted(
        self,
        store: DomainStore,
        proposal: ConversationActionProposal,
        project_id: UUID,
        expected_project_revision: int,
    ) -> str:
        """Dispatch to the corresponding Domain command.

        Returns a result_ref string.  Raises ConversationConflictError if
        the kind cannot be auto-executed safely — the caller must NOT
        mark the proposal accepted.
        """
        from aidison.application.service import ProjectApplication, canonical_hash
        from aidison.domain.models import (
            ExecutionPlanProposal,
            ProjectReshapeProposal,
            ProposedModule,
            RequirementsChangeProposal,
            UserAdjustmentKind,
        )
        from aidison.research.defaults import (
            RESEARCH_DEFAULT_ALLOWED_TOOL_CLASSES,
            RESEARCH_DEFAULT_MAX_CONCURRENCY,
            RESEARCH_DEFAULT_MAX_DURATION_SECONDS,
            RESEARCH_DEFAULT_MAX_TOKEN_BUDGET,
        )
        from aidison.runtime.contracts import MAX_DELEGATION_WAVE_SIZE, CoordinationMode

        app = ProjectApplication(store)
        payload = proposal.proposed_payload or {}

        if proposal.kind is ConversationActionProposalKind.REWRITE_REQUIREMENTS:
            if not {"goal", "modules"}.issubset(set(payload)):
                raise ConversationConflictError(
                    "rewrite_requirements payload missing goal or modules"
                )
            project = await store.get_project(project_id)
            if project is None:
                raise ConversationConflictError("project not found")
            change = await app.propose_requirements_change(
                proposal=RequirementsChangeProposal(
                    project_id=project_id,
                    basis_requirement_revision_id=project.active_requirement_revision_id,
                    basis_blueprint_id=project.active_blueprint_id,
                    target_goal=str(payload["goal"]),
                    hard_constraints=tuple(payload.get("hard_constraints", ())),
                    preferences=tuple(payload.get("preferences", ())),
                    available_resources=tuple(payload.get("available_resources", ())),
                    # Clarified context rides along; budget here is only a
                    # requirement constraint — accepting this proposal never
                    # creates a SpendBudgetRevision or any other side effect.
                    usage_context=str(payload.get("usage_context", "") or ""),
                    budget_context=str(payload.get("budget_context", "") or ""),
                    skill_context=str(payload.get("skill_context", "") or ""),
                    unknowns=tuple(payload.get("unknowns", ())),
                    summary=str(payload.get("summary") or proposal.summary),
                    modules=tuple(
                        ProposedModule.model_validate(item) for item in payload["modules"]
                    ),
                ),
                expected_project_revision=expected_project_revision,
                idempotency_key=f"conv-reqchange:{proposal.id}",
            )
            # This is deliberately not an approve_requirements call. The change
            # stays in PROPOSED until the user separately reviews and applies it
            # through the canonical requirements-change resolve endpoint.
            return f"requirements_change:{change.id}"

        elif proposal.kind is ConversationActionProposalKind.CHANGE_SELECTION:
            module_id = payload.get("module_id")
            candidate_id = payload.get("candidate_id")
            if not module_id or not candidate_id:
                raise ConversationConflictError(
                    "change_selection payload missing module_id or candidate_id"
                )
            try:
                parsed_module_id = UUID(str(module_id))
                parsed_candidate_id = UUID(str(candidate_id))
            except (TypeError, ValueError) as exc:
                raise ConversationConflictError(
                    "change_selection payload module_id and candidate_id must be UUIDs"
                ) from exc
            adjustment = await app.record_user_adjustment(
                project_id=project_id,
                module_id=parsed_module_id,
                kind=UserAdjustmentKind.SELECT_CANDIDATE,
                target={"candidate_id": str(parsed_candidate_id)},
                batch_window_seconds=2.0,
                expected_project_revision=expected_project_revision,
                idempotency_key=f"conv-prop:{proposal.id}",
            )
            preview_id: str | None = None
            if adjustment.batch_id is not None:
                preview = await app.generate_change_impact_preview(
                    batch_id=adjustment.batch_id,
                    expected_project_revision=expected_project_revision + 1,
                    idempotency_key=f"conv-preview:{proposal.id}",
                )
                preview_id = str(preview.id)
            return (
                f"adjustment:{parsed_module_id}:preview:{preview_id}"
                if preview_id is not None
                else f"adjustment:{parsed_module_id}"
            )

        elif proposal.kind is ConversationActionProposalKind.START_RESEARCH:
            project = await store.get_project(project_id)
            if project is None or project.active_requirement_revision_id is None:
                raise ConversationConflictError(
                    "requirements must be approved before execution planning"
                )
            requirement = await store.get_requirement_revision(
                project.active_requirement_revision_id
            )
            if requirement is None:
                raise ConversationConflictError("active requirement revision is unavailable")
            # Reuse the canonical active-module resolution used by execution
            # planning. This creates only a proposal; it never starts a job,
            # delegation, tool call, or model invocation.
            active_modules = await app._active_modules(project)
            if not active_modules:
                raise ConversationConflictError(
                    "research planning requires at least one active module"
                )
            # The plan's basis always covers the whole active module graph, so a
            # scoped re-research cannot smuggle a stale basis past the CAS guard.
            basis_hash = canonical_hash(
                project.active_requirement_revision_id, active_modules
            )
            requested_ids = payload.get("module_ids")
            if requested_ids:
                parsed_ids = [str(UUID(str(item).strip())) for item in requested_ids]
                active_by_id = {str(module.id): module for module in active_modules}
                missing = set(parsed_ids) - set(active_by_id)
                if missing:
                    raise ConversationConflictError(
                        "start_research module_ids reference inactive or unknown modules"
                    )
                modules = tuple(active_by_id[item] for item in parsed_ids)
                objective = payload.get("objective")
                if objective is None:
                    objective = (
                        "Re-research evidence and candidate options for the "
                        "requested modules."
                    )
            else:
                modules = active_modules
                objective = payload.get("objective")
                if objective is None:
                    objective = "Collect evidence and candidate options for each active module."
            if self._research_strategy_planner is None:
                raise ConversationConflictError("research strategy planner is unavailable")
            requested_concurrency = payload.get("max_concurrency")
            research_depth = cast(
                Literal["focused", "standard", "deep"],
                payload.get("research_depth", "deep"),
            )
            strategy = await self._research_strategy_planner(
                objective=objective,
                requirement=requirement,
                scope_modules=modules,
                research_depth=research_depth,
                source_strategy=payload.get("source_strategy"),
            )
            child_count = min(
                len(strategy.tasks),
                requested_concurrency or RESEARCH_DEFAULT_MAX_CONCURRENCY,
                MAX_DELEGATION_WAVE_SIZE,
            )
            requested_token_budget = payload.get("max_token_budget")
            requested_verification = payload.get("requires_independent_verification")
            requires_independent_verification = (
                requested_verification
                if requested_verification is not None
                else research_depth == "deep"
            )
            execution_plan = await app.propose_execution_plan(
                proposal=ExecutionPlanProposal(
                    project_id=project_id,
                    basis_hash=basis_hash,
                    objective=objective,
                    work_summary=(strategy.summary, *strategy.decision_notes),
                    allowed_coordination_modes=(CoordinationMode.DECOMPOSE,),
                    max_concurrency=child_count,
                    max_token_budget=requested_token_budget or RESEARCH_DEFAULT_MAX_TOKEN_BUDGET,
                    max_duration_seconds=(
                        payload.get("max_duration_seconds")
                        or RESEARCH_DEFAULT_MAX_DURATION_SECONDS
                    ),
                    research_depth=research_depth,
                    allowed_tool_classes=RESEARCH_DEFAULT_ALLOWED_TOOL_CLASSES,
                    allowed_effects=(),
                    requires_independent_verification=requires_independent_verification,
                    requires_result_approval=True,
                    research_strategy=strategy,
                ),
                expected_project_revision=expected_project_revision,
                idempotency_key=f"conv-plan:{proposal.id}",
            )
            return f"execution_plan:{execution_plan.id}"

        elif proposal.kind in {
            ConversationActionProposalKind.ADD_MODULE,
            ConversationActionProposalKind.RESHAPE_PROJECT,
        }:
            project = await store.get_project(project_id)
            if project is None or project.active_blueprint_id is None:
                raise ConversationConflictError(
                    "an active project blueprint is required before proposing a reshape"
                )
            active_modules = await app._active_modules(project)
            if not active_modules:
                raise ConversationConflictError(
                    "project reshape requires at least one active module"
                )

            if proposal.kind is ConversationActionProposalKind.ADD_MODULE:
                new_modules: tuple[ProposedModule, ...] = (
                    ProposedModule.model_validate(payload["module"]),
                )
                affected_module_ids: tuple[UUID, ...] = ()
                unchanged_module_ids = tuple(item.id for item in active_modules)
                target_goal = str(payload.get("target_goal") or project.goal)
                summary = str(payload.get("summary") or proposal.summary)
            else:
                new_modules = tuple(
                    ProposedModule.model_validate(item) for item in payload.get("new_modules", ())
                )
                try:
                    affected_module_ids = tuple(
                        UUID(str(item)) for item in payload.get("affected_module_ids", ())
                    )
                except (TypeError, ValueError) as exc:
                    raise ConversationConflictError(
                        "reshape_project affected_module_ids must contain UUID values"
                    ) from exc
                affected = set(affected_module_ids)
                unchanged_module_ids = tuple(
                    item.id for item in active_modules if item.id not in affected
                )
                target_goal = str(payload["target_goal"])
                summary = str(payload["summary"])

            dependency_edges_payload = payload.get("dependency_edges")
            dependency_edges = (
                tuple(
                    (
                        UUID(str(edge["source_module_id"])),
                        UUID(str(edge["target_module_id"])),
                    )
                    for edge in dependency_edges_payload
                )
                if dependency_edges_payload is not None
                else None
            )

            reshape = await app.propose_project_reshape(
                proposal=ProjectReshapeProposal(
                    project_id=project_id,
                    basis_blueprint_id=project.active_blueprint_id,
                    target_goal=target_goal,
                    summary=summary,
                    affected_module_ids=affected_module_ids,
                    unchanged_module_ids=unchanged_module_ids,
                    new_module_keys=tuple(item.key for item in new_modules),
                    new_modules=new_modules,
                    dependency_edges=dependency_edges,
                ),
                expected_project_revision=expected_project_revision,
                idempotency_key=f"conv-reshape:{proposal.id}",
            )
            # This is deliberately not an apply operation. The returned reshape
            # proposal stays in PROPOSED until the user separately reviews and
            # resolves it through the canonical reshape endpoint.
            return f"reshape_proposal:{reshape.id}"

        elif proposal.kind is ConversationActionProposalKind.CHANGE_SPEND_BUDGET:
            amount = str(payload.get("amount", "")).strip()
            currency = str(payload.get("currency", "")).strip().upper()
            summary = str(payload.get("summary") or proposal.summary)
            budget_proposal = await app.propose_spend_budget(
                project_id=project_id,
                amount=amount,
                currency=currency,
                summary=summary,
                expected_project_revision=expected_project_revision,
                idempotency_key=f"conv-budget:{proposal.id}",
            )
            # This is deliberately NOT a resolve. The proposal stays in PROPOSED
            # until the user separately approves it through the resolve endpoint.
            # project.revision is not bumped; active_spend_budget_revision_id is not set.
            return f"spend_budget:{budget_proposal.id}"

        elif proposal.kind is ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT:
            snapshot_id_str = str(payload.get("snapshot_id", "")).strip()
            snapshot_id = UUID(snapshot_id_str)
            await app.restore_solution_snapshot(
                snapshot_id=snapshot_id,
                expected_project_revision=expected_project_revision,
                idempotency_key=f"conv-restore:{proposal.id}",
            )
            # restore_solution_snapshot is self-contained with its own
            # CAS, idempotency, event, and lock enforcement.
            return f"solution_snapshot:{snapshot_id_str}"

        raise ConversationConflictError(f"unknown action proposal kind: {proposal.kind.value}")

    @staticmethod
    def _validate_proposal_payload(proposal: ConversationActionProposal) -> None:
        kind = proposal.kind
        payload = proposal.proposed_payload or {}

        if kind is ConversationActionProposalKind.REWRITE_REQUIREMENTS:
            required = {"goal", "modules"}
            if not required <= set(payload):
                raise ValueError("rewrite_requirements requires goal and modules in payload")
            if not isinstance(payload["goal"], str) or not payload["goal"].strip():
                raise ValueError("rewrite_requirements goal must be a non-empty string")
            if not isinstance(payload["modules"], (list, tuple)) or not payload["modules"]:
                raise ValueError("rewrite_requirements modules must be a non-empty list")
            for module in payload["modules"]:
                ProposedModule.model_validate(module)
            for field in ("usage_context", "budget_context", "skill_context"):
                value = payload.get(field)
                if value is not None and (not isinstance(value, str) or len(value) > 4_000):
                    raise ValueError(f"rewrite_requirements {field} must be a short string")
        elif kind is ConversationActionProposalKind.ADD_MODULE:
            module = payload.get("module")
            if not isinstance(module, dict):
                raise ValueError("add_module requires object 'module' in payload")
            ProposedModule.model_validate(module)
        elif kind is ConversationActionProposalKind.START_RESEARCH:
            allowed = {
                "objective",
                "module_ids",
                "max_concurrency",
                "max_token_budget",
                "max_duration_seconds",
                "requires_independent_verification",
                "research_depth",
                "source_strategy",
            }
            extra = set(payload) - allowed
            if extra:
                raise ValueError(
                    "start_research payload only allows objective, module_ids, "
                    "max_concurrency, max_token_budget, max_duration_seconds, and "
                    "requires_independent_verification, research_depth, and source_strategy; "
                    f"unexpected keys: {sorted(extra)}"
                )
            objective = payload.get("objective")
            if objective is not None and (not isinstance(objective, str) or not objective.strip()):
                raise ValueError("start_research objective must be a non-empty string")
            module_ids = payload.get("module_ids")
            if module_ids is not None:
                if not isinstance(module_ids, (list, tuple)) or not module_ids:
                    raise ValueError(
                        "start_research module_ids must be a non-empty list of UUIDs"
                    )
                seen: set[str] = set()
                for item in module_ids:
                    if not isinstance(item, str) or not item.strip():
                        raise ValueError("start_research module_ids must contain UUID strings")
                    try:
                        parsed = str(UUID(item.strip()))
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            "start_research module_ids must contain valid UUIDs"
                        ) from exc
                    if parsed in seen:
                        raise ValueError("start_research module_ids must be unique")
                    seen.add(parsed)
            max_concurrency = payload.get("max_concurrency")
            if max_concurrency is not None and (
                not isinstance(max_concurrency, int)
                or isinstance(max_concurrency, bool)
                or not 1 <= max_concurrency <= 16
            ):
                raise ValueError("start_research max_concurrency must be an integer from 1 to 16")
            max_token_budget = payload.get("max_token_budget")
            if max_token_budget is not None and (
                not isinstance(max_token_budget, int)
                or isinstance(max_token_budget, bool)
                or not 1 <= max_token_budget <= 1_000_000_000
            ):
                raise ValueError(
                    "start_research max_token_budget must be an integer from 1 to 1000000000"
                )
            max_duration_seconds = payload.get("max_duration_seconds")
            if max_duration_seconds is not None and (
                not isinstance(max_duration_seconds, int)
                or isinstance(max_duration_seconds, bool)
                or not 60 <= max_duration_seconds <= 604_800
            ):
                raise ValueError(
                    "start_research max_duration_seconds must be an integer from 60 to 604800"
                )
            requires_verification = payload.get("requires_independent_verification")
            if requires_verification is not None and not isinstance(requires_verification, bool):
                raise ValueError(
                    "start_research requires_independent_verification must be a boolean"
                )
            research_depth = payload.get("research_depth")
            if research_depth is not None and research_depth not in {
                "focused",
                "standard",
                "deep",
            }:
                raise ValueError(
                    "start_research research_depth must be focused, standard, or deep"
                )
            source_strategy = payload.get("source_strategy")
            if source_strategy is not None and source_strategy not in {
                "primary",
                "independent",
                "official",
                "mixed",
            }:
                raise ValueError(
                    "start_research source_strategy must be primary, independent, "
                    "official, or mixed"
                )
        elif kind is ConversationActionProposalKind.CHANGE_SELECTION:
            if "module_id" not in payload or "candidate_id" not in payload:
                raise ValueError("change_selection requires module_id and candidate_id in payload")
        elif kind is ConversationActionProposalKind.RESHAPE_PROJECT:
            required = {"target_goal", "summary", "affected_module_ids", "new_modules"}
            if not required <= set(payload):
                raise ValueError(
                    "reshape_project requires target_goal, summary, "
                    "affected_module_ids, and new_modules in payload"
                )
            if not isinstance(payload["target_goal"], str) or not payload["target_goal"].strip():
                raise ValueError("reshape_project target_goal must be a non-empty string")
            if not isinstance(payload["summary"], str) or not payload["summary"].strip():
                raise ValueError("reshape_project summary must be a non-empty string")
            if not isinstance(payload["affected_module_ids"], (list, tuple)):
                raise ValueError("reshape_project affected_module_ids must be a list")
            if not isinstance(payload["new_modules"], (list, tuple)):
                raise ValueError("reshape_project new_modules must be a list")
            try:
                tuple(UUID(str(item)) for item in payload["affected_module_ids"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "reshape_project affected_module_ids must contain UUID values"
                ) from exc
            for module in payload["new_modules"]:
                ProposedModule.model_validate(module)
            dependency_edges = payload.get("dependency_edges")
            if dependency_edges is not None:
                if not isinstance(dependency_edges, (list, tuple)):
                    raise ValueError("reshape_project dependency_edges must be a list")
                parsed_edges: set[tuple[UUID, UUID]] = set()
                for edge in dependency_edges:
                    if not isinstance(edge, dict) or set(edge) != {
                        "source_module_id",
                        "target_module_id",
                    }:
                        raise ValueError(
                            "reshape_project dependency_edges require source_module_id and "
                            "target_module_id"
                        )
                    try:
                        parsed_edge = (
                            UUID(str(edge["source_module_id"])),
                            UUID(str(edge["target_module_id"])),
                        )
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            "reshape_project dependency_edges must contain UUID values"
                        ) from exc
                    if parsed_edge[0] == parsed_edge[1] or parsed_edge in parsed_edges:
                        raise ValueError(
                            "reshape_project dependency_edges must be unique non-self edges"
                        )
                    parsed_edges.add(parsed_edge)
        elif kind is ConversationActionProposalKind.CHANGE_SPEND_BUDGET:
            allowed = {"amount", "currency", "summary"}
            extra = set(payload) - allowed
            if extra:
                raise ValueError(
                    "change_spend_budget payload only allows "
                    f"'amount', 'currency', 'summary'; unexpected keys: {sorted(extra)}"
                )
            if not isinstance(payload.get("amount"), str) or not payload["amount"].strip():
                raise ValueError("change_spend_budget requires a non-empty decimal amount string")
            if (
                not isinstance(payload.get("currency"), str)
                or len(payload["currency"].strip()) != 3
            ):
                raise ValueError("change_spend_budget requires a 3-letter ISO 4217 currency code")
            try:
                from decimal import Decimal, InvalidOperation

                Decimal(payload["amount"].strip())
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(
                    "change_spend_budget amount must be a valid decimal string"
                ) from exc
            summary = payload.get("summary")
            if summary is not None and (not isinstance(summary, str) or not summary.strip()):
                raise ValueError("change_spend_budget summary must be a non-empty string")
        elif kind is ConversationActionProposalKind.RESTORE_SOLUTION_SNAPSHOT:
            allowed = {"snapshot_id"}
            extra = set(payload) - allowed
            if extra:
                raise ValueError(
                    "restore_solution_snapshot payload only allows "
                    f"'snapshot_id'; unexpected keys: {sorted(extra)}"
                )
            snapshot_id_raw = payload.get("snapshot_id")
            if not isinstance(snapshot_id_raw, str) or not snapshot_id_raw.strip():
                raise ValueError("restore_solution_snapshot requires snapshot_id as a UUID string")
            try:
                UUID(snapshot_id_raw.strip())
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "restore_solution_snapshot snapshot_id must be a valid UUID"
                ) from exc


class ConversationNotFoundError(RuntimeError):
    pass


class ConversationConflictError(RuntimeError):
    pass


class ConversationPreconditionFailedError(ConversationConflictError):
    """Stale project revision or CAS failure — maps to HTTP 412.

    Extends ConversationConflictError so existing ``except ConversationConflictError``
    callers that do not distinguish remain safe, while API handlers that catch this
    first can return the correct status code.
    """
