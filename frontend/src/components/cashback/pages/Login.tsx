// @ts-nocheck
/* eslint-disable */
// Страница входа (фаза 15) — показывается только в live-режиме,
// когда backend отвечает и требует JWT. Демо-режим работает без логина.
// Этап B: никаких подсказок с учётными данными — пароль bootstrap-админа
// задаёт оператор (ADMIN_BOOTSTRAP_PASSWORD) или он печатается один раз.
import React, { useState } from "react";
import { httpStatus } from "@/shared/api/client";

export const authInputStyle = {
  width: "100%", boxSizing: "border-box", padding: "11px 14px",
  border: "1px solid #e2e8f0", borderRadius: 10, fontSize: 14,
  fontFamily: "inherit", outline: "none", color: "#0d1929",
  background: "white",
};

export const authLabelStyle = {
  fontSize: 12, fontWeight: 600, color: "#374151", display: "block", margin: "16px 0 6px",
};

/** Карточка экранов входа и смены пароля. */
export function AuthCard({ subtitle, onSubmit, children }) {
  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: "linear-gradient(135deg, #0d1929 0%, #0f2040 60%, #16305e 100%)",
      fontFamily: "'Inter', sans-serif", padding: 20,
    }}>
      <form onSubmit={onSubmit} style={{
        width: 380, background: "white", borderRadius: 16, padding: "36px 32px",
        boxShadow: "0 24px 64px rgba(0,0,0,0.35)",
      }}>
        {/* Logo */}
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
          <div style={{
            width: 42, height: 42, borderRadius: 12,
            background: "oklch(0.65 0.18 230)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <svg width="22" height="22" viewBox="0 0 20 20" fill="none">
              <path d="M3 10C3 6.686 6.134 4 10 4s7 2.686 7 6-3.134 6-7 6H3" stroke="white" strokeWidth="1.8" strokeLinecap="round"/>
              <path d="M7 10h6M10 7v6" stroke="white" strokeWidth="1.8" strokeLinecap="round"/>
            </svg>
          </div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 17, color: "#0d1929" }}>CashBack Admin</div>
            <div style={{ fontSize: 12, color: "#8896a8" }}>{subtitle}</div>
          </div>
        </div>
        {children}
      </form>
    </div>
  );
}

export function AuthError({ children }) {
  return (
    <div role="alert" style={{
      marginTop: 14, padding: "10px 12px", borderRadius: 8, fontSize: 13,
      background: "#fef2f2", border: "1px solid #fca5a5", color: "#dc2626",
    }}>{children}</div>
  );
}

export function AuthSubmit({ busy, disabled, children }) {
  return (
    <button type="submit" disabled={busy || disabled} style={{
      width: "100%", marginTop: 20, padding: "12px 0", borderRadius: 10,
      border: "none", cursor: busy ? "wait" : "pointer",
      background: "oklch(0.55 0.18 230)", color: "white",
      fontSize: 14, fontWeight: 700, fontFamily: "inherit",
      opacity: busy || disabled ? 0.6 : 1, transition: "opacity 0.15s",
    }}>{children}</button>
  );
}

/** «Повторите через N мин» из Retry-After (секунды) ответа 429. */
export function retryAfterText(err) {
  const secs = parseInt(err?.response?.headers?.["retry-after"] ?? "", 10);
  if (!Number.isFinite(secs) || secs <= 0) return "Повторите попытку позже.";
  return secs < 60
    ? `Повторите через ${secs} с.`
    : `Повторите через ${Math.ceil(secs / 60)} мин.`;
}

function loginErrorText(err) {
  const status = httpStatus(err);
  if (status === 401) return "Неверный email или пароль";
  if (status === 429) return `Слишком много попыток входа. ${retryAfterText(err)}`;
  return "Сервис входа недоступен — попробуйте позже";
}

function Login({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e?.preventDefault();
    if (!email || !password || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onLogin(email.trim().toLowerCase(), password);
    } catch (err) {
      setError(loginErrorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard subtitle="Вход в панель управления" onSubmit={submit}>
      <label style={{ ...authLabelStyle, margin: "0 0 6px" }}>Email</label>
      <input style={authInputStyle} type="email" value={email} autoComplete="username"
             onChange={e => setEmail(e.target.value)} placeholder="name@bank.ru" />

      <label style={authLabelStyle}>Пароль</label>
      <input style={authInputStyle} type="password" value={password} autoComplete="current-password"
             onChange={e => setPassword(e.target.value)} placeholder="••••••••" />

      {error && <AuthError>{error}</AuthError>}

      <AuthSubmit busy={busy} disabled={!email || !password}>
        {busy ? "Вход…" : "Войти"}
      </AuthSubmit>
    </AuthCard>
  );
}

export default Login;
