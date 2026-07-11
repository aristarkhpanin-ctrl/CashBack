// @ts-nocheck
/* eslint-disable */
// Страница входа (фаза 15) — показывается только в live-режиме,
// когда backend отвечает и требует JWT. Демо-режим работает без логина.
import React, { useState } from "react";

function Login({ onLogin }) {
  const [email, setEmail] = useState("admin@bank.ru");
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
    } catch {
      setError("Неверный email или пароль");
    } finally {
      setBusy(false);
    }
  }

  const inputStyle = {
    width: "100%", boxSizing: "border-box", padding: "11px 14px",
    border: "1px solid #e2e8f0", borderRadius: 10, fontSize: 14,
    fontFamily: "inherit", outline: "none", color: "#0d1929",
    background: "white",
  };

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: "linear-gradient(135deg, #0d1929 0%, #0f2040 60%, #16305e 100%)",
      fontFamily: "'Inter', sans-serif", padding: 20,
    }}>
      <form onSubmit={submit} style={{
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
            <div style={{ fontSize: 12, color: "#8896a8" }}>Вход в панель управления</div>
          </div>
        </div>

        <label style={{ fontSize: 12, fontWeight: 600, color: "#374151", display: "block", marginBottom: 6 }}>Email</label>
        <input style={inputStyle} type="email" value={email} autoComplete="username"
               onChange={e => setEmail(e.target.value)} placeholder="admin@bank.ru" />

        <label style={{ fontSize: 12, fontWeight: 600, color: "#374151", display: "block", margin: "16px 0 6px" }}>Пароль</label>
        <input style={inputStyle} type="password" value={password} autoComplete="current-password"
               onChange={e => setPassword(e.target.value)} placeholder="••••••••" />

        {error && (
          <div style={{
            marginTop: 14, padding: "10px 12px", borderRadius: 8, fontSize: 13,
            background: "#fef2f2", border: "1px solid #fca5a5", color: "#dc2626",
          }}>{error}</div>
        )}

        <button type="submit" disabled={busy || !email || !password} style={{
          width: "100%", marginTop: 20, padding: "12px 0", borderRadius: 10,
          border: "none", cursor: busy ? "wait" : "pointer",
          background: "oklch(0.55 0.18 230)", color: "white",
          fontSize: 14, fontWeight: 700, fontFamily: "inherit",
          opacity: busy || !email || !password ? 0.6 : 1, transition: "opacity 0.15s",
        }}>{busy ? "Вход…" : "Войти"}</button>

        <div style={{
          marginTop: 18, padding: "10px 12px", borderRadius: 8,
          background: "#f0f7ff", fontSize: 12, color: "#64748b", lineHeight: 1.5,
        }}>
          Демо-доступы: <strong>admin@bank.ru / admin</strong>,{" "}
          m.sokolova@bank.ru / marketer, d.ivanov@bank.ru / analyst
          (после <code>make seed-demo</code>)
        </div>
      </form>
    </div>
  );
}

export default Login;
