# Aidison 产品愿景与范围

- Status: sealed design
- Audience: product owner, implementer, reviewer
- Product type: single-user, local-first, general DIY engineering agent
- Implementation status: not started

## 1. 一句话定义

Aidison 把一个模糊的 DIY 目标持续转化为可理解、可比较、可采购、可实施、可验证、可追溯并可局部修订的工程方案。

它不是聊天记录生成器、研究报告工具或通用多 Agent 看板。其核心资产是用户批准的不可变 `SolutionVersion`，报告、BOM、安装指南、控制台和聊天只是这个版本的投影或交互入口。

## 2. “苏纳法”的通用定义

本项目中的“苏纳法”必须是领域无关的方法，不为无人机、相机、NAS、机器人或任何单一 DIY 类别写死流程。设计目标要求任何 DIY 使用同一个闭环：

```text
Goal
  -> RequirementRevision
  -> ModuleGraph
  -> ResearchQuestion
  -> Evidence / Counterevidence
  -> Candidate
  -> CompatibilityFinding
  -> Decision
  -> SolutionVersion + BOM + VerificationPlan
  -> Guided Implementation
  -> VerificationResult / Observation
  -> ImpactAnalysis
  -> PatchSet
  -> New SolutionVersion
```

领域差异只能通过四类扩展进入：

- `SourceAdapter`：从什么来源获得事实；
- `CompatibilityRule`：怎样机械判断某类兼容关系；
- `VerificationAdapter`：怎样验证一个结果；
- `ActionAdapter`：怎样执行一个被批准且可回滚的动作。

某个具体 DIY 可以作为首个真实 pilot，但不能产生专用业务实体、固定 Agent 角色或不可复用的主流程。

“通用”目前是架构约束与待验证目标，不是已经证明覆盖所有 DIY；由三类跨领域 fixture 和一个真实 pilot 关闭 `U3-012`。

## 3. 用户最终要得到什么

完成一个项目后，用户应能直接回答：

1. 我的真实目标、硬约束、偏好和未知分别是什么？
2. 系统把项目拆成了哪些模块，它们怎样依赖和互相约束？
3. 每个重要结论来自哪里，有什么反证、版本、地区和适用条件？
4. 有哪些可行路线，为什么选当前方案而不是备选？
5. 需要购买、复用、下载、制作和配置什么？
6. 怎样安装、组合、测试、发现失败并安全回退？
7. 用户反馈或目标变化后，哪些模块失效，哪些成果仍可复用？

## 4. 产品核心价值

### 4.1 证据不是装饰

搜索结果只产生 `DiscoveryHit`。只有形成稳定的来源快照、精确片段和 Claim 绑定后，才可成为确认过的 Evidence。关键结论必须保留支持、反证、适用条件、时间和版本。

### 4.2 兼容性是整体问题

候选不能只按单项评分。Aidison 必须检查模块接口、尺寸、电气、协议、软件版本、地区、预算、供应和验证可行性，并允许结果保持为 `unknown` 或 `needs_test`。

### 4.3 用户批准的是结构化方案

Agent 只能生成 Proposal。用户批准的是绑定精确需求、证据、候选、BOM、风险和验证计划的 `SolutionVersion`，不是聊天中的一句“好”。

### 4.4 反馈触发局部修订

新反馈先形成 `ImpactAnalysis`。系统只重开直接或传递受影响的模块，复用未受影响的证据和选择；旧版本永远可解释。

### 4.5 工程量服从个人项目边界

V0 只保留能证明闭环的能力。企业级组织、多租户、RBAC、Kubernetes、消息总线、向量数据库、图数据库、工作流设计器和自由 Agent 市场都不是价值证明。

## 5. 目标用户与使用方式

首要用户是项目拥有者本人：有一个实际 DIY 目标，愿意补充约束、检查证据、批准方案、执行步骤并反馈真实观察。

交互方式：

- Windows 11 作为用户桌面；
- Docker Desktop/WSL2 运行后端；
- 浏览器访问 Aidison；
- OpenAI 和阿里云百炼作为优先模型 Provider；
- 本地目录保存 Artifact，PostgreSQL 保存业务事实与运行事件。

## 6. 版本范围

### V0：证明通用闭环

必须包含：

- 创建项目与结构化澄清；
- 需求版本与模块分解；
- 有界多源研究、证据和反证；
- 候选与整体兼容性；
- 主方案、备选、BOM、实施和验证计划；
- 用户 Decision 与不可变 SolutionVersion；
- 用户 Observation、影响分析、局部 Patch 和新版本；
- 可重放运行事件、模块列表与可点击的最小模块详情；
- OpenAI/百炼两种 Provider 的同一核心 contract。

明确不做：

- 自动下单、支付、取消或退款；
- 自动控制物理设备；
- 任意 shell/代码或未知 MCP 的无审批执行；
- 完整模块图控制台和高级 Agent 调试台；
- 多用户、团队、组织、移动端；
- 无边界人格/语义 memory、向量库和图数据库；但必须实现 Working/Episodic/Canonical/Evidence/Preference/Procedural/Artifact 七层治理；
- 动态 swarm、无限递归研究和自由 Agent 角色创建。

### V1：完整交互控制台与辅助采购

- 独立 Research/Evidence 页面、完整模块 drill-down、Evidence Drawer 和 Artifact viewer；
- Decision Inbox、Evidence Drawer、语义版本 Diff；
- 图片、日志和测试 Artifact；
- ShoppingProvider 搜索、`OfferSnapshot`、`PurchaseProposal`、逐项确认和平台托管 checkout handoff；
- 更强的并行只读研究和领域 adapter。

### V2：受控自动化

- 只有 matched-budget 评测证明价值后才增加动态研究策略；
- 只有官方交易 API、个人生产权限、沙箱、地区和幂等/对账全部实测通过后才允许 `place_order`；
- 更强的 Verification/Action adapter；
- 跨项目复用已验证规则，但不共享用户私密事实。

## 7. 成功标准

V0 至少用三个不同领域的 fixture 和一个真实 pilot 验证，同一套领域模型和 LangGraph 不得出现领域专用分支。建议 fixture 覆盖：

- 一项电子/计算 DIY；
- 一项机械或结构 DIY；
- 一项软硬件组合 DIY。

首个真实 pilot 固定为四旋翼无人机，但这一选择不改变通用架构，也不得产生无人机专用实体、Graph 分支、Agent Profile 或工具路由。

每个 fixture 必须：

- 至少一次需求 revision；
- 至少三个模块；
- 关键结论有 snapshot/span；
- 至少一项反证或明确的反证检查；
- 至少一项跨模块兼容性约束；
- 形成不可变方案、BOM 和验证计划；
- 至少一次 Observation 和局部 Patch；
- crash/retry 不产生重复业务提交；
- stale 结果不能覆盖新 revision。

## 8. 产品不变量

1. Agent 不能直接批准、激活、解锁或修改已批准方案。
2. `SolutionVersion` 创建后不可原地修改。
3. LangGraph state、聊天、报告和 UI 都不是 canonical truth。
4. 不确定性必须保持显式；模型不能把 unknown 补成 compatible。
5. 外部副作用必须经过确定性 Gate、用户确认和幂等 receipt。
6. 任何模型、工具或供应商都必须可替换，替换不会改变业务语义。
