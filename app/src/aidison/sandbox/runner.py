"""Bounded local Docker execution for fixed T2 sandbox profiles."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Protocol

from pydantic import BaseModel, ValidationError

from aidison.sandbox.contracts import SandboxTaskSpec
from aidison.sandbox.controller import DockerSandboxController, SandboxLaunchCommand
from aidison.sandbox.workspace import CollectedSandboxOutput, TaskWorkspace


class SandboxExecutionError(RuntimeError):
    """A stable failure safe to expose to runtime diagnostics."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class SandboxOutputContract:
    schema_ref: str
    allowed_relative_paths: tuple[str, ...]
    required_relative_paths: tuple[str, ...]
    json_models: tuple[tuple[str, type[BaseModel]], ...] = ()

    def __post_init__(self) -> None:
        allowed = set(self.allowed_relative_paths)
        if not allowed or len(allowed) != len(self.allowed_relative_paths):
            raise ValueError("sandbox output contract paths must be non-empty and unique")
        if not set(self.required_relative_paths).issubset(allowed):
            raise ValueError("required sandbox outputs must be allowlisted")
        if not {path for path, _ in self.json_models}.issubset(allowed):
            raise ValueError("sandbox JSON validators must target allowlisted outputs")
        for path in allowed:
            parsed = PurePosixPath(path)
            if parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != path:
                raise ValueError("sandbox output contract path must be safe and canonical")

    def validate(self, outputs: tuple[CollectedSandboxOutput, ...]) -> None:
        by_path = {item.relative_path: item for item in outputs}
        if not set(self.required_relative_paths).issubset(by_path):
            raise SandboxExecutionError("sandbox_required_output_missing")
        for path, model in self.json_models:
            output = by_path.get(path)
            if output is None:
                continue
            try:
                model.model_validate_json(output.content)
            except ValidationError as error:
                raise SandboxExecutionError("sandbox_output_schema_invalid") from error


class SandboxOutputContractRegistry:
    def __init__(self) -> None:
        self._contracts: dict[str, SandboxOutputContract] = {}

    def register(self, contract: SandboxOutputContract) -> None:
        current = self._contracts.get(contract.schema_ref)
        if current is not None and current != contract:
            raise ValueError("sandbox output schema ref already has a different contract")
        self._contracts[contract.schema_ref] = contract

    def resolve(self, schema_ref: str) -> SandboxOutputContract:
        try:
            return self._contracts[schema_ref]
        except KeyError as error:
            raise SandboxExecutionError("sandbox_output_schema_unregistered") from error


@dataclass(frozen=True, slots=True)
class SandboxProcessResult:
    return_code: int
    stdout: bytes
    stderr: bytes


class SandboxProcessBackend(Protocol):
    async def run(
        self,
        *,
        command: SandboxLaunchCommand,
        timeout_seconds: float,
        stdio_max_bytes: int,
    ) -> SandboxProcessResult: ...


class AsyncioDockerProcessBackend:
    """Execute Docker without a shell and remove the exact task container on abort."""

    async def run(
        self,
        *,
        command: SandboxLaunchCommand,
        timeout_seconds: float,
        stdio_max_bytes: int,
    ) -> SandboxProcessResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise SandboxExecutionError("sandbox_docker_unavailable") from error

        total_bytes = [0]

        async def drain(stream: asyncio.StreamReader | None) -> bytes:
            if stream is None:
                return b""
            chunks: list[bytes] = []
            while chunk := await stream.read(8_192):
                total_bytes[0] += len(chunk)
                if total_bytes[0] > stdio_max_bytes:
                    raise SandboxExecutionError("sandbox_stdio_limit_exceeded")
                chunks.append(chunk)
            return b"".join(chunks)

        try:
            return_code, stdout, stderr = await asyncio.wait_for(
                asyncio.gather(
                    process.wait(),
                    drain(process.stdout),
                    drain(process.stderr),
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as error:
            await self._abort(process=process, container_name=command.container_name)
            raise SandboxExecutionError("sandbox_wall_clock_exceeded") from error
        except SandboxExecutionError:
            await self._abort(process=process, container_name=command.container_name)
            raise
        return SandboxProcessResult(
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
        )

    async def _abort(
        self,
        *,
        process: asyncio.subprocess.Process,
        container_name: str,
    ) -> None:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                pass
        cleanup = DockerSandboxController.build_force_remove_command(container_name)
        try:
            remover = await asyncio.create_subprocess_exec(
                *cleanup,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(remover.wait(), timeout=10)
        except (FileNotFoundError, TimeoutError):
            # The named container may already have disappeared because --rm is
            # enabled. The original timeout/output-limit failure remains the
            # authoritative result.
            return


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    sandbox_task_id: str
    duration_milliseconds: int
    stdout: bytes
    stderr: bytes
    outputs: tuple[CollectedSandboxOutput, ...]


class LocalDockerSandboxRunner:
    """Stage verified inputs, execute one profile and collect validated outputs."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        output_contracts: SandboxOutputContractRegistry,
        controller: DockerSandboxController | None = None,
        process_backend: SandboxProcessBackend | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._output_contracts = output_contracts
        self._controller = controller or DockerSandboxController()
        self._process_backend = process_backend or AsyncioDockerProcessBackend()

    async def execute(
        self,
        *,
        spec: SandboxTaskSpec,
        broker_paths: dict[str, Path],
    ) -> SandboxExecutionResult:
        contract = self._output_contracts.resolve(spec.output_schema_ref)
        remaining_seconds = (spec.deadline - datetime.now(UTC)).total_seconds()
        if remaining_seconds <= 0:
            raise SandboxExecutionError("sandbox_deadline_elapsed")
        workspace = TaskWorkspace.create(root=self._workspace_root, spec=spec)
        started = monotonic()
        try:
            workspace.stage_inputs(broker_paths)
            command = self._controller.build_command(spec=spec, workspace=workspace)
            remaining_seconds = (spec.deadline - datetime.now(UTC)).total_seconds()
            if remaining_seconds <= 0:
                raise SandboxExecutionError("sandbox_deadline_elapsed")
            process_result = await self._process_backend.run(
                command=command,
                timeout_seconds=min(
                    float(spec.resource_policy.wall_clock_seconds),
                    remaining_seconds,
                ),
                stdio_max_bytes=spec.resource_policy.stdio_max_bytes,
            )
            if process_result.return_code != 0:
                raise SandboxExecutionError("sandbox_process_failed")
            outputs = workspace.collect_outputs(
                allowed_relative_paths=contract.allowed_relative_paths
            )
            contract.validate(outputs)
            return SandboxExecutionResult(
                sandbox_task_id=str(spec.sandbox_task_id),
                duration_milliseconds=max(0, int((monotonic() - started) * 1_000)),
                stdout=process_result.stdout,
                stderr=process_result.stderr,
                outputs=outputs,
            )
        finally:
            workspace.cleanup()


__all__ = [
    "AsyncioDockerProcessBackend",
    "LocalDockerSandboxRunner",
    "SandboxExecutionError",
    "SandboxExecutionResult",
    "SandboxOutputContract",
    "SandboxOutputContractRegistry",
    "SandboxProcessBackend",
    "SandboxProcessResult",
]
