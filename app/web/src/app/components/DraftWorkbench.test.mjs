import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const dir = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(dir, "DraftWorkbench.tsx"), "utf8");

test("模块依赖图把编辑步骤、修改理由和清空草稿暴露给用户", () => {
  assert.ok(source.includes("拖动模块底部连线到另一个模块以新增依赖"));
  assert.ok(source.includes("修改理由（可选）"));
  assert.ok(source.includes("清空关系草稿"));
  assert.ok(source.includes("relationshipChangeReason"));
});

test("关系变更仍以可审核提案提交，并保留用户理由", () => {
  assert.ok(source.includes("createProjectReshape("));
  assert.ok(source.includes("用户调整模块依赖关系"));
  assert.ok(source.includes("请先审核并应用"));
});

test("计划批准收到 HTTP 524 时先刷新项目事实，不盲目重提", () => {
  const recoveryStart = source.indexOf(
    'if (decision === "approved" && status === 524)'
  );
  const recoveryEnd = source.indexOf(
    "report(describeApiError(error));",
    recoveryStart
  );
  assert.notEqual(recoveryStart, -1);
  assert.notEqual(recoveryEnd, -1);
  const recovery = source.slice(recoveryStart, recoveryEnd);
  assert.ok(recovery.includes("await onRefresh()"));
  assert.ok(recovery.includes('item.status === "approved"'));
  assert.ok(recovery.includes("批准结果仍未知"));
  assert.ok(recovery.includes("避免重复提交"));
});

test("API 错误提示展示安全追踪编号，便于关联服务端与 Langfuse 记录", () => {
  assert.ok(source.includes("correlation_id?: string"));
  assert.ok(source.includes("追踪编号：{feedback.reference}"));
});
