# LangSmith 真实 live evaluation 验收记录（2026-08-10）

本记录只描述本轮实际执行过的命令与观察结果，作为 LangSmith 观测适配器的真实上报证据。
脱敏的完整 JSON 报告见 `docs/status/evaluation-langsmith-live-20260810.json`，报告中不含任何
API key、环境变量名或 `lsv2_` 令牌片段。

## 结论

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| live evaluation 命令退出码 | `0` | 见下方命令输出 |
| 真实 LangSmith 项目中可定位此次 run | `passed` | run `204556f7-a4be-5108-898a-714f5b7d03c6` 可读回；URL 不含密钥 |
| 每 case 的 feedback 写入 | `passed` | 5 条 `case_status` feedback，score=1.0，value=passed |
| 相关单测 | `37 passed` | `tests/unit/test_evaluation_*` 四个测试文件 |
| 离线回归 | `5 passed` | `python -m aidison.evaluation --mode offline` |
| 报告脱敏 | `passed` | JSON 无 `api_key`/`lsv2`/`LANGSMITH`/`sk-` |

## 本轮执行

启用环境变量并运行 live 模式（reporter 严格 opt-in，缺 `AIDISON_EVAL_LANGSMITH_ENABLED` 时默认关闭）：

```bash
AIDISON_EVAL_LANGSMITH_ENABLED=1 .venv/bin/python -m aidison.evaluation \
  --mode live --report docs/status/evaluation-langsmith-live-20260810.json
```

输出：

```
evaluation live (reporter=langsmith): 5 passed, 0 failed, 0 errored, 0 skipped
exit 0
```

### 真实项目回读（只读 API，无密钥输出）

同一进程内先加载 `.env` 供 SDK 客户端读取密钥，再以 `Client(api_key=..., api_url=...)`
回读 run 并生成 URL：

```text
project:     aidison-v3
run_id:      204556f7-a4be-5108-898a-714f5b7d03c6
run name:    aidison-evaluation
run type:    chain
tags:        ['aidison-evaluation', 'mode:live']
run URL:     https://smith.langchain.com/o/8eb869fd-81cb-48ed-ba0e-ff0c663c8dd2/projects/p/7c2f31f1-455e-44d8-b304-3c9bea80074b/r/204556f7-a4be-5108-898a-714f5b7d03c6?poll=true
feedback:    5 x case_status (score=1.0, value=passed)
```

说明：`run_id` 由 `derive_run_id(mode, case_keys)` 确定性派生，因此同一组 case 的 live run
ID 稳定为 `204556f7-a4be-5108-898a-714f5b7d03c6`。这与 `docs/status/evaluation-live-20260810.json`
一致；本轮证据证明该 run 至今仍可在真实项目中被回读与定位。

## 涉及代码（本任务边界内）

- `src/aidison/evaluation/langsmith_reporter.py` — reporter 向 SDK `create_run` 传 `id`（非无效的 `run_id`），
  并将 `run_type` 从未被官方接受的 `evaluation` 改为 `chain`，用 tags 保留 evaluation 语义；fail-closed 吞错。
- `src/aidison/evaluation/fixtures.py` — 排版修正（黑格式化），无行为变化。
- `tests/unit/test_evaluation_langsmith_reporter.py` — 断言跟随 `id`/`run_type=chain`/tags，并新增
  `LANGSMITH_PROJECT` 标准环境变量映射测试。

本任务未改动前端、业务 Agent、数据库迁移或任何用户文件；未读取或输出 `.env` 中的秘密。

## 风险 / follow-up

- SDK 0.10.15 对 `create_feedback(run_id=...)` 不传 `session_id` 会打印 `LangSmithWarning`，提示未来版本
  将要求显式 session/project 关联。当前 SDK 仍可用且 feedback 已写入，但需按官方
  `smithdb-sdk-migration#feedback-create` 迁移说明在后续调整。与 `v3-real-acceptance-20260810.md`
  中已记录的 follow-up 一致。
- `read_run()` / `get_run_url()` 在本版本被标记为 deprecated（将迁移到 `client.runs.retrieve()` /
  `client.runs.get_url()`）；本记录仅为验证使用，业务代码未依赖这两个方法。
- `.env` 第 40 行存在 `python-dotenv` 解析格式警告，但所需 LangSmith 值可被正常加载；本轮未修改用户 `.env`。
