from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from aidison.sandbox.contracts import (
    SandboxInputArtifact,
    SandboxNetworkMode,
    SandboxResourcePolicy,
    SandboxTaskSpec,
)
from aidison.sandbox.controller import DockerSandboxController, SandboxControllerError
from aidison.sandbox.runner import (
    LocalDockerSandboxRunner,
    SandboxExecutionError,
    SandboxOutputContract,
    SandboxOutputContractRegistry,
    SandboxProcessResult,
)
from aidison.sandbox.workspace import SandboxWorkspaceError, TaskWorkspace


def _spec(**changes: object) -> SandboxTaskSpec:
    values: dict[str, object] = {
        "invocation_id": uuid4(),
        "run_id": uuid4(),
        "task_id": uuid4(),
        "producer_generation": 1,
        "basis_hash": "a" * 64,
        "sandbox_profile_ref": "sandbox-profile://pdf-parser/v1",
        "image_digest": "registry.example/pdf-parser@sha256:" + "b" * 64,
        "entrypoint_id": "parse_pdf",
        "arguments": {"ocr": False},
        "input_artifacts": (),
        "output_schema_ref": "schema://sandbox/pdf-output/v1",
        "network_mode": SandboxNetworkMode.NONE,
        "resource_policy": SandboxResourcePolicy(
            cpu_millis=500,
            memory_mebibytes=256,
            pids_limit=32,
            fd_limit=128,
            wall_clock_seconds=30,
            output_max_bytes=1024,
            output_max_files=2,
        ),
        "secret_policy_ref": "secret-policy://none/v1",
        "deadline": datetime.now(UTC) + timedelta(minutes=1),
        "idempotency_key": "sandbox-test-task",
    }
    values.update(changes)
    return SandboxTaskSpec.model_validate(values)


def test_task_spec_rejects_mutable_images_and_shell_like_entrypoints() -> None:
    with pytest.raises(ValidationError, match="image_digest"):
        _spec(image_digest="registry.example/pdf-parser:latest")
    assert _spec(image_digest="sha256:" + "c" * 64).image_digest.startswith("sha256:")
    with pytest.raises(ValidationError, match="entrypoint_id"):
        _spec(entrypoint_id="/bin/sh")


def test_workspace_stages_verified_input_and_rejects_symlink_output(tmp_path: Path) -> None:
    input_bytes = b"untrusted PDF bytes"
    source = tmp_path / "artifact.bin"
    source.write_bytes(input_bytes)
    input_artifact = SandboxInputArtifact(
        artifact_ref="artifact+sha256://" + sha256(input_bytes).hexdigest() + "/input",
        content_hash=sha256(input_bytes).hexdigest(),
        size_bytes=len(input_bytes),
        media_type="application/pdf",
        relative_path="document.pdf",
    )
    spec = _spec(input_artifacts=(input_artifact,))
    workspace = TaskWorkspace.create(root=tmp_path / "sandbox-root", spec=spec)

    workspace.stage_inputs({input_artifact.artifact_ref: source})
    assert (workspace.input_dir / "document.pdf").read_bytes() == input_bytes

    (workspace.output_dir / "result.json").write_text('{"ok":true}', encoding="utf-8")
    collected = workspace.collect_outputs(allowed_relative_paths=("result.json",))
    assert collected[0].relative_path == "result.json"
    assert collected[0].content == b'{"ok":true}'

    (workspace.output_dir / "escaped").symlink_to(source)
    with pytest.raises(SandboxWorkspaceError, match="symlink"):
        workspace.collect_outputs(allowed_relative_paths=("result.json", "escaped"))


def test_controller_builds_hardened_network_none_command_without_host_socket(
    tmp_path: Path,
) -> None:
    spec = _spec()
    workspace = TaskWorkspace.create(root=tmp_path / "sandbox-root", spec=spec)
    command = DockerSandboxController().build_command(spec=spec, workspace=workspace)

    assert "--read-only" in command.argv
    assert command.container_name == f"aidison-sandbox-{spec.sandbox_task_id.hex}"
    assert "--cap-drop=ALL" in command.argv
    assert "no-new-privileges:true" in command.argv
    assert "--network=none" in command.argv
    assert "--user" in command.argv
    assert all("docker.sock" not in item for item in command.argv)
    assert all("/home/" not in item for item in command.argv)


