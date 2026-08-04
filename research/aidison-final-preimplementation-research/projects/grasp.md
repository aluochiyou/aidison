# GRASP 拆解

- Evidence: imported from prior paper report
- Role: optional evidence-retrieval donor
- Reproduction: `not_checked`

## 价值

SourceSpan→Proposition→Passage rehydration 能在压缩研究上下文的同时把结论重新连接到原文，适合 Aidison 的证据检索。

## 版本位置

V0 使用 PostgreSQL/普通倒排和原始 span。只有 token 或召回瓶颈出现后，才 clean-room 实现 proposition index，并强制 rehydrate passage。

## 拒绝

Proposition 不能脱离 SourceSpan 成为事实；不为 QA graph 先引入 Neo4j，也不复制不可核实现。

## 验证

与 passage/FTS baseline 比较 recall、token、限定词、否定和冲突保留；proposition-only 结果不得进入 evidence gate。

