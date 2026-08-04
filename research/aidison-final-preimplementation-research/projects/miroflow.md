# MiroFlow 拆解

- Evidence: imported from prior paper/repository report
- Role: provider/tool failure-contract donor
- Runtime: `not_checked`

## 价值

输入输出 normalization、错误分类、bounded retry、FailureArtifact 和 task trace 对 Provider/Tool Broker 很有帮助。

## 融合方式

把这些机制写成 Aidison typed protocol 与 test fixture，不迁移整个 orchestrator。FailureArtifact 关联 Attempt、输入 hash、错误、delivery state、retry decision 和可见摘要。

## 拒绝

ambiguous external effect 不能自动重试；不能覆盖旧 JSON 日志、删历史 summary，或把未实现的 graph/checkpoint/replay 当成事实。

## 验证

使用 schema invalid、401、429、quota、timeout、partial stream、5xx、cancel 和 ambiguous effect fixture 测错误映射与重试政策。

