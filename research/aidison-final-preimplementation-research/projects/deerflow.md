# DeerFlow 拆解

- Evidence: imported from prior two research rounds; source not rescanned
- Role: conditional owned product/runtime base
- Runtime: `not_checked`

## 价值

旧研究确认 DeerFlow 是候选中少数同时覆盖 Next.js、FastAPI、LangGraph harness、checkpoint、stream、subagent、tool、provider 和部署骨架的项目。它最可能缩短 Aidison 的非差异化产品工程。

## 继承与深改

选定 exact SHA 后，代码树直接成为 Aidison。保留产品壳、Gateway、LangGraph 和通过测试的 stream/tool/provider 组件；把 chat/thread-first 产品、Graph state 和运行 DTO 深改成 Project、Module、Evidence、Decision、SolutionVersion 和 Patch。RunManager 可保留，但必须达到 Aidison 的 lease、fencing、idempotency、terminal 对账和 replay 语义。

## 拒绝

Thread/Run/checkpoint 不能成为业务真相；不叠加第二 LangGraph Server、第二 Runner、远程 sandbox、IM/TUI、长期 memory 和企业组件。

## 关键未知与验证

旧证据混有稳定 release 与 2.1-dev/main 的能力，不能预锁 `v2.0.0`。用 3–5 天 spike 比较稳定 release 与近期 main exact SHA，验证 clean WSL2/Compose、checkpoint/SSE/cancel、domain-commit crash、OpenAI/百炼、Project-first UI 和 upgrade replay。失败则转原生 LangGraph 组合路线。

