"use client";

import { useEffect, useMemo, useState } from "react";
import Form, { type IChangeEvent } from "@rjsf/core";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import validator from "@rjsf/validator-ajv8";
import { FileCheck2, Save, Sparkles } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { getClient } from "@/lib/api";
import type { ProjectSnapshot } from "@/app/types/types";

export type RequirementsDraft = {
  goal: string;
  usage_context: string;
  budget_context: string;
  skill_context: string;
  hard_constraints: string[];
  preferences: string[];
  available_resources: string[];
  unknowns: string[];
};

const SCHEMA: RJSFSchema = {
  type: "object",
  required: ["goal"],
  properties: {
    goal: {
      type: "string",
      title: "工程目标",
      minLength: 1,
      description: "用一句话说清你想让 AI 帮你完成什么。",
    },
    usage_context: {
      type: "string",
      title: "用途 / 使用场景",
      description: "这个东西在哪里、怎么用（例如室内窗边自动浇花、作为宠物喂食器）。",
    },
    budget_context: {
      type: "string",
      title: "预算 / 成本范围",
      description: "你愿意投入的成本范围（例如 500 元以内）。这只是需求约束，不代表已批准支出。",
    },
    skill_context: {
      type: "string",
      title: "知识 / 技能水平",
      description: "你掌握的技能或可用的维护能力（例如基础焊接、会用 3D 建模软件）。",
    },
    hard_constraints: {
      type: "array",
      title: "硬约束",
      description: "必须遵守、无法妥协的限制（例如尺寸、安全要求、接口标准）。",
      items: { type: "string" },
    },
    preferences: {
      type: "array",
      title: "偏好",
      description: "可以权衡的倾向，AI 会优先满足但不强制（例如优先 USB 供电、希望个人可维护）。",
      items: { type: "string" },
    },
    available_resources: {
      type: "array",
      title: "已有资源",
      description: "你已有的材料、工具或设备（例如 3D 打印机、基础焊接能力）。",
      items: { type: "string" },
    },
  },
};

const UI_SCHEMA: UiSchema = {
  "ui:options": { submitButtonOptions: { norender: true } },
  goal: {
    "ui:widget": "textarea",
    "ui:options": { rows: 3, placeholder: "用一句话描述你想让 AI 帮你完成的目标…" },
  },
  usage_context: { "ui:widget": "textarea", "ui:options": { rows: 2 } },
  budget_context: { "ui:widget": "textarea", "ui:options": { rows: 2 } },
  skill_context: { "ui:widget": "textarea", "ui:options": { rows: 2 } },
  hard_constraints: { items: { "ui:widget": "textarea", "ui:options": { rows: 2 } } },
  preferences: { items: { "ui:widget": "textarea", "ui:options": { rows: 2 } } },
  available_resources: { items: { "ui:widget": "textarea", "ui:options": { rows: 2 } } },
};

/** 需求背景字段的兼容别名：新 draft 用 usage_context/budget_context/skill_context，
    旧 draft / 历史快照可能用其他键名，读取时按别名回退，缺失则取空字符串。 */
const BACKGROUND_FIELD_ALIASES: Record<string, string[]> = {
  usage_context: ["usage_context", "usage", "use_case", "usage_scenario", "purpose"],
  budget_context: ["budget_context", "budget", "budget_range", "cost_range", "cost_ceiling"],
  skill_context: ["skill_context", "knowledge_level", "skill_level", "knowledge", "skills"],
};

function readBackgroundField(value: Record<string, unknown>, canonical: string): string {
  const aliases = BACKGROUND_FIELD_ALIASES[canonical] ?? [canonical];
  for (const alias of aliases) {
    const entry = value[alias];
    if (typeof entry === "string") return entry;
  }
  return "";
}

function asRequirementsDraft(value: Record<string, unknown> | null): RequirementsDraft | null {
  if (!value || typeof value.goal !== "string") return null;
  const textList = (item: unknown): string[] =>
    Array.isArray(item) ? item.filter((entry): entry is string => typeof entry === "string") : [];
  return {
    goal: value.goal,
    usage_context: readBackgroundField(value, "usage_context"),
    budget_context: readBackgroundField(value, "budget_context"),
    skill_context: readBackgroundField(value, "skill_context"),
    hard_constraints: textList(value.hard_constraints),
    preferences: textList(value.preferences),
    available_resources: textList(value.available_resources),
    unknowns: textList(value.unknowns),
  };
}

function seed(snapshot: ProjectSnapshot, draft?: Record<string, unknown> | null): RequirementsDraft {
  const usableDraft = asRequirementsDraft(draft ?? null);
  if (usableDraft) return usableDraft;
  const requirement = snapshot.requirements.find((item) => item.id === snapshot.project.active_requirement_revision_id);
  return {
    goal: requirement?.goal ?? snapshot.project.goal,
    usage_context: requirement?.usage_context ?? "",
    budget_context: requirement?.budget_context ?? "",
    skill_context: requirement?.skill_context ?? "",
    hard_constraints: requirement?.hard_constraints ?? [],
    preferences: requirement?.preferences ?? [],
    available_resources: requirement?.available_resources ?? [],
    unknowns: requirement?.unknowns ?? [],
  };
}

