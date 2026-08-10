# Aidison V3：算法逻辑、新颖性与可靠性审计

- 审计日期：2026-08-10
- 审计范围：V3 方案、`src/aidison/`、unit / isolated PostgreSQL integration、浏览器工作台冒烟和一次淘宝只读搜索。
- 相关完成度：见 [V3 完成度审计](v3-completion-audit.md)。本文只判断技术逻辑、可安全宣称的新颖性和可靠性，不把功能清单等同于算法价值。

## 结论

Aidison 的 V3 价值是**可恢复、可审计、用户授权约束下的多智能体执行基础设施**，不是一篇提出全新调度算法的研究工作。它将 ready-set 调度、不可变计划、确定性汇合、结果验证、调用回放、预算账本和人工授权放入同一 PostgreSQL 事实源中，解决了多智能体系统中“模型看似成功、状态却不可证明或不可恢复”的工程问题。

在简历或面试中，合理表述为“设计并实现可靠多智能体编排 runtime”；不应表述为“提出原创通用 DAG 算法”“已证明任意复杂任务的自主规划能力”或“生产级高可用”。

## 核心算法逻辑

| 机制 | 算法/状态机要点 | 解决的问题 | 可验证证据 |
|---|---|---|---|
| ready-set 调度 | 每轮只 claim 依赖已经满足的 Task；以数据库 lock、lease、generation/fencing 和幂等 task→child-job binding 竞争领取 | 固定 wave barrier 会让已经就绪的后继任务无谓等待；多 worker 会重复派发 | `application/ready_set.py`；`test_ready_set_scheduler.py` 覆盖 diamond、竞争、重入、lease reclaim 与 retry |
| immutable plan + basis/CAS | planner 输出不可变 `PlanRevision`；执行与结果均携带 revision、task key 与 basis hash；过期结果进入 quarantine | replan 后旧 worker 覆盖新事实 | `runtime/planning.py`、`application/execution.py`、plan/revision integration tests |
| deterministic join | `ALL_REQUIRED`、`BOUNDED_PARTIAL`、`FIRST_VALID` 用同一状态评估函数；winner 按完成时间和 UUID 确定，收敛时取消 sibling 并结算预算 | 并行分支的收敛、取消、迟到结果和资源泄漏 | `runtime/contracts.py`、`test_join_policy_evaluation.py`、`test_join_policies.py` |
| result admission + verification | 成功 AttemptResult 不会自动成为 canonical input；先执行 schema、evidence 数量/去重、artifact 必填等确定性策略，拒绝和 quarantine 也留下记录 | “任务成功”掩盖无证据、格式错误或不满足约束的建议 | `runtime/verification.py`、`infrastructure/result_admissions.py`、`test_result_verification.py` |
| evidence ranking | exact URL 去重、SimHash 近重复抑制、BM25 与 authority/title 规则稳定排序，保留 snapshot/provenance | 检索噪声与重复证据污染 proposal | `research/evidence_ranking.py`、`test_research_evidence_ranking.py` |
| effect-aware replay | provider/model 调用前写 job-stable `PENDING`；成功保存 artifact，未知外部 effect 标成 `AMBIGUOUS` 而非盲重试；reclaim 复用记录 | 崩溃窗口的重复调用、重复外部副作用和预算重复扣减 | `runtime/replay.py`、`infrastructure/replay.py`、`test_invocation_replay.py` |
| budget + capability gate | root/child allocation 与每次 operation ledger 持久化；在工具 dispatch 前检查冻结 profile、allowed effect、deadline 和 reservation | 子任务超配预算或绕过已批准的能力范围 | `infrastructure/budget.py`、`tools/capabilities.py`、ready-set / capability tests |
| ExecutionPlanProposal | 用户批准目标、并发、预算、能力和确认点；入口以 basis 与 scope hash fail-closed 校验，随后冻结到 Job | 用户看不见或无法限制 Agent 的实际工作边界 | [ADR-0007](../adr/0007-approved-execution-plan-runtime-gate.md)、API closed-loop integration |

这些机制不是彼此独立的“功能点”：计划冻结提供结果的判定基准；ready-set 派发 durable child；join 只消费 admitted result；replay 保护 child 的物理调用；预算和 capability gate 约束可执行范围；ExecutionPlanProposal 则将用户意图变成入口约束。

## 新颖性判断

### 可以成立的工程创新

1. **统一事实源的可靠编排闭环**：计划、领取、预算、结果、人工批准和外部 effect 都落在 PostgreSQL，而不是把 LangGraph/LLM 内存状态当成事实源。
2. **结果不是天然可信的**：通过独立 admission/verification 层，把 Agent 输出变成“待证明的 staged proposal”，这是比仅检查任务状态更严格的系统边界。
3. **恢复语义与外部调用绑定**：replay 不是简单 retry；它区分尚未调用、已成功、外部状态未知三类窗口，并保留未知状态用于人工处理。
4. **用户可控性进入 runtime**：ExecutionPlanProposal 不是 UI 提示，而是创建 Job 前的 server-side capability gate；工作台中的结构、选择与参数也有 revision/lock/history。

