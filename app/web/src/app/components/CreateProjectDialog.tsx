"use client";

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

interface CreateProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (name: string, goal: string) => Promise<void>;
}

export function CreateProjectDialog({
  open,
  onOpenChange,
  onSubmit,
}: CreateProjectDialogProps) {
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    if (!name.trim() || !goal.trim()) return;
    setSubmitting(true);
    try {
      await onSubmit(name.trim(), goal.trim());
      setName("");
      setGoal("");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
    >
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>创建工程项目</DialogTitle>
          <DialogDescription>
            填写作品名称和最初想法。创建后，AI 会先问少量关键问题；任何工作计划仍需你批准才会执行。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 py-4">
          <div className="grid gap-2">
            <Label htmlFor="project-name">项目名称</Label>
            <Input
              id="project-name"
              placeholder="例如：阳台自动浇水装置"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="project-goal">最初想法</Label>
            <Textarea
              id="project-goal"
              placeholder="描述你想做什么；预算、使用环境和细节不确定也没关系，AI 会先向你确认。"
              rows={5}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
            />
          </div>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
          >
            取消
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!name.trim() || !goal.trim() || submitting}
          >
            {submitting ? "正在创建并准备澄清…" : "创建并开始澄清"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
