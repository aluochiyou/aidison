"""The only component allowed to construct a local Docker sandbox command."""

from __future__ import annotations

import json
from dataclasses import dataclass

from aidison.sandbox.contracts import SandboxNetworkMode, SandboxTaskSpec
from aidison.sandbox.workspace import TaskWorkspace


@dataclass(frozen=True)
class SandboxLaunchCommand:
    argv: tuple[str, ...]


class SandboxControllerError(RuntimeError):
    pass


class DockerSandboxController:
    """Build a fixed-entrypoint hardened container launch; never accept shell text."""

    def build_command(
        self, *, spec: SandboxTaskSpec, workspace: TaskWorkspace
    ) -> SandboxLaunchCommand:
        if spec.network_mode is not SandboxNetworkMode.NONE:
            raise SandboxControllerError(
                "egress_proxy sandbox tasks require a dedicated proxy controller"
            )
        policy = spec.resource_policy
        input_mount = f"type=bind,src={workspace.input_dir},dst=/input,readonly"
        output_mount = f"type=bind,src={workspace.output_dir},dst=/output"
        argv = (
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(policy.pids_limit),
            "--memory",
            f"{policy.memory_mebibytes}m",
            "--cpus",
            str(policy.cpu_millis / 1000),
            "--ulimit",
            f"nofile={policy.fd_limit}:{policy.fd_limit}",
            "--network=none",
            "--user",
            "65532:65532",
            "--mount",
            input_mount,
            "--mount",
            output_mount,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={policy.memory_mebibytes // 2}m",
            "--tmpfs",
            f"/work:rw,noexec,nosuid,size={policy.memory_mebibytes // 2}m",
            "--entrypoint",
            f"/opt/aidison/entrypoints/{spec.entrypoint_id}",
            spec.image_digest,
            "--arguments-json",
            json.dumps(spec.arguments, sort_keys=True, separators=(",", ":")),
        )
        return SandboxLaunchCommand(argv=argv)