def test_controller_refuses_egress_until_a_dedicated_proxy_exists(tmp_path: Path) -> None:
    spec = _spec(network_mode=SandboxNetworkMode.EGRESS_PROXY)
    workspace = TaskWorkspace.create(root=tmp_path / "sandbox-root", spec=spec)

    with pytest.raises(SandboxControllerError, match="proxy controller"):
        DockerSandboxController().build_command(spec=spec, workspace=workspace)


class _ResultPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


class _WritingProcessBackend:
    def __init__(self, *, output_path: Path, content: bytes, return_code: int = 0) -> None:
        self._output_path = output_path
        self._content = content
        self._return_code = return_code
        self.timeout_seconds: float | None = None
        self.stdio_max_bytes: int | None = None

    async def run(
        self,
        *,
        command: object,
        timeout_seconds: float,
        stdio_max_bytes: int,
    ) -> SandboxProcessResult:
        del command
        self.timeout_seconds = timeout_seconds
        self.stdio_max_bytes = stdio_max_bytes
        self._output_path.write_bytes(self._content)
        return SandboxProcessResult(
            return_code=self._return_code,
            stdout=b"profile completed",
            stderr=b"",
        )


def _output_contracts() -> SandboxOutputContractRegistry:
    registry = SandboxOutputContractRegistry()
    registry.register(
        SandboxOutputContract(
            schema_ref="schema://sandbox/pdf-output/v1",
            allowed_relative_paths=("result.json",),
            required_relative_paths=("result.json",),
            json_models=(("result.json", _ResultPayload),),
        )
    )
    return registry


@pytest.mark.asyncio
async def test_runner_validates_output_and_removes_exact_workspace(tmp_path: Path) -> None:
    spec = _spec()
    workspace_root = tmp_path / "sandbox-root"
    output_path = workspace_root / f"task-{spec.sandbox_task_id}" / "output" / "result.json"
    backend = _WritingProcessBackend(output_path=output_path, content=b'{"ok":true}')
    runner = LocalDockerSandboxRunner(
        workspace_root=workspace_root,
        output_contracts=_output_contracts(),
        process_backend=backend,
    )

    result = await runner.execute(spec=spec, broker_paths={})

    assert result.outputs[0].content == b'{"ok":true}'
    assert result.stdout == b"profile completed"
    assert backend.stdio_max_bytes == 65_536
    assert backend.timeout_seconds is not None and backend.timeout_seconds <= 30
    assert not output_path.parent.parent.exists()


@pytest.mark.asyncio
async def test_runner_rejects_invalid_schema_and_still_cleans_workspace(tmp_path: Path) -> None:
    spec = _spec()
    workspace_root = tmp_path / "sandbox-root"
    task_root = workspace_root / f"task-{spec.sandbox_task_id}"
    backend = _WritingProcessBackend(
        output_path=task_root / "output" / "result.json",
        content=b'{"unexpected":true}',
    )
    runner = LocalDockerSandboxRunner(
        workspace_root=workspace_root,
        output_contracts=_output_contracts(),
        process_backend=backend,
    )

    with pytest.raises(SandboxExecutionError) as raised:
        await runner.execute(spec=spec, broker_paths={})

    assert raised.value.reason_code == "sandbox_output_schema_invalid"
    assert not task_root.exists()


@pytest.mark.asyncio
async def test_runner_refuses_elapsed_deadline_before_creating_workspace(tmp_path: Path) -> None:
    spec = _spec(deadline=datetime.now(UTC) - timedelta(seconds=1))
    backend = _WritingProcessBackend(
        output_path=tmp_path / "unused.json",
        content=b'{"ok":true}',
    )
    runner = LocalDockerSandboxRunner(
        workspace_root=tmp_path / "sandbox-root",
        output_contracts=_output_contracts(),
        process_backend=backend,
    )

    with pytest.raises(SandboxExecutionError) as raised:
        await runner.execute(spec=spec, broker_paths={})

    assert raised.value.reason_code == "sandbox_deadline_elapsed"
    assert not (tmp_path / "sandbox-root").exists()
