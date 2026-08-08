# Aidison 上层编排、PostgreSQL Runtime 与 Globex 参考价值审计

- 日期：2026-08-07
- 范围：当前 Aidison 源码、本地 LoopX/OpenRath、`完整学习项目/globex电商采购助手`
- 方式：只读源码/文档核验，不修改产品代码

## 1. 核心结论

[F] Aidison 的上层多智能体系统不能简单概括为“基于 LoopX 和 OpenRath”。更准确的分层是：

| 层 | 主要来源 | Aidison 中的职责 |
|---|---|---|
| 计划控制与任务图 | LoopX 为主；WebSwarm、InfoSeeker、ScaffoldAgent/DeepResearch 为辅 | PlanRevision、ready frontier、Gap/PlanPatch、scoped gate、角色/模式选择 |
| 耐久执行与副作用语义 | Aidison 自有实现；OpenRath 是最强 donor，LoopX/Symphony 补充 | Job/Attempt、lease/fencing、reclaim、JoinReceipt、cancel、late result、budget/effect |
| 单个 Agent 执行 | Deep Agents Core + LangGraph/LangChain | 有界 model/tool loop、structured output、middleware |
| 业务逻辑 | Aidison 自有 Domain | Project/Requirement/Module/Evidence/Candidate/Decision/Solution/Observation/Impact/Patch/Shopping |

[J] 因此，“控制面较多参考 LoopX，durable kernel 较多参考 OpenRath”是正确的；“Aidison 的业务逻辑主要基于它们”是不正确的。

## 2. PostgreSQL Runtime 的来源与作用

### 来源

[F] `src/aidison/infrastructure/runtime.py` 和 ORM/migration 是 Aidison 仓库自己的实现，不是把 OpenRath 的 `PostgresRunStore` 复制进来，也不是 Deep Agents/LangGraph 自动引入。

[F] OpenRath 本地源码确有 `src/rath/runtime/postgres.py:PostgresRunStore`，其定位是“Transactional Postgres store using row locks and fencing tokens”，并包含 checkpoint、lease、fencing、interrupt、effect 与 cursor event；它是 Aidison 选择 PostgreSQL durable runtime 的最强协议/故障模型 donor。

[F] LoopX 提供 claim/generation、lease、reconcile、validate→writeback→spend→ack、durable frontier 等控制语义，但它自己的 filesystem/registry/daemon 状态没有进入 Aidison。

[F] ADR-0001 明确拒绝直接把 Deep Agents native task、进程内 `gather()`、第二 LangGraph Server 或第二 Agent runtime 当 durable ledger。

### 作用

PostgreSQL runtime 不是单纯“保存聊天记录”，而是多智能体执行的事务控制层：

1. `Job/Attempt`：一个逻辑任务可以经历多次物理执行。
2. claim/lease：多个 worker 竞争时只有一个获得执行权；worker 死亡后 lease 到期可回收。
3. generation fencing：旧 worker 即使晚回来，也不能覆盖新 generation。
4. `Delegation/JoinGroup/JoinReceipt`：parent fan-out 后只接收合法 child 结果，并只提交一次 Join。
5. cancel/quarantine：取消向下传播；旧、晚到或错误 basis 的结果隔离。
6. Profile/budget freeze：重放仍使用原 Profile、root cap 和 allocation，不因配置变化漂移。
7. Plan history：PlanRevision/Task/Edge/Gap/Patch/ReplanReceipt 可恢复、可比较、可审计。
8. canonical command receipt：Agent 输出只有经 Domain command 幂等晋升后才成为业务事实。

[J] 没有这一层，所谓多 Agent 只是同时运行几个协程；进程崩溃、重复请求、late result 和外部副作用都无法可靠解释。

## 3. LoopX 和 OpenRath 分别影响了什么

### LoopX：偏上层控制逻辑

- typed work graph / durable frontier。
- scoped gate，而不是一个阻塞全局的笼统状态。
- targeted wake：消息只唤醒相关 worker；恢复后重读事实源。
- validate-before-spend。
- claim generation、CAS、write scope、writeback/ack。
- frontstage/action packet 对用户工作台投影的启发。

Aidison 拒绝：filesystem/Markdown/JSONL truth、tmux/TUI 生命周期、外部 daemon 和第二 quota truth。

### OpenRath：偏底层 durable kernel

- PostgreSQL row lock、lease 和 fencing token。
- checkpoint/effect watermark。
- durable interrupt/decision。
- effect class、ambiguous/needs_review 思路。
- cursor event 与 SSE replay。
- crash、cancel、late checkpoint 等 chaos test 场景。

Aidison 拒绝：OpenRath v1/v2 双 runtime、Session lineage 取代 Project Domain、整套 Redis/S3/Kubernetes 运行面。

## 4. 为什么之前没有深入研究 Globex

[F] 当前目录 `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手` 共 42 个文件，全部是 Markdown；没有 `.py/.ts/package.json/pyproject.toml/compose`，也不是 Git 仓库。

[F] 文档内部包含大量示例代码，但没有可直接安装、执行、测试和核对 commit lineage 的完整源码树。

[J] 之前审计按“可运行源码 donor”优先级处理，于是将 Globex 归为教程/审查清单。这解释了为什么没深挖，但相对于用户要求的“完整学习项目复用”仍然是不充分的：它虽然不能证明生产实现，却很适合作为电商搜索算法、工具合同和产品流程 donor。应当补升其参考等级，而不是继续忽略。

## 5. Globex 对 Aidison 产品搜索的价值

### 值得采用或重实现

