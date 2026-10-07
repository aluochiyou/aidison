import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const dir = dirname(fileURLToPath(import.meta.url));
const consoleSrc = readFileSync(join(dir, "ProjectConsole.tsx"), "utf8");
const requirementsSrc = readFileSync(join(dir, "RequirementsRevisionForm.tsx"), "utf8");
const apiSrc = readFileSync(join(dir, "../../lib/api.ts"), "utf8");

test("需求确认不自动生成研究计划或启动研究", () => {
  assert.ok(!consoleSrc.includes("ensureDefaultPlan"));
  assert.ok(!consoleSrc.includes("onRequirementsApproved"));
  assert.ok(!requirementsSrc.includes("modules: formData.modules"));
});

test("初始结构生成必须由用户显式触发，且只创建待确认提案", () => {
  assert.ok(consoleSrc.includes('case "discover_initial_modules"'));
  assert.ok(consoleSrc.includes("discoverInitialModules("));
  assert.ok(consoleSrc.includes("不会创建研究任务或修改项目模块"));
  assert.ok(apiSrc.includes('`/api/projects/${projectId}/module-discovery`'));
});

test("研究计划只能由 AI 策略入口生成，不能回退到固定计划接口", () => {
  assert.ok(consoleSrc.includes("proposeResearchStrategyExecutionPlan("));
  assert.ok(
    apiSrc.includes('`/api/projects/${projectId}/execution-plans/research-strategy`'),
  );
  assert.ok(!apiSrc.includes("proposeDefaultResearchExecutionPlan"));
  assert.ok(!apiSrc.includes("research-default"));
});

test("未显式降级时默认使用深度研究策略", () => {
  assert.ok(
    consoleSrc.includes('useState<"focused" | "standard" | "deep">("deep")'),
  );
  assert.ok(apiSrc.includes('research_depth: options.researchDepth ?? "deep"'));
  assert.ok(consoleSrc.includes('useState<number | undefined>(undefined)'));
  assert.ok(consoleSrc.includes("留空时默认 200,000,000"));
  assert.ok(consoleSrc.includes("最长研究时长（秒）"));
  assert.ok(consoleSrc.includes("留空时默认 36,000 秒（10 小时）"));
  assert.ok(consoleSrc.includes("深度：按 Coverage 与研究视角持续扩展"));
  assert.ok(consoleSrc.includes("useState(true)"));
  assert.ok(consoleSrc.includes("关键结论独立核验默认开启"));
});

test("对话研究提案也展示服务端冻结的深度默认值", () => {
  const conversationSrc = readFileSync(join(dir, "ConversationPanel.tsx"), "utf8");
  assert.ok(conversationSrc.includes('payload.research_depth ?? "deep"'));
  assert.ok(conversationSrc.includes("深度研究（默认独立核验）"));
  assert.ok(conversationSrc.includes("优先官方资料"));
});

test("新策略草案优先于旧批准计划，用户可在首次运行前重新规划", () => {
  assert.ok(consoleSrc.includes("const latestResearchPlan"));
  assert.ok(consoleSrc.includes("left.created_at.localeCompare(right.created_at)"));
  assert.ok(consoleSrc.includes("请先审核最新策略"));
  assert.ok(consoleSrc.includes("按新边界生成策略草案"));
  assert.ok(consoleSrc.includes("不会自动启动研究，也不会改写当前项目事实"));
});

test("审批卡展示 AI 的决策取舍，而不只展示任务标题", () => {
  assert.ok(consoleSrc.includes("决策取舍：{plan.research_strategy.decision_notes.join"));
});

test("深度研究策略会向用户展示每个任务的可审核研究视角", () => {
  assert.ok(consoleSrc.includes("研究视角：{task.research_lenses.join"));
});

test("失败研究的重新规划面板直接显示安全失败摘要", () => {
  assert.ok(consoleSrc.includes("上一次研究停止原因：{failedResearchRun.latest_error}"));
  assert.ok(consoleSrc.includes("不会复用旧 Run 的私有上下文"));
});
