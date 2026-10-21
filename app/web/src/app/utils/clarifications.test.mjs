import { test } from "node:test";
import assert from "node:assert/strict";
import { dedupeClarifications } from "./clarifications.ts";

test("dedupes duplicate question texts while keeping first-seen order", () => {
  const items = [
    { id: "a", question: "预算多少？" },
    { id: "b", question: "预算多少？" },
    { id: "c", question: "需要联网吗？" },
  ];
  assert.deepEqual(
    dedupeClarifications(items).map((item) => item.question),
    ["预算多少？", "需要联网吗？"]
  );
});

test("filters out questions the user already answered", () => {
  const items = [
    { id: "a", question: "预算多少？" },
    { id: "c", question: "需要联网吗？" },
  ];
  assert.deepEqual(
    dedupeClarifications(items, ["预算多少？"]).map((item) => item.id),
    ["c"]
  );
});

test("returns empty array for empty input", () => {
  assert.deepEqual(dedupeClarifications([]), []);
});
