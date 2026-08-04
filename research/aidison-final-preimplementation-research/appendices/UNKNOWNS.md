# 剩余未知项与验证门

## P0：实施前或 S0 必须关闭

| ID | Unknown | Why it matters | Close by | Fail action |
|---|---|---|---|---|
| U3-001 | DeerFlow 稳定 release 与近期 main 的 exact SHA | 决定代码基线与运行能力 | 同口径 adoption spike | 选另一 SHA 或 Route B |
| U3-002 | WSL2/Compose clean start 与文件/卷行为 | 决定 Windows 可重复性 | 两次全新环境 build/start/restore | 修 Compose；仍失败则 Route B |
| U3-003 | checkpoint、node 重执行与 domain commit 的崩溃窗口 | 决定能否安全恢复 | fault injection | 增加 receipt/reconciler；禁止副作用 |
| U3-004 | RunManager lease/fencing/SSE/cancel 真实语义 | 决定是否需要深改 runtime | integration + 100 disconnects | 最小深改；若成第二控制面则 Route B |
| U3-005 | Project-first 前端脱离 chat/thread 的修改量 | 决定复用是否真的省工程 | vertical slice | 改用选择性 UI harvest |
| U3-006 | OpenAI/百炼关键模型 capability | 决定 provider contract 与降级 | live contract tests | capability fail closed |
| U3-019 | 选定 DeerFlow SHA 的 subagent executor 能否承载 durable child Job/interrupt | 决定 Route A 能否复用上游 subagent 骨架 | S0 two-child crash/stale/single-join spike | 只保留叶子代码，由 Aidison runtime 实现 delegation |

## P1：V0 实施中关闭

| ID | Unknown | Validation |
|---|---|---|
| U3-007 | Domain/runtime 的最小物理表集是否足够 | 真实 query/migration/property tests；按聚合和一致性边界拆分，不按逻辑类型机械扩表 |
| U3-008 | 两个只读 ResearchTask 是否优于顺序 | matched-budget fixture ablation |
| U3-009 | PostgreSQL FTS/普通索引是否够用 | evidence corpus latency/recall baseline |
| U3-010 | V0 页面合并后的信息密度 | 真实 pilot usability walkthrough |
| U3-011 | 8–14 周 V0 core 是否现实 | 每个 slice 记录实际工时和删除项；pilot 日历独立记录 |
| U3-012 | 无领域专用核心是否可覆盖三类 DIY | cross-domain fixtures + real pilot |
| U3-020 | episodic/artifact/model attempt 的具体 retention 与删除窗口 | S2 按真实磁盘、恢复与隐私需求定标 |
| U3-021 | 四旋翼材料、地区、法规和真实安全 oracle | pilot 前冻结用户范围；安全项优先 deterministic/manual oracle |
| U3-022 | evaluator 对哪些兼容/安全判断具有可信 oracle | S4 冻结 fixture version、mutation test、grader invalid/cheating gate |

## P2：V1/V2 前关闭

| ID | Unknown | Entry gate |
|---|---|---|
| U3-013 | eBay 或其他 marketplace 的个人生产权限 | credential/access spike + sandbox/production docs |
| U3-014 | AliExpress 等平台的业务模型与区域可用性 | provider-specific spike，不影响核心 contract |
| U3-015 | 动态 deep/wide/manager 策略是否增益 | matched-budget, repeated evaluation |
| U3-016 | proposition/graph/vector 检索是否必要 | baseline 暴露瓶颈且净收益稳定 |
| U3-017 | API 自动下单能否安全开放 | official API、idempotency、reconciliation、approval、webhook 全通过 |
| U3-018 | 物理 Verification/Action adapter 的安全边界 | sandbox、权限、rollback、real-device pilot |

## 保持 `not_checked` 的项目

- 所有上游 runtime tests；
- WSL/Docker 安装已检查；重启后的 Linux daemon、Compose 部署、迁移、备份和恢复仍为 `not_checked`；
- OpenAI/百炼真实请求、价格、延迟、限流和逐模型兼容；
- Shopping production account、报价、checkout、webhook 和交易；
- 性能、token 节省、准确率和交付周期；
- 真实 DIY 设备的安全与物理结果。
