# Aidison UI

Aidison 的前端工作台。它面向 Project、AgentRun、Evidence、Decision 和成本投影，调用本仓库
FastAPI API；它不是 DeepAgents 的通用聊天界面，也不持有 Agent runtime、模型密钥或项目事实。

## 本地运行

```bash
yarn install
yarn dev
```

生产构建：

```bash
yarn build
```

后端默认运行时是 LangGraph + PostgreSQL：LangGraph 负责每次 AgentRun 的 checkpoint 和 interrupt，
PostgreSQL 负责项目事实、用户批准、结果准入、预算与事件。前端只能发起受权限约束的 API 命令，
不能直接修改这些状态。
