import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const dir = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(dir, "ProjectConsole.tsx"), "utf8");

test("玩家工作区聚焦讨论和模块方案两个主入口", () => {
  const start = source.indexOf('className="console-page-switch"');
  const end = source.indexOf("</div>", start);
  const primaryNavigation = source.slice(start, end);

  assert.ok(primaryNavigation.includes("项目讨论"));
  assert.ok(primaryNavigation.includes("模块与方案"));
  assert.ok(!primaryNavigation.includes("运行链路"));
  assert.ok(!primaryNavigation.includes("集成监控"));
});

test("场景侧栏显示真实项目数据并连接现有诊断视图", () => {
  assert.ok(source.includes('className="project-world-rail"'));
  assert.ok(source.includes('src="/art/mc/steve-pig-cutout.png"'));
  assert.ok(source.includes("snapshot.modules.length"));
  assert.ok(source.includes("snapshot.evidence.length"));
  assert.ok(source.includes("snapshot.solutions.length"));
  assert.ok(source.includes("setAuditOpen(true)"));
  assert.ok(source.includes('setView("integration-health")'));
});
