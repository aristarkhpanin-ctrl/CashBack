// @ts-nocheck
// Shared UI primitives (ported from Cashback2.zip → UI.jsx).
/* eslint-disable */
import React from "react";
import { Icon } from "./Icon";

// ── Status Badge ──────────────────────────────────────────────────────────────
export const STATUS_CONFIG = {
  active:    { label: "Активна",        bg: "#dcfce7", color: "#16a34a", dot: "#16a34a" },
  paused:    { label: "Приостановлена", bg: "#fef9c3", color: "#ca8a04", dot: "#ca8a04" },
  completed: { label: "Завершена",      bg: "#f1f5f9", color: "#64748b", dot: "#94a3b8" },
  draft:     { label: "Черновик",       bg: "#f0f7ff", color: "#3b82f6", dot: "#3b82f6" },
};

export function StatusBadge({ status }) {
  const cfg = STATUS_CONFIG[status] || STATUS_CONFIG.draft;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      padding: "3px 10px", borderRadius: 20, fontSize: 12, fontWeight: 600,
      background: cfg.bg, color: cfg.color,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: cfg.dot, flexShrink: 0 }}></span>
      {cfg.label}
    </span>
  );
}

// ── Card ──────────────────────────────────────────────────────────────────────
export function Card({ children, style = {}, onClick, hover = false }) {
  const [hovered, setHovered] = React.useState(false);
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => hover && setHovered(true)}
      onMouseLeave={() => hover && setHovered(false)}
      style={{
        background: "white",
        borderRadius: 12,
        border: "1px solid #e8edf4",
        boxShadow: hovered ? "0 4px 20px rgba(0,0,0,0.08)" : "0 1px 3px rgba(0,0,0,0.04)",
        transition: "box-shadow 0.2s, transform 0.2s",
        transform: hovered ? "translateY(-1px)" : "none",
        cursor: onClick ? "pointer" : "default",
        ...style,
      }}
    >
      {children}
    </div>
  );
}

// ── Stat Card ─────────────────────────────────────────────────────────────────
/** «vs предыдущие 30 дней» / «vs предыдущий 1 день» — окно тренда (B11). */
export function trendWindowLabel(days) {
  const n = Math.abs(days) % 100, d = n % 10;
  if (d === 1 && n !== 11) return `vs предыдущий ${days} день`;
  const word = d >= 2 && d <= 4 && (n < 12 || n > 14) ? "дня" : "дней";
  return `vs предыдущие ${days} ${word}`;
}

// trend: undefined — строки тренда нет; null — тренда нет (малая база, B11)
// → «—»; 0 — без изменений, нейтрально; иначе ▲/▼ (сервер режет до ±999.9).
function TrendValue({ trend }) {
  if (trend == null) {
    return <span data-trend="none" style={{ color: "#94a3b8", fontSize: 12, fontWeight: 600 }}>—</span>;
  }
  if (trend === 0) {
    return <span data-trend="flat" style={{ color: "#64748b", fontSize: 12, fontWeight: 600 }}>0%</span>;
  }
  const up = trend > 0;
  const abs = Math.abs(trend);
  return (
    <span data-trend={up ? "up" : "down"} style={{ color: up ? "#16a34a" : "#dc2626", fontSize: 12, fontWeight: 600 }}>
      {up ? "▲" : "▼"} {abs >= 999.9 ? "> 999" : abs}%
    </span>
  );
}

