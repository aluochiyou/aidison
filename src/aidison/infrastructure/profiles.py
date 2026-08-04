from __future__ import annotations

from collections.abc import Iterable, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aidison.infrastructure.orm import (
    AgentProfileActiveRow,
    AgentProfileRevisionRow,
    JobProfileBindingRow,
)
from aidison.runtime.contracts import AgentProfileRevision, ProfileBinding


class ProfileConflictError(RuntimeError):
    pass


class ProfileNotFoundError(RuntimeError):
    pass


class ProfileRepository:
    """PostgreSQL-backed immutable AgentProfile registry."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(
        self,
        profile: AgentProfileRevision,
        *,
        activate: bool = False,
    ) -> AgentProfileRevision:
        existing = await self._session.get(
            AgentProfileRevisionRow,
            (profile.profile_id, profile.revision),
        )
        if existing is not None:
            if existing.definition_hash != profile.definition_hash:
                raise ProfileConflictError("profile identity already exists with different content")
        else:
            self._session.add(
                AgentProfileRevisionRow(
                    profile_id=profile.profile_id,
                    revision=profile.revision,
                    definition_hash=profile.definition_hash,
                    prompt_hash=profile.prompt_hash,
                    purpose=profile.purpose,
                    prompt_template=profile.prompt_template,
                    input_schema_ref=profile.input_schema_ref,
                    output_schema_ref=profile.output_schema_ref,
                    definition=profile.model_dump(mode="json"),
                    token_cap=profile.token_cap,
                    tool_call_cap=profile.tool_call_cap,
                    concurrency_cap=profile.concurrency_cap,
                    timeout_seconds=profile.timeout_seconds,
                )
            )
            await self._session.flush()
        if activate:
            await self.activate(profile.profile_id, profile.revision)
        return profile

    async def ensure_registered(
        self,
        profiles: Iterable[AgentProfileRevision],
        *,
        activate: bool = False,
    ) -> None:
        for profile in profiles:
            await self.register(profile)
            if activate:
                pointer = await self._session.get(AgentProfileActiveRow, profile.profile_id)
                if pointer is None:
                    await self.activate(profile.profile_id, profile.revision)

    async def get_revision(self, profile_id: str, revision: int) -> AgentProfileRevision:
        row = await self._session.get(AgentProfileRevisionRow, (profile_id, revision))
        if row is None:
            raise ProfileNotFoundError(f"profile revision not found: {profile_id}@{revision}")
        return AgentProfileRevision.model_validate(row.definition)

    async def activate(self, profile_id: str, revision: int) -> None:
        await self.get_revision(profile_id, revision)
        pointer = await self._session.scalar(
            select(AgentProfileActiveRow)
            .where(AgentProfileActiveRow.profile_id == profile_id)
            .with_for_update()
        )
        if pointer is None:
            self._session.add(
                AgentProfileActiveRow(profile_id=profile_id, active_revision=revision)
            )
        else:
            pointer.active_revision = revision
        await self._session.flush()

    async def resolve_active(self, profile_id: str) -> AgentProfileRevision:
        pointer = await self._session.get(AgentProfileActiveRow, profile_id)
        if pointer is None:
            raise ProfileNotFoundError(f"active profile not found: {profile_id}")
        return await self.get_revision(profile_id, pointer.active_revision)

    async def bind_revisions(
        self,
        *,
        root_job_id: UUID,
        roles: Mapping[str, tuple[str, int]],
    ) -> tuple[ProfileBinding, ...]:
        bindings: list[ProfileBinding] = []
        for role_key, (profile_id, revision) in roles.items():
            profile = await self.get_revision(profile_id, revision)
            existing = await self._session.get(JobProfileBindingRow, (root_job_id, role_key))
            binding = ProfileBinding(
                root_job_id=root_job_id,
                role_key=role_key,
                profile_id=profile.profile_id,
                profile_revision=profile.revision,
                definition_hash=profile.definition_hash,
            )
            if existing is not None:
                if (
                    existing.profile_id != binding.profile_id
                    or existing.profile_revision != binding.profile_revision
                    or existing.definition_hash != binding.definition_hash
                ):
                    raise ProfileConflictError("root Job profile manifest is already frozen")
            else:
                self._session.add(
                    JobProfileBindingRow(
                        root_job_id=root_job_id,
                        role_key=role_key,
                        profile_id=binding.profile_id,
                        profile_revision=binding.profile_revision,
                        definition_hash=binding.definition_hash,
                    )
                )
            bindings.append(binding)
        await self._session.flush()
        return tuple(bindings)

    async def get_binding(self, root_job_id: UUID, role_key: str) -> ProfileBinding:
        row = await self._session.get(JobProfileBindingRow, (root_job_id, role_key))
        if row is None:
            raise ProfileNotFoundError(f"profile binding not found: {role_key}")
        profile = await self.get_revision(row.profile_id, row.profile_revision)
        if profile.definition_hash != row.definition_hash:
            raise ProfileConflictError("profile binding definition hash does not match registry")
        return ProfileBinding(
            root_job_id=row.root_job_id,
            role_key=row.role_key,
            profile_id=row.profile_id,
            profile_revision=row.profile_revision,
            definition_hash=row.definition_hash,
        )
