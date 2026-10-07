import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const dir = dirname(fileURLToPath(import.meta.url));
const component = readFileSync(join(dir, "RequirementsRevisionForm.tsx"), "utf8");
const css = readFileSync(join(dir, "../globals.css"), "utf8");
const types = readFileSync(join(dir, "../types/types.ts"), "utf8");
const api = readFileSync(join(dir, "../../lib/api.ts"), "utf8");

const FORBIDDEN_SCHEMA_TITLES = [
  "模块 key",
  "模块名称",
  "职责",
  "依赖模块 key",
  "验收条件",
  "待确认问题",
  "模块骨架",
];

const REQUIRED_FACT_TITLES = [
  "工程目标",
  "用途 / 使用场景",
  "预算 / 成本范围",
  "知识 / 技能水平",
  "硬约束",
  "偏好",
  "已有资源",
];

test("需求单只展示用户已确认的需求事实，不含模块骨架/键/职责/依赖/验收/待确认问题", () => {
  for (const title of FORBIDDEN_SCHEMA_TITLES) {
    assert.ok(
      !component.includes(`title: "${title}"`),
      `表单 schema 不应再显示字段标题：${title}`
    );
  }
});

test("需求单包含目标/用途/预算/技能/硬约束/偏好/已有资源七个事实字段", () => {
  for (const title of REQUIRED_FACT_TITLES) {
    assert.ok(
      component.includes(`title: "${title}"`),
      `表单 schema 应包含字段标题：${title}`
    );
  }
});

test("长文本字段使用 textarea（目标 + 三个背景字段 + 三个事实数组逐项）", () => {
  const count = component.match(/"ui:widget": "textarea"/g)?.length ?? 0;
  assert.ok(count >= 7, `期望至少 7 处 textarea widget，实际 ${count}`);
});

test("提交时保留 unknowns，但绝不发送模块骨架", () => {
  assert.ok(component.includes("unknowns: formData.unknowns"));
  assert.ok(!component.includes("modules: formData.modules"));
});

test("初始手填表单不写入虚构的资源、偏好、未知项或约束", () => {
  for (const value of [
    "Windows 工作站",
    "常见 DIY 工具",
    "个人可维护，优先选择可购买和可验证的方案",
    "具体器件、接口和兼容性需要证据确认",
  ]) {
    assert.ok(!component.includes(value), `初始表单不应预填：${value}`);
  }
  assert.ok(component.includes("available_resources: requirement?.available_resources ?? []"));
});

test("API 边界与类型同步新增三个可选背景字段，兼容旧 snapshot 缺失", () => {
  for (const key of ["usage_context", "budget_context", "skill_context"]) {
    assert.ok(
      component.includes(`${key}: string`),
      `RequirementsDraft 类型应包含 ${key}`
    );
    assert.ok(
      api.includes(`${key}?: string`),
      `approveRequirements body 应包含可选 ${key}`
    );
    assert.ok(
      types.includes(`${key}?: string`),
      `RequirementRevision 类型应包含可选 ${key}`
    );
  }
});

test("读取 draft 时按别名回退旧字段名，缺失取空字符串", () => {
  for (const key of ["usage_context", "budget_context", "skill_context"]) {
    assert.ok(
      component.includes(`${key}: ["${key}"`),
      `背景字段别名表应以 ${key} 为规范键名`
    );
  }
  assert.ok(component.includes("use_case"));
  assert.ok(component.includes("knowledge_level"));
});

test("表单使用可读的全宽自适应栅格，消除右侧空白", () => {
  assert.match(
    css,
    /\.requirements-rjsf-form\s*\{[^}]*repeat\(auto-fit, minmax\(min\(100%, 300px\), 1fr\)\)/,
    "需求表单应使用 auto-fit 自适应栅格"
  );
  assert.match(
    css,
    /\[id="requirements_goal"\]\s*\{[^}]*grid-column: 1 \/ -1/,
    "工程目标应独占整行（仅目标，不含背景字段）"
  );
});

test("长文本 textarea 完整可见、可纵向扩展", () => {
  const blocks = [...css.matchAll(/textarea\.form-control\s*\{[^}]*\}/g)].map(
    (match) => match[0]
  );
  const requirementsTextarea = blocks.find(
    (block) => /resize: vertical/.test(block) && /min-height/.test(block)
  );
  assert.ok(
    requirementsTextarea,
    "需求表单的 textarea 应同时可纵向扩展且有最小高度"
  );
});
