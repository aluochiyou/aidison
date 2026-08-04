# CORAL 拆解

- Evidence: imported from prior paper/repository report
- Role: evaluator and experiment donor
- Runtime: `not_checked`

## 价值

typed score、grader protocol、attempt lineage、grader error 与 mutation testing 有助于把硬门、质量指标和 evaluator 失败分开。

## 版本位置

V0 只建立 evaluator contract 与基础 oracle；V1/V2 在有可信 fixture 后，才评估 best-of-N、shared learning 或 co-evolution。

## 拒绝

不使用 filesystem/worktree 作为业务真相，不把 heartbeat 当数据库 lease，不在普通研究上默认开启 co-evolution，也不依赖 host best-effort isolation。

## 验证

grader mutation 必须捕获 invalid/timeout/partial/cheating/overflow；算法实验使用 matched-budget 并报告方差。

