"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { LogIn, LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { AUTH_REQUIRED_EVENT, type ApiError, getClient } from "@/lib/api";

type AuthState = "checking" | "authenticated" | "unauthenticated";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>("checking");
  const [username, setUsername] = useState("aidison");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const checkSession = useCallback(async () => {
    try {
      const session = await getClient().getAuthSession();
      setState(session.authenticated ? "authenticated" : "unauthenticated");
    } catch {
      setError("无法确认登录状态，请检查服务后重试。");
      setState("unauthenticated");
    }
  }, []);

  useEffect(() => {
    void checkSession();
    const requireLogin = () => {
      setPassword("");
      setError("会话已过期，请重新登录。登录后会返回当前页面。");
      setState("unauthenticated");
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, requireLogin);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, requireLogin);
  }, [checkSession]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await getClient().login(username.trim(), password);
      setPassword("");
      setState("authenticated");
    } catch (caught) {
      const apiError = caught as ApiError & { status?: number };
      setError(
        apiError.status === 429
          ? "尝试次数过多，请稍后再试。"
          : "用户名或密码不正确。"
      );
    } finally {
      setSubmitting(false);
    }
  };

  const logout = async () => {
    try {
      await getClient().logout();
    } finally {
      setPassword("");
      setError(null);
      setState("unauthenticated");
    }
  };

  if (state === "checking") {
    return (
      <main
        className="auth-shell auth-checking"
        aria-live="polite"
      >
        <span className="auth-loader" />
        正在打开项目空间…
      </main>
    );
  }

  if (state === "unauthenticated") {
    return (
      <main className="auth-shell">
        <div
          className="auth-grid"
          aria-hidden="true"
        />
        <section
          className="auth-card"
          aria-labelledby="auth-title"
        >
          <div
            className="auth-mark"
            aria-hidden="true"
          >
            <img
              src="/art/mc/chest.png"
              alt=""
            />
          </div>
          <div className="auth-heading">
            <span>AIDISON · 私人项目空间</span>
            <h1 id="auth-title">登录 Aidison</h1>
            <p>登录后继续查看你的项目、资料和方案。</p>
          </div>
          <form
            onSubmit={submit}
            className="auth-form"
          >
            <div>
              <Label htmlFor="aidison-username">用户名</Label>
              <Input
                id="aidison-username"
                name="username"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                disabled={submitting}
                required
              />
            </div>
            <div>
              <Label htmlFor="aidison-password">密码</Label>
              <Input
                id="aidison-password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={submitting}
                required
                autoFocus
              />
            </div>
            {error ? (
              <p
                className="auth-error"
                role="alert"
              >
                {error}
              </p>
            ) : null}
            <Button
              type="submit"
              size="lg"
              disabled={submitting}
            >
              <LogIn className="h-4 w-4" />
              {submitting ? "正在登录…" : "登录并继续"}
            </Button>
          </form>
          <small>退出后，其他人无法从这个浏览器打开你的项目。</small>
        </section>
      </main>
    );
  }

  return (
    <>
      {children}
      <Button
        className="auth-logout"
        variant="outline"
        size="sm"
        onClick={() => void logout()}
        title="退出当前安全会话"
      >
        <LogOut className="h-4 w-4" />
        退出
      </Button>
    </>
  );
}
