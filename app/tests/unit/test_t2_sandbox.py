from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.sandbox.contracts import (
    SandboxInputArtifact,
    SandboxNetworkMode,
    SandboxResourcePolicy,
    SandboxTaskSpec,
)
from aidison.sandbox.controller import DockerSandboxController, SandboxControllerError
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