export function RequirementsRevisionForm({ snapshot, onDone, draft, onDismissDraft }: { snapshot: ProjectSnapshot; onDone: () => Promise<unknown>; draft?: Record<string, unknown> | null; onDismissDraft?: () => void }) {
  const initial = useMemo(() => seed(snapshot, draft), [snapshot, draft]);
  const [formData, setFormData] = useState<RequirementsDraft>(initial);
  const [busy, setBusy] = useState(false);
  useEffect(() => setFormData(initial), [initial]);
  const submit = async ({ formData: value }: IChangeEvent) => {
    const next = (value ?? {}) as Partial<RequirementsDraft>;
    const goal = typeof next.goal === "string" ? next.goal : formData.goal;
    if (!goal.trim()) return;
    setBusy(true);
    const payload: RequirementsDraft = {
      goal,
      usage_context:
        typeof next.usage_context === "string"
          ? next.usage_context
          : formData.usage_context,
      budget_context:
        typeof next.budget_context === "string"
          ? next.budget_context
          : formData.budget_context,
      skill_context:
        typeof next.skill_context === "string"
          ? next.skill_context
          : formData.skill_context,
      hard_constraints: Array.isArray(next.hard_constraints)
        ? next.hard_constraints
        : formData.hard_constraints,
      preferences: Array.isArray(next.preferences)
        ? next.preferences
        : formData.preferences,
      available_resources: Array.isArray(next.available_resources)
        ? next.available_resources
        : formData.available_resources,
      unknowns: formData.unknowns,
    };
    try {
      await getClient().approveRequirements(snapshot.project.id, snapshot.project.revision, payload);
      toast.success(
        snapshot.project.active_requirement_revision_id
          ? "新的需求修订已批准"
          : "需求已确认；下一步请让 AI 生成模块结构"
      );
    } catch (error) {
      const message = error && typeof error === "object" && "error" in error ? (error as { error: { message?: string } }).error.message : error instanceof Error ? error.message : "需求修订未保存";
      toast.error(message ?? "需求修订未保存");
      setBusy(false);
      return;
    }
    try {
      await onDone();
    } catch {
      // 忽略：需求确认本身已成功，失败信息由页面内的计划提示卡呈现。
    } finally { setBusy(false); }
  };
  return (
    <div className="action-block requirements-approve">
      <div className="action-title">
        <FileCheck2 className="h-5 w-5" />
        <div>
          <small>{snapshot.project.active_requirement_revision_id ? "修订需求" : "确认需求边界"}</small>
          <h2>
            {snapshot.project.active_requirement_revision_id
              ? "更新需求单"
              : "确认这份需求单"}
          </h2>
        </div>
      </div>
      <p>
        AI 已根据你们的对话整理出这份需求单。请核对或补充下面的目标与事实，
        确认后 AI 会只在确认的范围内工作，之后的任何修改都需要你再次确认。
      </p>
      <div className="requirements-ai-note">
        <Sparkles className="h-4 w-4" />
        <span>
          {draft
            ? "这份草案来自对话页 AI 的建议，请逐项审阅或调整后再确认。"
            : "请填写你的实际需求事实；确认后可再显式让 AI 生成模块结构。"}
        </span>
      </div>
      {draft && onDismissDraft ? (
        <div className="requirements-dismiss-row">
          <Button
            size="sm"
            variant="outline"
            onClick={onDismissDraft}
          >
            不使用这份草案，改用手动填写
          </Button>
        </div>
      ) : null}
      <Form
        className="requirements-rjsf-form"
        disabled={busy}
        formData={formData}
        idPrefix="requirements"
        liveValidate={false}
        noHtml5Validate
        onChange={(event) => {
          const next = (event.formData ?? {}) as Partial<RequirementsDraft>;
          setFormData((current) => ({
            goal:
              typeof next.goal === "string" ? next.goal : current.goal,
            usage_context:
              typeof next.usage_context === "string"
                ? next.usage_context
                : current.usage_context,
            budget_context:
              typeof next.budget_context === "string"
                ? next.budget_context
                : current.budget_context,
            skill_context:
              typeof next.skill_context === "string"
                ? next.skill_context
                : current.skill_context,
            hard_constraints: Array.isArray(next.hard_constraints)
              ? next.hard_constraints
              : current.hard_constraints,
            preferences: Array.isArray(next.preferences)
              ? next.preferences
              : current.preferences,
            available_resources: Array.isArray(next.available_resources)
              ? next.available_resources
              : current.available_resources,
            unknowns: current.unknowns,
          }));
        }}
        onSubmit={submit}
        schema={SCHEMA}
        uiSchema={UI_SCHEMA}
        validator={validator}
      >
        <Button
          disabled={busy}
          type="submit"
        >
          <Save className="h-4 w-4" />
          {busy
            ? "正在保存…"
            : snapshot.project.active_requirement_revision_id
            ? "批准新修订"
            : "确认需求单"}
        </Button>
      </Form>
    </div>
  );
}