### 不应夸大的部分

- ready-set、lease/fencing、CAS、BM25、SimHash、幂等键、事件唤醒和 saga/effect recovery 都是已有成熟思想；V3 的贡献是有边界的组合、契约化和测试，而非发明这些算法。
- 当前 planner 是有界 typed plan，不是无限深度、任意图结构的自治 DAG planner。
- 当前 VERIFY 是确定性规则验证，尚不是能解释所有领域正确性的通用 verifier；LLM-as-judge 仍后置。
- 没有多模型路由/ESCALATE 的真实价值证据；仅一个有效模型 profile 时不应伪造“模型自适应”。

## 可靠性论证

### 已被当前证据支持的保证

| 可靠性主张 | 支持范围 | 边界 |
|---|---|---|
| 同一 ready task 不会被竞争 scheduler 重复创建 child job | isolated PostgreSQL integration 覆盖 racing tick、idempotent tick、reclaim | 不是跨地域多主数据库证明 |
| 失败/retry/lease reclaim 不会沿用过期 claim | generation fencing、terminal supersede、retry claim 测试 | 依赖 PostgreSQL 的事务与时钟语义 |
| 不满足 JoinPolicy 的结果不进入 parent 收敛 | join policy 与 cancellation/late-result tests | 不证明 Agent 内容本身正确 |
| 未通过结构/证据/artifact 规则的结果不会写入 canonical join | deterministic verification/admission tests | 规则覆盖范围需要随领域扩展 |
| 已录制调用在 reclaim 时不重复同一受控 provider 操作 | fault-injection/replay tests，记录状态 `PENDING` → terminal | 真实模型/provider 的长期重放仍未 live 验收 |
| 用户未批准或 scope/basis 不匹配的运行请求会被拒绝 | domain/API execution-plan tests | 不是 IAM/RBAC；当前是单用户 control-plane gate |
| 淘宝不产生购买副作用 | adapter capability 与 handoff/cart fail-closed tests | 一次真实搜索不等于持续可用、库存或最终价格保证 |

### 验收强度

- 本地静态/单元：Ruff、strict mypy（66 source files）、`338 passed` unit。
- 隔离数据库：从 Alembic head 迁移 `aidison_test` 后，PostgreSQL integration `70 passed`，覆盖 transaction、claim、reclaim、join、API closed loop。
- UI：production build 通过；浏览器冒烟验证项目创建、需求批准、模块点击、参数 revision/history 持久化。
- 外部只读：淘宝 TOP material search 曾返回并解析关键词商品；该证据只覆盖搜索边界。

### 仍需补齐的可靠性证据

1. 生产运行库 migration/drift（不可替代为测试库结果）。
2. 真实 DeepSeek + Tavily/GitHub + Artifact + Join + Domain 的重复、故障与长程稳定性测试。
3. 并发/负载门槛、数据库故障恢复时间与容量基准。
4. LangSmith 真实脱敏 trace/dataset 的受控验证；它只能评估质量趋势，不能替代确定性 correctness gate。
5. 认证主体、RBAC/双人审批与 actor audit；当前 ExecutionPlanProposal/EffectApproval 不应被宣传为完整 IAM。

## 代码图谱辅助发现

对 `src/aidison/` 的静态图谱（66 个源码文件）产生 1,505 个节点和 5,210 条有向边；核心桥接节点是 `PostgresDomainStore`、`DomainStore`、`ResearchWorker` 与 `ProjectApplication`。这与“Domain 是唯一事实源、Agent 输出经过应用层提交”的架构判断一致。

图谱的 AST 提取报告 443 条 dangling edge 和 241 条同端点合并，因此它只用于定位模块关系，**不作为运行时正确性或性能证据**。可视化与原始报告保存在 `graphify-out/`，属于本地分析产物，不纳入产品发布物。

## 面试表述模板

> 我没有试图把 LLM 当成可靠状态机，而是让它只产出 typed proposal。系统用 PostgreSQL 保存不可变计划、任务 claim、预算和结果账本；调度器以 ready-set 增量领取任务，JoinPolicy 确定性收敛，并在崩溃后根据 invocation recording 决定回放、恢复或人工审查。这样可将多智能体系统的可靠性从“模型这次看起来答对了”转为可测试的状态转换、不变量和故障窗口处理。

这段表述应同时说明边界：现有证据是本地与隔离 PostgreSQL 验收；真实模型的长期稳定性、压力基准和生产 IAM 尚未完成。
