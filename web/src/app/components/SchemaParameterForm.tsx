"use client";

import { useEffect, useMemo, useState } from "react";
import Form, { type IChangeEvent } from "@rjsf/core";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import validator from "@rjsf/validator-ajv8";
import { RotateCcw, Save } from "lucide-react";
import { Button } from "@/components/ui/button";

interface SchemaParameterFormProps {
  options: Record<string, unknown>;
  busy: boolean;
  onSave: (key: string, value: unknown) => Promise<boolean>;
  onReport?: (feedback: {
    kind: "success" | "conflict" | "forbidden" | "error";
    title: string;
    message: string;
  }) => void;
}

function propertySchema(value: unknown): RJSFSchema {
  if (typeof value === "number" && Number.isFinite(value)) {
    return { type: "number" };
  }
  if (typeof value === "boolean") {
    return { type: "boolean" };
  }
  if (Array.isArray(value)) {
    return {
      type: "array",
      items: value.length ? propertySchema(value[0]) : {},
      uniqueItems: true,
    };
  }
  if (typeof value === "object" && value !== null) {
    return { type: "object" };
  }
  return { type: "string" };
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null) return a === b;
  if (Array.isArray(a) && Array.isArray(b)) {
    return (
      a.length === b.length && a.every((item, index) => deepEqual(item, b[index]))
    );
  }
  if (typeof a === "object" && typeof b === "object") {
    const aObject = a as Record<string, unknown>;
    const bObject = b as Record<string, unknown>;
    const aKeys = Object.keys(aObject);
    const bKeys = Object.keys(bObject);
    return (
      aKeys.length === bKeys.length &&
      aKeys.every((key) => deepEqual(aObject[key], bObject[key]))
    );
  }
  return false;
}

export function SchemaParameterForm({
  options,
  busy,
  onSave,
  onReport,
}: SchemaParameterFormProps) {
  const [formData, setFormData] = useState<Record<string, unknown>>(options);

  useEffect(() => {
    setFormData(options);
  }, [options]);

  const schema = useMemo<RJSFSchema>(
    () => ({
      type: "object",
      properties: Object.fromEntries(
        Object.entries(options).map(([key, value]) => [key, propertySchema(value)])
      ),
      additionalProperties: false,
    }),
    [options]
  );

  const uiSchema = useMemo<UiSchema>(
    () => ({
      "ui:order": Object.keys(options),
      "ui:options": {
        submitButtonOptions: { norender: true },
      },
    }),
    [options]
  );

  const hasChanges = useMemo(
    () =>
      !deepEqual(formData, options) &&
      Object.keys(formData).some((key) => !deepEqual(formData[key], options[key])),
    [formData, options]
  );

  const handleSubmit = async (event: IChangeEvent) => {
    const next = (event.formData ?? {}) as Record<string, unknown>;
    const changedKeys = Object.keys(next).filter(
      (key) => !deepEqual(next[key], options[key])
    );
    if (!changedKeys.length) {
      onReport?.({
        kind: "success",
        title: "没有待保存的参数变更",
        message: "参数与当前草稿一致，无需记录新的调整。",
      });
      return;
    }
    for (const key of changedKeys) {
      const ok = await onSave(key, next[key]);
      if (!ok) break;
    }
  };

  const handleError = () => {
    onReport?.({
      kind: "error",
      title: "参数未通过校验",
      message: "请修正下方标记的字段后重试；保存仍通过后端锁校验执行。",
    });
  };

  return (
    <Form
      className="rjsf-param-form"
      disabled={busy}
      formData={formData}
      idPrefix="param"
      liveValidate={false}
      noHtml5Validate
      onChange={(event) => setFormData(event.formData ?? {})}
      onError={handleError}
      onSubmit={handleSubmit}
      schema={schema}
      uiSchema={uiSchema}
      validator={validator}
    >
      <div className="crafting-param-form-actions">
        <Button
          disabled={busy}
          onClick={() => setFormData(options)}
          size="sm"
          type="button"
          variant="outline"
        >
          <RotateCcw className="h-3.5 w-3.5" />
          重置
        </Button>
        <Button disabled={busy || !hasChanges} size="sm" type="submit">
          <Save className="h-3.5 w-3.5" />
          保存参数调整
        </Button>
      </div>
    </Form>
  );
}
