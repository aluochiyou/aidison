"use client";

import { useEffect, useMemo, useState } from "react";
import Form, { type IChangeEvent } from "@rjsf/core";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import validator from "@rjsf/validator-ajv8";
import { FileCheck2, Save } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { getClient } from "@/lib/api";
import type { ProjectSnapshot } from "@/app/types/types";

type FormData = {
  goal: string;
  hard_constraints: string[];
  preferences: string[];
  available_resources: string[];
  unknowns: string[];
  modules: Array<{
    key: string;
    name: string;
    responsibility: string;
    dependency_keys?: string[];
    acceptance?: string[];
    open_questions?: string[];
  }>;
};

const DEFAULT_MODULES: FormData["modules"] = [
  { key: "structure", name: "结构与接口", responsibility: "定义承载结构、空间边界和机械接口", acceptance: ["关键尺寸可验证", "部件可拆换"], open_questions: ["可用材料和加工方式是什么？"] },
  { key: "power_control", name: "供能与控制", responsibility: "定义供能、控制链路与安全边界", dependency_keys: ["structure"], acceptance: ["功率预算闭合", "失效模式可测试"], open_questions: ["目标负载和续航是多少？"] },
];

const SCHEMA: RJSFSchema = {
  type: "object",
  required: ["goal", "modules"],
  properties: {
    goal: { type: "string", title: "工程目标", minLength: 1 },
    hard_constraints: { type: "array", title: "硬约束", items: { type: "string" } },
    preferences: { type: "array", title: "偏好", items: { type: "string" } },
    available_resources: { type: "array", title: "可用资源", items: { type: "string" } },
    unknowns: { type: "array", title: "待确认问题", items: { type: "string" } },
    modules: {
      type: "array", title: "模块骨架（1–8 个）", minItems: 1, maxItems: 8,
      items: {
        type: "object", required: ["key", "name", "responsibility"],
        properties: {
          key: { type: "string", title: "模块 key", pattern: "^[a-z][a-z0-9_]*$" },
          name: { type: "string", title: "模块名称", minLength: 1 },
          responsibility: { type: "string", title: "职责", minLength: 1 },
          dependency_keys: { type: "array", title: "依赖模块 key", items: { type: "string" }, uniqueItems: true },
          acceptance: { type: "array", title: "验收条件", items: { type: "string" } },
          open_questions: { type: "array", title: "待确认问题", items: { type: "string" } },
        },
      },
    },
  },
};

const UI_SCHEMA: UiSchema = { "ui:options": { submitButtonOptions: { norender: true } } };

function seed(snapshot: ProjectSnapshot): FormData {
  const requirement = snapshot.requirements.find((item) => item.id === snapshot.project.active_requirement_revision_id);
  const modules = snapshot.modules.filter((item) => item.requirement_revision_id === requirement?.id);
  const keys = new Map(modules.map((item) => [item.id, item.key]));
  return {
    goal: requirement?.goal ?? snapshot.project.goal,
    hard_constraints: requirement?.hard_constraints ?? ["Agent 只提交建议，事实写入需要用户确认"],
    preferences: requirement?.preferences ?? ["个人可维护，优先选择可购买和可验证的方案"],
    available_resources: requirement?.available_resources ?? ["Windows 工作站", "常见 DIY 工具"],
    unknowns: requirement?.unknowns ?? ["具体器件、接口和兼容性需要证据确认"],
    modules: modules.length ? modules.map((item) => ({ key: item.key, name: item.name, responsibility: item.responsibility, dependency_keys: item.dependency_ids.map((id) => keys.get(id)).filter((value): value is string => Boolean(value)), acceptance: item.acceptance, open_questions: item.open_questions })) : DEFAULT_MODULES,
  };
}

export function RequirementsRevisionForm({ snapshot, onDone }: { snapshot: ProjectSnapshot; onDone: () => Promise<unknown> }) {
  const initial = useMemo(() => seed(snapshot), [snapshot]);
  const [formData, setFormData] = useState<FormData>(initial);
  const [busy, setBusy] = useState(false);
  useEffect(() => setFormData(initial), [initial]);
  const submit = async ({ formData: value }: IChangeEvent) => {
    const next = value as FormData | undefined;
    if (!next) return;
    setBusy(true);
    try {
      await getClient().approveRequirements(snapshot.project.id, snapshot.project.revision, next);
      toast.success(snapshot.project.active_requirement_revision_id ? "新的需求修订与模块骨架已批准" : "需求版本与模块骨架已批准");
      await onDone();
    } catch (error) {
      const message = error && typeof error === "object" && "error" in error ? (error as { error: { message?: string } }).error.message : error instanceof Error ? error.message : "需求修订未保存";
      toast.error(message ?? "需求修订未保存");
    } finally { setBusy(false); }
  };
  return <div className="action-block"><div className="action-title"><FileCheck2 className="h-5 w-5" /><div><small>{snapshot.project.active_requirement_revision_id ? "修订需求" : "确认需求"}</small><h2>{snapshot.project.active_requirement_revision_id ? "创建新的需求与模块骨架修订" : "批准第一版需求和模块"}</h2></div></div><p>此表单生成新的 RequirementRevision。提交会 supersede 当前模块骨架；历史与已冻结方案不会被静默覆盖。</p><Form className="requirements-rjsf-form" disabled={busy} formData={formData} idPrefix="requirements" liveValidate={false} noHtml5Validate onChange={(event) => setFormData((event.formData as FormData | undefined) ?? initial)} onSubmit={submit} schema={SCHEMA} uiSchema={UI_SCHEMA} validator={validator}><Button disabled={busy} type="submit"><Save className="h-4 w-4" />{busy ? "正在保存…" : snapshot.project.active_requirement_revision_id ? "批准新修订" : "批准需求"}</Button></Form></div>;
}
