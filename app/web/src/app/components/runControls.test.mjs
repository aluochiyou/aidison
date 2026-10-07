import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const dir = dirname(fileURLToPath(import.meta.url));
const runCenterSrc = readFileSync(join(dir, "RunCenter.tsx"), "utf8");
const apiSrc = readFileSync(join(dir, "../../lib/api.ts"), "utf8");

test("运行中心接入唯一的暂停、继续与运行态引导 API", () => {
  assert.ok(apiSrc.includes("createAgentRunControlRequest("));
  assert.ok(apiSrc.includes("resumeAgentRun("));
  assert.ok(apiSrc.includes("/controls"));
  assert.ok(apiSrc.includes("/resume"));
  assert.ok(runCenterSrc.includes("暂停运行"));
  assert.ok(runCenterSrc.includes("继续运行"));
  assert.ok(runCenterSrc.includes("引导后续研究"));
});

test("没有全模块已准入证据的研究预览不能被采纳", () => {
  assert.ok(runCenterSrc.includes("unsupported_module_ids"));
  assert.ok(runCenterSrc.includes("!decision.proposal_preview?.adoption_allowed"));
  assert.ok(runCenterSrc.includes("证据不足，无法采纳"));
});
