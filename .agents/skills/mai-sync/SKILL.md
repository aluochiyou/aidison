---
name: mai-sync
description: Scan an existing software project into Mai and maintain compact structured project data during development. Use for project baselines, tasks, modules, interfaces, decisions, risks, milestones, test outcomes, blockers, Git milestones, and any work that should appear in the Mai dashboard.
---

# Mai Sync

Keep Mai current with small factual writes. Maintain stable entities when the state can change; emit events only for meaningful history.

Mai is a projection, not the canonical source for coordination, implementation behavior, architecture, domain language, or decision rationale. During multi-Agent work only the controller writes project-level Mai state; Workers return stable IDs, source paths and verification evidence.

## Choose the smallest write

| Development fact | Write |
| --- | --- |
| A task, module, phase, milestone, interface, decision, risk or architecture link changed | Upsert that one entity by stable ID |
| A test run, blocker transition, release, merge or completed delivery should remain in history | Emit one event |
| Mai starts tracking a project or module boundaries changed substantially | Run an incremental scan |
| Nothing user-visible or structurally meaningful changed | Do not write |

Do not send source code, full chat transcripts, hidden reasoning, secrets or speculative conclusions. Prefer relative paths, test names, document paths and real commit SHAs as evidence. A Mai decision is a compact dashboard index: when an ADR exists, add an `adr:<relative-path>` entry to `evidence` instead of copying the full alternatives and rationale. Do not invent fields that the active Mai API/model schema rejects.

## Development loop

1. At the start of a meaningful work item, upsert it as `in_progress` with acceptance criteria.
2. After a verified checkpoint, update only its progress, evidence or status. Add a separate event only when the checkpoint belongs in the timeline.
3. Record a decision, risk or interface as soon as it becomes stable enough to affect another module.
4. On completion, set the work item to `done`, progress to `100`, and include concrete evidence.
5. On a blocker, set the work item to `blocked` and emit one `blocked` event. Update the same item when cleared.

This keeps a live dashboard to one compact write per changed entity. Do not rescan the whole project after routine edits.

## Preferred MCP tools

When the Mai MCP server is configured, prefer:

- `mai_scan_project` with `mode: incremental`
- `mai_get_project_context` before planning or reviewing
- `mai_upsert_project_entity` for stable project state
- `mai_remove_project_entity` when an entity was genuinely removed
- `mai_emit_event` for timeline-worthy facts

Use the CLI below when MCP is unavailable.

## Baseline or incremental scan

Run once when Mai first tracks a project, then only after structural changes:

```bash
node .agents/skills/mai-sync/scripts/mai-sync.mjs scan --project-root .
```

The CLI registers or resolves the project by absolute path, then calls POST /api/projects/:id/scan with mode: incremental (default). Pass --mode full for a complete rescan. When Mai is offline, the CLI falls back to a lightweight local event baseline written to .mai/events.ndjson.

Mai's backend caches module analysis and parsed process documents. Normal refreshes compare content hashes, reuse unchanged documents, and invalidate module analysis when relevant source content changes.

## Upsert project state

Create or update a task by stable ID:

```bash
node .agents/skills/mai-sync/scripts/mai-sync.mjs upsert --project-root . --collection workItems --id task-project-switch --title "项目切换贯穿全部视图" --description "项目选择驱动看板、甘特、架构和 Git 数据刷新。" --status in_progress --priority high --progress 60 --module project-context --acceptance "切换后无旧项目数据,刷新后仍保持当前项目" --evidence "tests/project-switch.spec.ts"
```

For other entities, pass a compact JSON object:

```bash
node .agents/skills/mai-sync/scripts/mai-sync.mjs upsert --project-root . --collection interfaces --id api-project-scan --data '{"name":"POST /api/projects/:id/scan","type":"http","moduleId":"scanner","method":"POST","path":"/api/projects/:id/scan","description":"Incremental project scan"}'
```

Allowed collections are `modules`, `workItems`, `phases`, `milestones`, `decisions`, `risks`, `interfaces`, `architectureNodes`, `architectureEdges`, and `sourceDocuments`. See [references/project-model-schema.md](references/project-model-schema.md).

`sourceDocuments` uses its relative `path` as the stable key. For example:

```bash
node .agents/skills/mai-sync/scripts/mai-sync.mjs upsert --project-root . --collection sourceDocuments --path docs/ARCHITECTURE.md --hash 0123456789abcdef
```

Remove only a confirmed obsolete entity:

```bash
node .agents/skills/mai-sync/scripts/mai-sync.mjs remove --project-root . --collection risks --id risk-obsolete
```

The CLI writes through the Mai API when available. If Mai is offline, it atomically updates `.mai/project.json`, so the next scan can recover the state.

## Emit a timeline event

Emit after a meaningful delivery, test result, blocker transition or Git milestone:

```bash
node .agents/skills/mai-sync/scripts/mai-sync.mjs emit --project-root . --type feature_completed --title "完成项目切换" --detail "项目选择现在驱动全部业务视图刷新，并通过项目隔离回归测试。" --status completed --module project-context --files "src/store/index.ts,src/components/layout/AppLayout.tsx" --evidence "npm run build,Playwright project switch test"
```

Write `detail` as one to three concrete sentences. Use the constraints in [references/event-schema.md](references/event-schema.md). Offline events are deduplicated in `.mai/events.ndjson`.
