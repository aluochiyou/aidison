---
status: accepted
date: 2026-08-08
supersedes: []
commit_lineage: []
---

# ADR-0006: 外部副作用前使用一次性、精确 scope 的 EffectApproval

## Problem

Shopping 已能生成 PurchaseProposal 和 provider-hosted checkout handoff，但浏览器命令可以直接触发 `create_cart`。`DecisionRequest` 表达的是方案选择，不是对外部副作用的授权；复用它会混淆“决定买什么”和“允许此刻对哪个冻结报价执行一次操作”。如果 approval 只存在内存或只检查一个布尔值，报价、SolutionVersion 或 provider 变化后仍可能误用旧授权，provider 边界 crash 还会造成重复调用。

## Decision

新增独立 `EffectApproval` 聚合，首个 effect kind 为 `shopping.create_cart`。不可变 scope 由服务端从 `project_id`、PurchaseProposal、OfferSnapshot、active SolutionVersion、provider、basis 与 constraints 规范化计算；客户端只能提交 approval ID，不能声明自己被批准的 scope。

状态机为：

```text
requested -> approved -> consumed
          -> denied
          -> expired
approved  -> expired
```

同一 `(project_id, scope_hash)` 最多存在一个 `requested` 或 `approved` gate。PostgreSQL 使用 partial unique index 防止并发重复 live gate，复合外键保证 proposal 与 project 一致，trigger 阻止 scope 字段和 JSON payload 中 scope 的更新。应用层再以 expected-status CAS 执行 resolve/consume，形成数据库与领域双层 fail-closed。

checkout 的事务顺序固定为：校验当前 scope → `approved` CAS 为 `consumed` → 写 `PREPARED` CheckoutHandoff、domain event 和 command receipt → commit → 调用 provider。这样 provider 前 crash 可以确定恢复；provider 后结果未知时 approval 仍 consumed，不能用不同 idempotency key 重放外部 effect。同一 checkout key 重放返回原 handoff，不再调用 provider。

TTL 是非 secret 产品配置，保存在 `config.yaml` 的 `shopping.effect_approval_ttl_seconds`，数据库保存绝对 `expires_at`。当前 resolve endpoint 明确是本地单用户 control plane；本版本不伪造用户身份、RBAC 或多租户授权。

## Alternatives

- 复用 `DecisionRequest`：两个状态机的审计含义和消费语义不同，拒绝。
- 在 `.env` 放 TTL：TTL 不是 secret，且需要可版本化、可审查，拒绝。
- provider 成功后才 consume approval：进程在 provider 成功与数据库提交之间崩溃会重复 effect，拒绝。
- 保存支付凭证或直接完成购买：超出当前 provider-hosted checkout handoff 边界，拒绝。

## Validation

- domain tests 覆盖状态时间戳、deny reason 与 consumed lifecycle；
- application/API tests 覆盖 request、approve、deny、expiry、wrong scope、cross-project、consumed/new-key、同 key replay、provider error 与 provider-boundary crash；
- migration 在 PostgreSQL 17/18 从零升级，且保持单一 Alembic head；
- Web 控制台必须显式呈现 request → approve/deny → checkout 三步，不得隐藏授权动作。

## Consequences

外部 effect 现在具备可审计、一次性、精确 scope 的 capability gate，且和 idempotent command receipt 共同覆盖 crash window。它不是完整 IAM：生产化仍需把 resolver 绑定到认证主体、角色、审计 actor 与可能的双人审批。未来接入淘宝 API/MCP 时，授权对象必须继续是服务端冻结的报价和 provider action，不能授权 Agent 任意调用购买工具。
