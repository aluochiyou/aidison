# ADR Format

ADRs live in `docs/adr/` and use sequential numbering: `0001-slug.md`, `0002-slug.md`, etc.

Create the `docs/adr/` directory lazily — only when the first ADR is needed.

## Template

Use the compact form only for low-complexity qualifying decisions. For architecture, shared contracts, persistence, security, migration, provider lock-in, or other decisions that future implementation depends on, use the traceable form:

```md
---
status: proposed
date: YYYY-MM-DD
supersedes: []
commit_lineage: []
---

# ADR-NNNN: Title

## Problem
需要解决的工程问题、约束和决策边界。

## Decision
选择了什么。

## Alternatives
实际考虑过的替代方案及未选择原因。

## Evidence
支持该决定的代码、测试、文档、测量或用户确认。

## Counterevidence
反例、不利证据和仍不确定之处；没有时写 `None found`，不得省略。

## Validation
决定成立后可执行的验证方法、结果状态和通过标准。

## Invalidation
哪些新事实、失败信号或边界变化会使决定失效并触发复审或回滚。

## Consequences
代价、收益和下游影响。
```

`supersedes` 使用 ADR ID；被替代 ADR 状态改为 `superseded by ADR-NNNN`。`commit_lineage` 只记录真实 commit SHA；尚未进入 Git 时写 `not_available` 及原因，集成提交后再补充。ADR 记录可审查的依据和结果，不记录 chain-of-thought、聊天流水或未经验证的猜测。

## Optional sections

Only include these when they add genuine value. Most ADRs won't need them.

- **Status** frontmatter (`proposed | accepted | deprecated | superseded by ADR-NNNN`) — useful when decisions are revisited
- **Considered Options** — only when the rejected alternatives are worth remembering
- **Consequences** — only when non-obvious downstream effects need to be called out

## Numbering

Scan `docs/adr/` for the highest existing number and increment by one.

## When to offer an ADR

All three of these must be true:

1. **Hard to reverse** — the cost of changing your mind later is meaningful
2. **Surprising without context** — a future reader will look at the code and wonder "why on earth did they do it this way?"
3. **The result of a real trade-off** — there were genuine alternatives and you picked one for specific reasons

If a decision is easy to reverse, skip it — you'll just reverse it. If it's not surprising, nobody will wonder why. If there was no real alternative, there's nothing to record beyond "we did the obvious thing."

### What qualifies

- **Architectural shape.** "We're using a monorepo." "The write model is event-sourced, the read model is projected into Postgres."
- **Integration patterns between contexts.** "Ordering and Billing communicate via domain events, not synchronous HTTP."
- **Technology choices that carry lock-in.** Database, message bus, auth provider, deployment target. Not every library — just the ones that would take a quarter to swap out.
- **Boundary and scope decisions.** "Customer data is owned by the Customer context; other contexts reference it by ID only." The explicit no-s are as valuable as the yes-s.
- **Deliberate deviations from the obvious path.** "We're using manual SQL instead of an ORM because X." Anything where a reasonable reader would assume the opposite. These stop the next engineer from "fixing" something that was deliberate.
- **Constraints not visible in the code.** "We can't use AWS because of compliance requirements." "Response times must be under 200ms because of the partner API contract."
- **Rejected alternatives when the rejection is non-obvious.** If you considered GraphQL and picked REST for subtle reasons, record it — otherwise someone will suggest GraphQL again in six months.