1. **购物意图结构化**：预算、品类、材质、风格、地区、平台、禁用条件分开表达。
2. **跨平台 fan-out**：不同 marketplace 搜索可并行，合流后再统一比较。
3. **稳定 Candidate schema**：`item_id/platform/title/price/currency/rating/sales/attributes`。
4. **小工具输入输出**：`query/platform/top_k/user_id`，结果带 `total_recall/truncated`，避免把原始 provider payload 塞给模型。
5. **两阶段检索**：结构化硬过滤在前，语义召回/个性化与 rerank 在后。
6. **Query/User/Item 三塔思想**：适合未来有真实商品目录和点击/购买日志后的个性化召回。
7. **跨平台归一与同款去重**：币种、包装数量、SKU/图片 hash、跨语言同品识别。
8. **Landed cost 展示**：商品价、运费、税费、预计时效分开，最终再生成到手价。
9. **ItemPicker/ShoppingSummary**：先确定性过滤和打分，再由模型写购买理由。
10. **评测指标**：Recall@K、MRR、NDCG、约束满足率、推荐完成率和购买转化分层。

### 必须修正后才能进入 Aidison

- Globex 示例使用 `float` 处理金额；Aidison 应继续用 `Decimal` 和明确 rounding policy。
- `FX_RATES`、`DUTY_TABLE`、`SHIPPING_TABLE` 是静态示例；真实系统必须记录来源、地区、时间、品类和失效时间。
- duty 不能只按 marketplace 推断；shipping 不能只按猜测重量。
- `user_id` 不能由模型/请求体随意注入，必须由服务端 principal 绑定。
- 三塔/OpenSearch/Reranker 只有在商品目录和行为数据达到规模后才有收益，不应为 Demo 先部署。
- in-process `asyncio.gather`、全局 `Dict[thread_id, asyncio.Task]` 和 LangGraph checkpoint 不能替代 Aidison PostgreSQL runtime。

## 6. Globex 对 Aidison 购买环节的价值与限制

### 有价值的部分

- 搜索 → 比价 → 运费税费 → shortlist → 用户确认的产品顺序。
- 高金额动作必须 HITL。
- 用户可以中断、追问和修正条件。
- 下单前应明确展示购买理由和约束命中情况。

### 不能直接作为购买基座

[F] Globex 第 11 章明确说明真实平台 API 的 OAuth/合规/反爬接入超出课程范围，实际使用统一 `SearchClient` 抽象。

[F] “最终下单确认”和 `confirm_purchase` 主要出现在面试题描述；当前 42 个 Markdown 中没有对应可运行 cart/order/payment/refund 实现。

[F] Globex 没有证明价格/库存复核、订单幂等、支付 webhook、退款、取消、税费最终确认或 provider reconciliation。

[J] Aidison 当前自己的 `OfferSnapshot/PurchaseProposal/CheckoutHandoff` 与 Shopify adapter 在安全边界上反而更接近真实购买执行。正确组合是：

```text
Globex donor
  → 意图拆解、跨平台搜索、归一、比价、排序、解释、评测

Aidison durable shopping
  → OfferSnapshot、TTL/重新验证、人工确认、幂等 handoff、ambiguous reconciliation

官方 marketplace/provider API
  → 商品、库存、价格、cart、托管 checkout
```

## 7. 建议调整后的参考等级

| 对象 | 原等级 | 建议等级 | 形式 |
|---|---|---|---|
| Globex Agent runtime | 低/教程 | 仍低 | negative fixture；不接入其 in-memory runtime |
| Globex 产品搜索 | 低 | 中高 | DESIGN/ALGORITHM_DONOR，按 Aidison contract 重实现 |
| Globex 比价/到手价 | 未充分评估 | 中 | 采用数据分层与 UX；替换静态表、float 和无来源计算 |
| Globex 个性化召回 | 未充分评估 | 中长期 | 有真实目录/行为数据后再 spike |
| Globex 购买执行 | 低 | 低 | 只有 HITL/流程启发；不能作为 cart/order/payment 代码来源 |

## 8. 下一步最小实施建议

1. 先把 Globex 的搜索思想映射成 Aidison 自有 `ProductSearchQuery/ProductOffer/OfferSnapshot` 合同。
2. 保留现有 Shopify 单店 adapter，增加 provider-neutral search adapter，而不是复制 Globex 示例代码。
3. 加入 `source/observed_at/expires_at/region/currency/shipping/tax/total` 和 price/stock revalidation。
4. 先用两个 provider fixture 做跨平台归一、同款去重和到手价排序；通过后再接真实 marketplace API。
5. 购买仍停在“用户确认后创建 provider-hosted cart/checkout URL”，不自动支付。
6. 三塔、OpenSearch、训练和 RL 等到真实检索质量数据证明需要时再进入。

## 9. 证据位置

- `docs/adr/0001-single-runtime-durable-agent-delegation.md`
- `src/aidison/infrastructure/runtime.py`
- `src/aidison/infrastructure/planning.py`
- `src/aidison/infrastructure/budget.py`
- `src/aidison/application/shopping.py`
- `src/aidison/providers/shopify.py`
- `/home/aluo/project/cankao_ws/新项目/OpenRath-main/src/rath/runtime/postgres.py`
- `/home/aluo/project/cankao_ws/新项目/loopx-main/`
- `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/项目配置篇/09 Globex项目总览与工程初始化.md`
- `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/工具设计篇/11 ItemSearch商品检索工具实现与跨平台fork触发场景.md`
- `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/工具设计篇/12 PriceCompare比价工具与ShippingCalc关税运费工具.md`
- `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/效果评测篇/14 主AgentLoop组装与同质子AgentLoop-fork协同机制.md`
- `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/部署观测篇/15 FastAPI接口与前后端闭环.md`
