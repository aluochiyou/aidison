# AGENTS.md

Default rules shared by all AI agents (Claude Code, Codex, OpenCode). Skill schemas handle their own discovery and execution.

## Communication

- Use Simplified Chinese by default; keep code, commands, paths, identifiers, model names, and API names in English.
- State uncertainty, important assumptions, and meaningful trade-offs directly.
- Ask questions only when ambiguity could materially affect the result or introduce risk.
- Report the exact save path for every generated artifact.

## Working Style

- Do only what the user requested and choose the smallest sufficient solution.
- Reuse existing code, tools, and project patterns before introducing new abstractions, configuration, or dependencies.
- Match workflow size to task size; do not use heavy Skills or multi-agent workflows for small changes.
- Modify only what the current task requires, avoid unrelated code, and preserve user changes.
- After two materially different attempts fail on the same configuration, permission, or architecture-choice blocker, stop retrying, preserve the evidence, and ask the user to decide or act.

## Verification

- Define verifiable success criteria before implementation.
- Reproduce bugs before fixing them; confirm behavior before refactoring and verify it again afterward.
- Never claim that unexecuted or unverified work is complete.
- Record verification as `passed`, `failed`, or `not_checked`.

## Sources of Truth

- Git: code, configuration, tests, and history.
- A specification produced by `to-spec`, or another accepted specification: requirements and acceptance criteria.
- Orca: active terminals, worktrees, tasks, and agent state.
- Handoff artifacts: session transfer only.

Keep one authoritative source for each mutable state.

## Multi-Agent

Use `aluo-team-workflow` for complex module work that benefits from multi-process parallelism. That Skill defines roles, concurrency, ownership, review, and worktree rules. Handle ordinary tasks in the current agent or with internal subagents.

- Keep at most five active child-worktree agents or other isolated writable workers. Short-lived agents that share the main worktree for read-only research, review, configuration diagnosis, or another explicitly non-conflicting action do not count toward this worktree limit; create and release them according to task volume.
- Assign persistent agents by task boundary and explicit ownership, not by a fixed list of module names. The boundary may follow a module, feature, workflow, layer, or another independently verifiable unit; reuse an existing owner when the follow-up remains within that boundary.
- When a task does not need independent parallel writes, dependency isolation, or a long-lived context, prefer the current worktree or a temporary subagent. This applies to implementation, tests, research, review, and documentation, not only read-only work.
- A persistent agent may use its native subagents for parallel subtasks inside its assigned boundary when the subtasks have non-overlapping ownership and the runtime supports it. The parent agent remains responsible for coordination, validation, and handoff; subagents do not silently expand the parent scope.
- Create a separate worktree only when writable isolation, incompatible dependencies, long-lived ownership, or parallel integration justifies it. Do not create one worktree per small task or per tool/provider.

## Safety

- Back up important files before risky operations.
- Do not delete important files. Without approval, do not force-reset Git, overwrite user changes, or perform irreversible merges, migrations, releases, or cleanup.
- Never expose passwords, API keys, tokens, or other sensitive information.
