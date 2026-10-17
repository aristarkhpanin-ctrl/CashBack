// @ts-nocheck
/* eslint-disable */
// Обязательная смена пароля (этап B): bootstrap-админ со сгенерированным
// паролем или сброшенный пароль. До смены сервер принимает токен только на
// /auth/me и /auth/change-password, поэтому вместо приложения — эта форма.
import React, { useState } from "react";
import { authApi } from "@/features/auth/api/authApi";
import { httpStatus } from "@/shared/api/client";
import {
  AuthCard, AuthError, AuthSubmit, authInputStyle, authLabelStyle, retryAfterText,
} from "./Login";

// Политика сервера (security.password_policy_violation): ≥ 12 символов,
// ≤ 72 байт (предел bcrypt), не email, не текущий, не известный дефолт.
const MIN_LENGTH = 12;
const MAX_BYTES = 72;

// detail 422 сервера → понятная причина.
const POLICY_MESSAGES: Array<[string, string]> = [
  ["known default", "Это известный пароль по умолчанию — выберите другой"],
  ["differ from email", "Пароль не должен совпадать с email"],
  ["differ from the current", "Новый пароль должен отличаться от текущего"],
  ["at least", `Пароль должен быть не короче ${MIN_LENGTH} символов`],
  ["at most", `Пароль должен быть не длиннее ${MAX_BYTES} байт`],
];

function changeErrorText(err) {
  const status = httpStatus(err);
  if (status === 401) return "Неверный текущий пароль";
  if (status === 429) return `Слишком много попыток. ${retryAfterText(err)}`;
  if (status === 422) {
    const detail = err?.response?.data?.detail;
    const hit = typeof detail === "string"
      && POLICY_MESSAGES.find(([needle]) => detail.includes(needle));
    return hit ? hit[1] : "Пароль не соответствует требованиям";
  }
  return "Не удалось сменить пароль — сервис недоступен";
}

function ChangePassword({ email, onChanged, onLogout }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const problems = [];
  if (next && next.length < MIN_LENGTH) problems.push(`Не короче ${MIN_LENGTH} символов`);
  if (new TextEncoder().encode(next).length > MAX_BYTES) problems.push(`Не длиннее ${MAX_BYTES} байт`);
  if (next && email && next.trim().toLowerCase() === email.trim().toLowerCase()) {
    problems.push("Не должен совпадать с email");
  }
  if (next && current && next === current) problems.push("Должен отличаться от текущего");
  if (repeat && next !== repeat) problems.push("Пароли не совпадают");
  const ready = !!current && !!next && next === repeat && problems.length === 0;

  async function submit(e) {
    e?.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      await authApi.changePassword(current, next);
    } catch (err) {
      setError(changeErrorText(err));
      setBusy(false);
      return;
    }
    try {
      await onChanged();
    } catch {
      setError("Пароль изменён, но профиль не загрузился — войдите заново");
    }
    setBusy(false);
  }

  return (
    <AuthCard subtitle="Требуется смена пароля" onSubmit={submit}>
      <div style={{ fontWeight: 700, fontSize: 16, color: "#0d1929", marginBottom: 6 }}>Смена пароля</div>
      <div style={{ fontSize: 13, color: "#64748b", lineHeight: 1.5 }}>
        Для <strong>{email}</strong> действует временный пароль. Задайте новый,
        чтобы продолжить работу.
      </div>

      <label style={authLabelStyle}>Текущий пароль</label>
      <input style={authInputStyle} type="password" value={current} autoComplete="current-password"
             aria-label="Текущий пароль" onChange={e => setCurrent(e.target.value)} />

      <label style={authLabelStyle}>Новый пароль (не короче {MIN_LENGTH} символов)</label>
      <input style={authInputStyle} type="password" value={next} autoComplete="new-password"
             aria-label="Новый пароль" onChange={e => setNext(e.target.value)} />

      <label style={authLabelStyle}>Повторите новый пароль</label>
      <input style={authInputStyle} type="password" value={repeat} autoComplete="new-password"
             aria-label="Повторите новый пароль" onChange={e => setRepeat(e.target.value)} />

      {problems.length > 0 && (
        <ul style={{ margin: "12px 0 0", paddingLeft: 18, fontSize: 12, color: "#b45309", lineHeight: 1.6 }}>
          {problems.map(p => <li key={p}>{p}</li>)}
        </ul>
      )}

      {error && <AuthError>{error}</AuthError>}

      <AuthSubmit busy={busy} disabled={!ready}>
        {busy ? "Сохранение…" : "Сменить пароль"}
      </AuthSubmit>
      <button type="button" onClick={onLogout} style={{
        width: "100%", marginTop: 10, padding: "10px 0", borderRadius: 10,
        border: "1px solid #e2e8f0", background: "white", color: "#64748b",
        fontSize: 13, fontWeight: 600, fontFamily: "inherit", cursor: "pointer",
      }}>Выйти</button>
    </AuthCard>
  );
}

export default ChangePassword;
