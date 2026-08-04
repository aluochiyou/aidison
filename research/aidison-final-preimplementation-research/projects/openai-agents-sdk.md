# OpenAI Agents SDK 拆解

- Evidence: imported from prior reports
- Role: execution design reference, not V0 runtime
- Runtime: `not_checked`

## 价值

SDK 的 Runner、tool/handoff、guardrail、RunState、stream 和测试资产为 typed agent execution、usage 与错误行为提供成熟参照。

## Aidison 裁决

本轮已经确定 LangGraph 是唯一 Agent runtime，因此 V0 不再叠加 Agents SDK Runner。值得吸收的是 provider/tool guardrail、typed result、sensitive trace 控制和 fake-model contract-test 方法，而不是第二套执行循环。

## 拒绝

Session/RunState 不能充当 Job scheduler、lease、项目历史或 canonical solution；不能把 provider trace 默认上传，也不 fork 大型 RunState 形成嵌套控制面。

## 未来条件

只有 LangGraph 无法覆盖某个窄能力，且独立 spike 证明 adapter 的收益超过双 runtime 成本时，才重新评估单向 adapter。默认答案仍是直接调用 OpenAI Responses provider。

