# 07. 工程实践与决策方法

## 1. 一种状态只保留一个权威 owner

Aidison 把状态分为 Domain truth、runtime facts、plan history、Artifact bytes 和 UI projection。PlanTask status 可从 Job 重建，SSE/NOTIFY 只是投影/信号；不能把每层都做成可独立写入的“真相”。

面试表达：分布式系统最危险的不是少一个队列，而是同一事实有两个可写 owner。

## 2. 用数据库约束守住跨进程不变量

应用校验只能约束正常代码路径。并发、多版本 Worker 或人工 SQL 仍可能绕过。因此项目使用：

- unique/partial unique：唯一 receipt、唯一 live approval；
- compound FK：跨 project/revision 引用不能串线；
- CHECK：有限状态和 payload/typed column 一致；
- trigger：EffectApproval scope 不可修改；
- row lock + CAS：revision/generation/status 转移。

原则是“应用层给出好错误，数据库层保证坏状态写不进去”。

## 3. 以 crash window 设计测试

普通 happy path 不能证明 durable。测试应主动在以下边界崩溃：

- wave 已创建但 parent 未记录后续状态；
- 部分 child 已完成；
- JoinReceipt 已提交但 Domain 未写；
- 外部 provider 调用前已 PREPARED；
- provider 已 dispatch 但结果未知；
- lease 过期后旧 Worker 才返回。

恢复后检查的不只是“最终成功”，还包括 child/receipt/Domain write/provider call 是否恰好一次，以及预算是否可解释。

## 4. 性能优化不得改变正确性来源

PostgreSQL NOTIFY 让 Join 更快醒来，但不携带授权信息，也不替代数据库重读。这个模式比“消息到了就执行”多一次 query，却能容忍丢消息和断线。

## 5. 抽象必须有第二个真实使用者

通用 executor 不是从 Research helper 改名，而是让真实 Solution workflow 接入并通过 reclaim/atomicity 测试。尚无第二使用者的 ResearchGap 保留在业务包，避免 speculative abstraction。

## 6. 外部副作用使用 fail-closed

对模型推理，失败可以重试；对创建购物车、支付、设备操作，retry 可能重复产生现实后果。Aidison 使用 scoped approval、PREPARED receipt 和 ambiguous 状态，把“我们不知道 provider 是否执行”表示出来，而不是伪装成失败后自动重试。

## 7. 配置、secret 与版本化决定分离

- API key/token：环境变量，不进入 Git；
- 模型名、路由、TTL、预算：`config.yaml`，可 review；
- schema：Alembic；
- 架构选择：ADR；
- 当前验收：`docs/STATUS.md`；
- 上游来源：`UPSTREAM_MAP.md`。

每种可变状态只有一个记录位置，避免 `.env`、README 和代码默认值互相漂移。

## 8. 数据库验收必须隔离

集成测试拒绝指向默认业务库，fresh migration 使用临时 PostgreSQL 17/18 数据库。评审 Agent 也必须遵守 read-only 范围；本轮曾有下级进程越界修改隔离测试容器 role password，已立即终止并恢复。这说明“提示它只读”不等于技术隔离，后续应进一步使用只读账号/容器权限。

## 9. 上游源码采用需要 lineage

导入 Deep Agents/深改 UI 后，项目记录 exact commit、导入范围、排除范围、修改原因和回归结果。这样能回答：哪些是上游能力、哪些是 Aidison 自研、升级成本在哪里。

## 10. 版本收口清单

- acceptance criteria 与未完成项已更新；
- Ruff、mypy、unit/integration、migration、web build 有明确 passed/failed/not_checked；
- PostgreSQL 至少覆盖项目支持版本；
- Spec 与 Standards review 分开；
- ADR/STATUS/学习文档和简历 claims 同步；
- 不把一次 live 成功包装成稳定生产能力；
- 保留 worktree 和 commits，未经批准不删除开发现场。

## 可复用的工程决策模板

```text
Problem: 当前不变量或失败窗口是什么？
Owner: 哪一层是唯一事实源？
Decision: 最小状态机/事务边界是什么？
Alternatives: 为什么不选更简单或更重的方案？
Validation: 哪些 crash/concurrency/replay tests 能证伪？
Invalidation: 什么证据出现后应该改设计？
Consequence: 获得什么，长期成本是什么？
```

这个模板比单纯记录“用了 PostgreSQL/LangGraph”更能体现工程判断。