export function StatCard({
  label, value, sub, trend, trendLabel = "vs предыдущий период",
  color = "oklch(0.65 0.18 230)", icon,
}) {
  return (
    <Card style={{ padding: "20px 24px" }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
        <div>
          <div style={{ fontSize: 12, color: "#8896a8", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 8 }}>{label}</div>
          <div style={{ fontSize: 28, fontWeight: 700, color: "#0d1929", fontFamily: "'JetBrains Mono', monospace", letterSpacing: "-1px" }}>{value}</div>
          {sub && <div style={{ fontSize: 12, color: "#8896a8", marginTop: 4 }}>{sub}</div>}
        </div>
        {icon && (
          <div style={{
            width: 40, height: 40, borderRadius: 10,
            background: color + "20",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            {/* Фаза 27: icon — kebab-имя Lucide (не emoji); цвет наследует акцент. */}
            {typeof icon === "string"
              ? <Icon name={icon} size={20} color={color} />
              : <span style={{ fontSize: 18 }}>{icon}</span>}
          </div>
        )}
      </div>
      {trend !== undefined && (
        <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 4 }}>
          <TrendValue trend={trend} />
          <span style={{ fontSize: 12, color: "#8896a8" }}>{trendLabel}</span>
        </div>
      )}
    </Card>
  );
}

// ── Состояние live-виджета (B10) ──────────────────────────────────────────────
// Live-виджет не подменяет данные демо-цифрами: пока идёт запрос — «Загрузка»,
// пустой ответ — «нет данных», ошибка — HTTP-статус и «Повторить».
export function WidgetStatus({
  state, error, onRetry, empty = "Нет данных за выбранный период", minHeight = 120,
  forbidden = "Нет права «Аналитика» — показатель недоступен",
}) {
  const box = {
    minHeight, display: "flex", flexDirection: "column", alignItems: "center",
    justifyContent: "center", gap: 10, textAlign: "center", padding: "16px 20px",
    borderRadius: 10, fontSize: 13, boxSizing: "border-box", width: "100%",
  };
  if (state === "loading") {
    return <div data-widget-state="loading" style={{ ...box, color: "#94a3b8" }}>Загрузка…</div>;
  }
  if (state === "forbidden") {
    return (
      <div data-widget-state="forbidden" style={{ ...box, color: "#64748b", background: "#f8fafc" }}>
        {forbidden}
      </div>
    );
  }
  if (state === "error") {
    return (
      <div role="alert" data-widget-state="error"
        style={{ ...box, color: "#b91c1c", background: "#fef2f2", border: "1px solid #fecaca" }}>
        <div style={{ fontWeight: 600 }}>Не удалось загрузить данные — {error}</div>
        {onRetry && <Button variant="secondary" size="sm" onClick={onRetry}>Повторить</Button>}
      </div>
    );
  }
  return <div data-widget-state="empty" style={{ ...box, color: "#94a3b8", background: "#f8fafc" }}>{empty}</div>;
}

// ── Progress Bar ──────────────────────────────────────────────────────────────
export function ProgressBar({ value, max, color = "oklch(0.65 0.18 230)", height = 6 }) {
  const pct = Math.min(100, Math.round((value / max) * 100));
  return (
    <div style={{ background: "#f1f5f9", borderRadius: 99, height, overflow: "hidden" }}>
      <div style={{
        height: "100%", borderRadius: 99,
        background: color,
        width: `${pct}%`, transition: "width 0.4s ease",
      }} />
    </div>
  );
}

// ── Table ─────────────────────────────────────────────────────────────────────
export function Table({ columns, rows, onRowClick }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <thead>
          <tr style={{ borderBottom: "2px solid #f1f5f9" }}>
            {columns.map(col => (
              <th key={col.key} style={{
                textAlign: col.align || "left",
                padding: "10px 16px",
                fontSize: 11, fontWeight: 700,
                color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px",
                whiteSpace: "nowrap",
              }}>{col.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}
              onClick={() => onRowClick && onRowClick(row)}
              style={{
                borderBottom: "1px solid #f8fafc",
                cursor: onRowClick ? "pointer" : "default",
                transition: "background 0.1s",
              }}
              onMouseEnter={e => { if (onRowClick) e.currentTarget.style.background = "#f8fafc"; }}
              onMouseLeave={e => { e.currentTarget.style.background = ""; }}
            >
              {columns.map(col => (
                <td key={col.key} style={{ padding: "12px 16px", textAlign: col.align || "left", color: "#374151" }}>
                  {col.render ? col.render(row[col.key], row) : row[col.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Button ─────────────────────────────────────────────────────────────────────
export function Button({ children, variant = "primary", onClick, disabled, size = "md", style: extStyle = {}, title }) {
  const [hov, setHov] = React.useState(false);
  const base = {
    primary:   { bg: "oklch(0.55 0.18 230)", hbg: "oklch(0.50 0.18 230)", color: "white", border: "none" },
    secondary: { bg: "white", hbg: "#f8fafc", color: "#374151", border: "1px solid #e2e8f0" },
    danger:    { bg: "#fee2e2", hbg: "#fecaca", color: "#dc2626", border: "1px solid #fca5a5" },
    ghost:     { bg: "transparent", hbg: "#f1f5f9", color: "#64748b", border: "none" },
    success:   { bg: "oklch(0.55 0.18 160)", hbg: "oklch(0.50 0.18 160)", color: "white", border: "none" },
  }[variant];
  const sizes = { sm: { padding: "5px 12px", fontSize: 12 }, md: { padding: "8px 16px", fontSize: 13 }, lg: { padding: "11px 22px", fontSize: 14 } }[size];
  return (
    <button
      onClick={onClick} disabled={disabled} title={title}
      onMouseEnter={() => setHov(true)}
      onMouseLeave={() => setHov(false)}
      style={{
        background: hov ? base.hbg : base.bg, color: base.color, border: base.border,
        borderRadius: 8, fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.5 : 1, transition: "all 0.15s",
        display: "inline-flex", alignItems: "center", gap: 6,
        fontFamily: "inherit", ...sizes, ...extStyle,
      }}
    >{children}</button>
  );
}

// ── Input ─────────────────────────────────────────────────────────────────────
// readOnly — значение показывается, но не редактируется (hint — почему).
export function Input({ label, value, onChange, placeholder, type = "text", required, suffix, prefix, readOnly = false, hint }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {label && <label style={{ fontSize: 12, fontWeight: 600, color: "#374151" }}>{label}{required && !readOnly && <span style={{ color: "#ef4444" }}> *</span>}</label>}
      <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
        {prefix && <span style={{ position: "absolute", left: 10, fontSize: 13, color: "#94a3b8", zIndex: 1 }}>{prefix}</span>}
        <input
          type={type} value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
          readOnly={readOnly} aria-readonly={readOnly || undefined}
          style={{
            width: "100%", padding: `9px ${suffix ? 40 : 12}px 9px ${prefix ? 30 : 12}px`,
            border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 13,
            color: readOnly ? "#64748b" : "#0d1929", background: readOnly ? "#f8fafc" : "white",
            fontFamily: "inherit", cursor: readOnly ? "default" : "text",
            outline: "none", transition: "border-color 0.15s", boxSizing: "border-box",
          }}
          onFocus={e => { if (!readOnly) (e.target as HTMLInputElement).style.borderColor = "oklch(0.65 0.18 230)"; }}
          onBlur={e => (e.target as HTMLInputElement).style.borderColor = "#e2e8f0"}
        />
        {suffix && <span style={{ position: "absolute", right: 10, fontSize: 13, color: "#94a3b8" }}>{suffix}</span>}
      </div>
      {hint && <div style={{ fontSize: 11, color: "#94a3b8" }}>{hint}</div>}
    </div>
  );
}

// ── Select ─────────────────────────────────────────────────────────────────────
export function Select({ label, value, onChange, options, required }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {label && <label style={{ fontSize: 12, fontWeight: 600, color: "#374151" }}>{label}{required && <span style={{ color: "#ef4444" }}> *</span>}</label>}
      <select value={value} onChange={e => onChange(e.target.value)} style={{
        padding: "9px 12px", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 13,
        color: "#0d1929", background: "white", fontFamily: "inherit", outline: "none",
        cursor: "pointer",
      }}>
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </div>
  );
}

// ── SectionHeader ─────────────────────────────────────────────────────────────
export function SectionHeader({ title, action }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
      <h3 style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", margin: 0 }}>{title}</h3>
      {action}
    </div>
  );
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
export function Tabs({ tabs, active, onChange }) {
  return (
    <div style={{ display: "flex", gap: 0, borderBottom: "2px solid #f1f5f9", marginBottom: 24 }}>
      {tabs.map(tab => (
        <button key={tab.id} onClick={() => onChange(tab.id)} style={{
          padding: "10px 20px", fontSize: 13, fontWeight: active === tab.id ? 700 : 500,
          color: active === tab.id ? "oklch(0.55 0.18 230)" : "#64748b",
          background: "none", border: "none", cursor: "pointer",
          borderBottom: active === tab.id ? "2px solid oklch(0.55 0.18 230)" : "2px solid transparent",
          marginBottom: -2, transition: "all 0.15s",
        }}>{tab.label}</button>
      ))}
    </div>
  );
}

// ── Modal ─────────────────────────────────────────────────────────────────────
export function Modal({ open, onClose, title, children, width = 560 }) {
  if (!open) return null;
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
    }} onClick={onClose}>
      <div style={{
        background: "white", borderRadius: 16, width, maxWidth: "90vw", maxHeight: "85vh",
        overflow: "auto", boxShadow: "0 20px 60px rgba(0,0,0,0.2)",
      }} onClick={e => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "20px 24px", borderBottom: "1px solid #f1f5f9" }}>
          <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: "#0d1929" }}>{title}</h3>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", fontSize: 20, color: "#94a3b8", padding: "0 4px" }}>×</button>
        </div>
        <div style={{ padding: 24 }}>{children}</div>
      </div>
    </div>
  );
}

// ── Toast ─────────────────────────────────────────────────────────────────────
export function Toast({ message, type = "success", onClose }) {
  React.useEffect(() => { const t = setTimeout(onClose, 3000); return () => clearTimeout(t); }, []);
  const colors = { success: { bg: "#f0fdf4", border: "#86efac", color: "#16a34a" }, error: { bg: "#fef2f2", border: "#fca5a5", color: "#dc2626" }, warning: { bg: "#fffbeb", border: "#fcd34d", color: "#b45309" }, info: { bg: "#eff6ff", border: "#93c5fd", color: "#3b82f6" } };
  const c = colors[type];
  return (
    <div style={{
      position: "fixed", bottom: 24, right: 24, zIndex: 9999,
      background: c.bg, border: `1px solid ${c.border}`,
      borderRadius: 10, padding: "12px 20px",
      display: "flex", alignItems: "center", gap: 10,
      boxShadow: "0 4px 16px rgba(0,0,0,0.1)", fontSize: 13, fontWeight: 600, color: c.color,
      animation: "slideIn 0.2s ease",
    }}>
      {type === "success" ? "✓" : type === "error" ? "✗" : type === "warning" ? "⚠" : "ℹ"} {message}
    </div>
  );
}
