"use client";

import { useState, useEffect } from "react";
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
import { resetClient, getClient } from "@/lib/api";

interface ApiSettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ApiSettingsDialog({ open, onOpenChange }: ApiSettingsDialogProps) {
  const [apiUrl, setApiUrl] = useState("");
  const [status, setStatus] = useState<"unknown" | "ok" | "error">("unknown");
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (open) {
      setApiUrl(getClient().getBaseUrl());
      setStatus("unknown");
    }
  }, [open]);

  const handleSave = () => {
    resetClient(apiUrl);
    onOpenChange(false);
  };

  const handleTest = async () => {
    setTesting(true);
    try {
      const res = await fetch(`${apiUrl.replace(/\/$/, "")}/health`);
      if (res.ok) {
        setStatus("ok");
      } else {
        setStatus("error");
      }
    } catch {
      setStatus("error");
    }
    setTesting(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle>API Settings</DialogTitle>
          <DialogDescription>
            Configure the Aidison backend API endpoint.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 py-4">
          <div className="grid gap-2">
            <Label htmlFor="api-url">API URL</Label>
            <div className="flex gap-2">
              <Input
                id="api-url"
                placeholder="http://localhost:8000"
                value={apiUrl}
                onChange={(e) => { setApiUrl(e.target.value); setStatus("unknown"); }}
              />
              <Button variant="outline" onClick={handleTest} disabled={testing}>
                {testing ? "Testing..." : "Test"}
              </Button>
            </div>
            {status === "ok" && (
              <p className="text-xs text-green-600">Connection successful</p>
            )}
            {status === "error" && (
              <p className="text-xs text-destructive">Connection failed</p>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
