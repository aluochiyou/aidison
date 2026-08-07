# Aidison 版本收口学习与面试文档计划

- 状态：active，随开发维护事实素材；版本收口时生成最终文档。
- 权威来源：Git 与测试结果、accepted specs、ADR、`development-decisions.md`。
- 原则：最终材料解释架构和核心代码路径，但不逐文件复述实现；所有性能、稳定性和完成度表述必须有验收证据。

## 开发期间持续维护

每个 architecture slice 收口时必须：

1. 新增或更新 ADR，记录 problem、decision、alternatives、validation、consequences；
2. 在 `development-decisions.md` 记录实现细节、踩坑、锁/事务/幂等边界和面试主题；
3. 更新 `ARCHITECTURE.md`、`STATUS.md` 与精确测试结果；
4. 标注参考项目的参考强度，以及参考的是代码、算法、产品逻辑还是 UI；
5. 保存关键 review finding 与修正结论，但不把临时 Agent 对话当事实源。

## 版本收口交付目录

`docs/learning/`：

- `01-project-architecture.md`：系统边界、状态所有权、请求与 Agent 执行时序；
- `02-core-algorithms.md`：lease/fencing、JoinPolicy、CAS replan、receipt replay、预算账本、effect gate；
- `03-tech-stack-and-tradeoffs.md`：技术栈、版本、选择原因、未选择方案；
- `04-development-roadmap.md`：从 V0 到当前版本的开发顺序及为什么这样拆；
- `05-source-reading-map.md`：Aidison 核心入口与参考项目必读/选读/无需读源码部分；
- `06-core-code-walkthrough.md`：核心代码路径、关键不变量和故障恢复，不逐行覆盖全部代码；
- `07-engineering-practice.md`：数据库迁移、测试隔离、配置、worktree、多 Agent review、踩坑与修复。

`docs/interview/`：

- `resume-project-bullets.md`：可证实、可量化的简历 bullet 与技术关键词；
- `project-introduction.md`：30 秒、2 分钟、5 分钟项目介绍；
- `interview-qa.md`：基础、进阶、追问、反例和 trade-off 问答；
- `deep-dive-stories.md`：STAR 形式讲述难点、故障、创新和工程决策；
- `claims-and-evidence.md`：每项简历主张对应 commit、测试或文档证据，防止过度包装。

## 收口 gate

只有在目标版本代码合并并完成最终 PostgreSQL、前后端和真实外部服务验收后，才把上述文档标为 complete。外部服务未稳定验证的能力必须写作 `not_checked` 或实验性能力，不能包装成生产稳定结论。
