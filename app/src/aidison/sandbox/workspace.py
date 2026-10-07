"""Trusted per-task staging and output collection for T2 Sandbox tasks."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from aidison.sandbox.contracts import SandboxTaskSpec


class SandboxWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectedSandboxOutput:
    relative_path: str
    content: bytes
    content_hash: str


@dataclass(frozen=True)
class TaskWorkspace:
    spec: SandboxTaskSpec
    root: Path
    input_dir: Path
    output_dir: Path
    work_dir: Path

    @classmethod
    def create(cls, *, root: Path, spec: SandboxTaskSpec) -> TaskWorkspace:
        base = root.resolve()
        task_root = (base / f"task-{spec.sandbox_task_id}").resolve()
        if base not in task_root.parents:
            raise SandboxWorkspaceError("sandbox task root escaped configured root")
        input_dir, output_dir, work_dir = (
            task_root / "input",
            task_root / "output",
            task_root / "work",
        )
        for directory in (input_dir, output_dir, work_dir):
            directory.mkdir(parents=True, exist_ok=False)
            os.chmod(directory, 0o700)
        return cls(
            spec=spec,
            root=task_root,
            input_dir=input_dir,
            output_dir=output_dir,
            work_dir=work_dir,
        )

    def stage_inputs(self, broker_paths: dict[str, Path]) -> None:
        """Copy exactly authorized artifacts and verify bytes on the trusted side."""

        for artifact in self.spec.input_artifacts:
            source_path = broker_paths.get(artifact.artifact_ref)
            if source_path is None:
                raise SandboxWorkspaceError("sandbox input is missing from trusted broker")
            source = source_path.resolve()
            if not source.is_file() or source.is_symlink():
                raise SandboxWorkspaceError("sandbox input broker path must be a regular file")
            if source.stat().st_size != artifact.size_bytes:
                raise SandboxWorkspaceError("sandbox input size does not match artifact manifest")
            target = self._safe_child(self.input_dir, artifact.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if sha256(target.read_bytes()).hexdigest() != artifact.content_hash:
                target.unlink(missing_ok=True)
                raise SandboxWorkspaceError("sandbox input hash does not match artifact manifest")
            os.chmod(target, 0o400)
        os.chmod(self.input_dir, 0o500)

    def collect_outputs(
        self, *, allowed_relative_paths: tuple[str, ...]
    ) -> tuple[CollectedSandboxOutput, ...]:
        """Read only allowlisted regular files under `/output`, with hard limits."""

        allowed = set(allowed_relative_paths)
        if len(allowed) != len(allowed_relative_paths):
            raise SandboxWorkspaceError("sandbox output allowlist must be unique")
        discovered: list[Path] = []
        for candidate in self.output_dir.rglob("*"):
            relative = candidate.relative_to(self.output_dir).as_posix()
            if candidate.is_symlink():
                raise SandboxWorkspaceError("sandbox output symlink is not allowed")
            if candidate.is_dir():
                continue
            if not stat.S_ISREG(candidate.stat(follow_symlinks=False).st_mode):
                raise SandboxWorkspaceError("sandbox output must be a regular file")
            if relative not in allowed:
                raise SandboxWorkspaceError("sandbox produced a non-allowlisted output")
            discovered.append(candidate)
        outputs: list[CollectedSandboxOutput] = []
        total = 0
        for candidate in sorted(
            discovered, key=lambda item: item.relative_to(self.output_dir).as_posix()
        ):
            content = candidate.read_bytes()
            total += len(content)
            if (
                len(outputs) + 1 > self.spec.resource_policy.output_max_files
                or total > self.spec.resource_policy.output_max_bytes
            ):
                raise SandboxWorkspaceError("sandbox output exceeds resource policy")
            outputs.append(
                CollectedSandboxOutput(
                    relative_path=candidate.relative_to(self.output_dir).as_posix(),
                    content=content,
                    content_hash=sha256(content).hexdigest(),
                )
            )
        return tuple(outputs)

    @staticmethod
    def _safe_child(root: Path, relative_path: str) -> Path:
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise SandboxWorkspaceError("sandbox path escaped workspace")
        candidate = (root / path).resolve()
        if root not in candidate.parents:
            raise SandboxWorkspaceError("sandbox path escaped workspace")
        return candidate
