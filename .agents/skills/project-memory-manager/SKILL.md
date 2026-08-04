---
name: project-memory-manager
description: Build, sync, and audit long-term project memory documents. Three modes - init, sync, audit.
---

# Project Memory Manager

Maintains the project's long-term knowledge: initializes or audits CONTEXT.md, syncs docs/ARCHITECTURE.md and docs/STATUS.md, and audits/indexes docs/adr/. `domain-modeling` owns vocabulary changes and qualifying ADR proposals; in multi-Agent work the controller is the only final writer.

## Modes

### init

Create project memory from scratch.

**New project** (after an available `grilling` pass or equivalent user-confirmed discovery): Extract facts from the completed interview and repository contents.

**Existing project** (brownfield): Scan directory structure, entry files, dependencies, build config, tests, deployment config, README, and existing docs. Record only what is verifiable from code/config/tests. Mark unverifiable content:

```
Unknown | Needs confirmation | TODO: verify
```

Output: CONTEXT.md + docs/ARCHITECTURE.md + docs/STATUS.md. Do not create speculative ADRs.

### sync

Run after implementation and verification are complete.

1. Inspect actual code, config, test, and interface changes from this session
2. Determine which long-term documents are affected
3. Update only affected sections
4. Preserve user-confirmed domain terms and design decisions (especially in CONTEXT.md)
5. Do not rewrite unrelated documents
6. Output a sync report:

```
Updated: [list modified docs]
Unchanged: [checked but unchanged docs]
Drift: [inconsistencies or items needing confirmation]
```

If a repository change specification was completed, extract only information that has lasting value for project cognition. Do not copy the specification verbatim.

### audit

Read-only check. Do not modify any files.

Check:
- Documents referencing non-existent modules
- Architecture diagram mismatch with actual code
- ADRs whose implementation no longer exists
- STATUS.md listing completed features as in-progress
- Completed change specifications conflicting with long-term docs
- Document content not verifiable from code, config, tests, or confirmed decisions

## Constraints

- Never overwrite user-confirmed domain language in CONTEXT.md
- Never generate bulk ADRs for "documentation completeness"
- Never copy OpenSpec spec content into project memory documents
- Never treat speculation as fact
- See `references/document-ownership.md` for ownership rules
- See `references/sync-rules.md` for conflict priority

## Engineering decision lineage

When a durable architecture decision is affected, do not create a second decision record. Link to the canonical ADR and preserve only verified long-term consequences in ARCHITECTURE/STATUS. A qualifying ADR proposal must make the following reviewable: problem/context, alternatives, selected/rejected options, evidence and counterevidence, expected consequences, validation command/result, invalidation or rollback trigger, `supersedes`, and real commit/spec references when available.

Do not record chain-of-thought, chat transcripts, speculative rationale, or a process diary. In multi-Agent work only the controller may run write-mode `init` or `sync`; other Agents return a drift report or patch proposal.
