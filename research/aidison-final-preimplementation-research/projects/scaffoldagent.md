# ScaffoldAgent 拆解

- Evidence: imported from prior paper report
- Role: optional research-outline donor
- Reproduction: `not_checked`

## 价值

outline expand/contract/revise、bounded patch、utility vector 和 stop reason 适合表达研究计划的局部调整。

## 版本位置

V0 使用静态 ResearchQuestion/Task 列表和机械预算。若真实任务频繁出现“只需局部重规划”，V1 再 clean-room 实现 `ResearchOutline` projection 与 CAS patch。

## 拒绝

Outline 不是 canonical solution，模型 utility 不能自动批准，也不能原地修改已批准方案。

## 验证

对目标 subtree 做 patch 时，未影响部分 hash 必须保持不变；与 static outline/简单 heuristic 做 matched-budget 比较。

