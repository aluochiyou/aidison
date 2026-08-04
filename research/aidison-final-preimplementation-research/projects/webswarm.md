# WebSwarm 拆解

- Evidence: imported from prior paper/repository report
- Role: optional research-strategy donor
- Runtime: `not_checked`

## 价值

`atom/deep/wide/entity_collect` verbs 和 revise/expand 提供按问题形状选择研究策略的思路。

## 版本位置

不进入 V0。V0 先冻结静态 bounded Research 基线；只有真实 fixture 证明特定问题需要 deep/wide/entity 策略时，才作为 feature-gated strategy plugin 进入 V1/V2。

## 拒绝

ThreadPool/in-memory trace、每个 Agent 独立大预算、默认递归和把论文榜单直接外推到工程 DIY。

## 验证

与静态顺序/两 worker 基线使用相同模型、tool、token、wall-clock 总预算，比较 evidence coverage、错误率、成本和方差；必须支持 stale quarantine。

