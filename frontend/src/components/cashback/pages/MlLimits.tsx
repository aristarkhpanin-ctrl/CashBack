// @ts-nocheck
/* eslint-disable */
import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import * as Recharts from "recharts";
import {
  USERS, ROLE_LABELS, PERMISSIONS, MCC_CATEGORIES, SEGMENTS,
  CAMPAIGNS, generateTrendData, MCC_HEATMAP, FUNNEL_DATA,
  MATRIX_DATA, CHANNEL_DATA, computeActivityMatrix,
} from "@/data/mockData";
import {
  StatusBadge, Card, StatCard, ProgressBar, Table,
  Button, Input, Select, SectionHeader, Tabs, Modal, Toast,
  STATUS_CONFIG,
} from "../UI";

const AppData = {
  USERS, ROLE_LABELS, PERMISSIONS, MCC_CATEGORIES, SEGMENTS,
  CAMPAIGNS, generateTrendData, MCC_HEATMAP, FUNNEL_DATA,
  MATRIX_DATA, CHANNEL_DATA, computeActivityMatrix,
};



// ── Defaults stored in localStorage ───────────────────────────────────────────
const DEFAULT_LIMITS = {
  premium:  { maxCashback: 15, minCashback: 3, dailyBudget: 200000, autoApprove: true,  riskLevel: "medium" },
  business: { maxCashback: 12, minCashback: 3, dailyBudget: 150000, autoApprove: true,  riskLevel: "medium" },
  young:    { maxCashback: 10, minCashback: 2, dailyBudget: 100000, autoApprove: false, riskLevel: "high"   },
  mass:     { maxCashback: 7,  minCashback: 1, dailyBudget: 80000,  autoApprove: false, riskLevel: "low"    },
  senior:   { maxCashback: 8,  minCashback: 2, dailyBudget: 60000,  autoApprove: true,  riskLevel: "low"    },
};

const SEGMENT_COLORS = {
  premium:  { bg: "oklch(0.96 0.04 280)", color: "oklch(0.40 0.18 280)", accent: "oklch(0.55 0.20 280)" },
  business: { bg: "oklch(0.96 0.04 230)", color: "oklch(0.40 0.18 230)", accent: "oklch(0.55 0.20 230)" },
  young:    { bg: "oklch(0.96 0.05 40)",  color: "oklch(0.45 0.18 40)",  accent: "oklch(0.60 0.20 40)"  },
  mass:     { bg: "oklch(0.96 0.04 160)", color: "oklch(0.35 0.18 160)", accent: "oklch(0.55 0.20 160)" },
  senior:   { bg: "oklch(0.96 0.03 200)", color: "oklch(0.40 0.15 200)", accent: "oklch(0.55 0.18 200)" },
};

const RISK_OPTIONS = [
  { value: "low",    label: "Низкий риск",   desc: "Консервативные рекомендации",      color: "oklch(0.55 0.18 160)" },
  { value: "medium", label: "Средний риск",  desc: "Балансная стратегия",              color: "oklch(0.55 0.18 230)" },
  { value: "high",   label: "Высокий риск",  desc: "Агрессивные предложения, высокий ROI", color: "oklch(0.55 0.20 30)"  },
];

function MlLimits({ currentUser, liveLimits }) {
  const { SEGMENTS } = AppData;
  const isLive = !!liveLimits?.enabled;

  // Демо-режим: localStorage; live-режим: стартуем с дефолтов и
  // подменяем стейт данными из GET /ml-limits, когда они приходят.
  const [limits, setLimits] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("ml_limits"));
      return saved || DEFAULT_LIMITS;
    } catch { return DEFAULT_LIMITS; }
  });
  const [savedLimits, setSavedLimits] = useState(limits);
  const [toast, setToast] = useState(null);
  const [globalEnabled, setGlobalEnabled] = useState(() => {
    try { return JSON.parse(localStorage.getItem("ml_global_enabled")) ?? true; }
    catch { return true; }
  });
  const [savedGlobal, setSavedGlobal] = useState(globalEnabled);

  // Live-инициализация из БД (фаза 18): единожды на приход данных.
  const liveInitial = liveLimits?.initial;
  useEffect(() => {
    if (!isLive || !liveInitial) return;
    setLimits(liveInitial.limits);
    setSavedLimits(liveInitial.limits);
    setGlobalEnabled(liveInitial.globalEnabled);
    setSavedGlobal(liveInitial.globalEnabled);
  }, [isLive, liveInitial]);

  const isAdmin = currentUser.role === "admin";
  const isDirty = JSON.stringify(limits) !== JSON.stringify(savedLimits)
    || globalEnabled !== savedGlobal;

  function updateLimit(segId, field, value) {
    setLimits(prev => ({
      ...prev,
      [segId]: { ...prev[segId], [field]: value }
    }));
  }

  async function saveAll() {
    if (isLive) {
      const ok = await liveLimits.save(limits, globalEnabled);
      if (!ok) return; // ошибка показана интерцептором API-клиента
      setToast({
        msg: "Лимиты записаны в БД — BRE (правило R7) применит их к ближайшей выдаче",
        type: "success",
      });
    } else {
      localStorage.setItem("ml_limits", JSON.stringify(limits));
      localStorage.setItem("ml_global_enabled", JSON.stringify(globalEnabled));
      setToast({ msg: "Лимиты сохранены и применены ко всем ML-рекомендациям", type: "success" });
    }
    setSavedLimits(limits);
    setSavedGlobal(globalEnabled);
  }

  function resetAll() {
    setLimits(savedLimits);
    setGlobalEnabled(savedGlobal);
    setToast({ msg: "Изменения отменены", type: "info" });
  }

  function resetToDefaults() {
    setLimits(DEFAULT_LIMITS);
    setToast({ msg: "Установлены значения по умолчанию", type: "info" });
  }

  const totalDailyBudget = Object.values(limits).reduce((s, l) => s + l.dailyBudget, 0);
  const avgMaxCashback = (Object.values(limits).reduce((s, l) => s + l.maxCashback, 0) / Object.values(limits).length).toFixed(1);
  const autoApproveCount = Object.values(limits).filter(l => l.autoApprove).length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24, maxWidth: 1280, paddingBottom: 40 }}>

      {/* Intro */}
      <div style={{
        background: "white", borderRadius: 12, padding: "20px 24px",
        border: "1px solid #e8edf4",
        display: "flex", alignItems: "flex-start", gap: 16,
      }}>
        <div style={{
          width: 44, height: 44, borderRadius: 12, flexShrink: 0,
          background: "linear-gradient(135deg, oklch(0.65 0.18 230), oklch(0.65 0.18 280))",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 22,
        }}>🛡️</div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", marginBottom: 4 }}>
            Контроль ML-предложений
          </div>
          <div style={{ fontSize: 13, color: "#64748b", lineHeight: 1.5 }}>
            Здесь задаются <strong style={{ color: "#0d1929" }}>максимальные лимиты</strong>, в пределах которых ML-модель может генерировать персональные кэшбэк-предложения. Это страховка от того, чтобы модель не предложила клиенту экономически невыгодную ставку.
          </div>
        </div>
        {!isAdmin && (
          <div style={{
            background: "#fef3c7", color: "#92400e",
            padding: "6px 12px", borderRadius: 20,
            fontSize: 12, fontWeight: 600, whiteSpace: "nowrap",
          }}>Только просмотр (нужны права администратора)</div>
        )}
      </div>

      {/* Global toggle */}
      <div style={{
        background: "white", borderRadius: 12, padding: "18px 24px",
        border: "1px solid #e8edf4",
        display: "flex", alignItems: "center", gap: 16,
      }}>
        <ToggleSwitch
          value={globalEnabled}
          onChange={isAdmin ? setGlobalEnabled : null}
        />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: "#0d1929" }}>
            ML-рекомендации {globalEnabled ? "включены" : "отключены"} глобально
          </div>
          <div style={{ fontSize: 12, color: "#64748b" }}>
            {globalEnabled
              ? "Модель генерирует предложения для всех сегментов в рамках указанных лимитов"
              : "Все ML-предложения приостановлены, используются только ручные правила"}
          </div>
        </div>
        <div style={{
          padding: "5px 12px", borderRadius: 99,
          background: globalEnabled ? "#dcfce7" : "#fee2e2",
          color: globalEnabled ? "#15803d" : "#dc2626",
          fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px",
        }}>
          {globalEnabled ? "● Активна" : "○ Пауза"}
        </div>
      </div>

      {/* Summary stats */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
        <SummaryCard label="Средний лимит кэшбэка" value={avgMaxCashback + "%"} sub="по всем сегментам" icon="📊" color="oklch(0.55 0.18 230)" />
        <SummaryCard label="Общий дневной бюджет" value={"₽" + (totalDailyBudget/1000).toFixed(0) + "К"} sub="на ML-предложения" icon="💰" color="oklch(0.45 0.18 160)" />
        <SummaryCard label="Авто-одобрение" value={autoApproveCount + " из " + Object.keys(limits).length} sub="сегментов без модерации" icon="⚡" color="oklch(0.55 0.20 30)" />
      </div>

      {/* Segments */}
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <SectionHeader
          title="Лимиты по сегментам"
          action={isAdmin && (
            <button onClick={resetToDefaults} style={{
              background: "none", border: "none", cursor: "pointer",
              fontSize: 12, color: "#64748b", textDecoration: "underline", fontFamily: "inherit",
            }}>Восстановить значения по умолчанию</button>
          )}
        />
        {SEGMENTS.map(seg => (
          <SegmentLimitCard
            key={seg.id}
            segment={seg}
            limit={limits[seg.id]}
            color={SEGMENT_COLORS[seg.id]}
            onUpdate={(field, value) => updateLimit(seg.id, field, value)}
            readOnly={!isAdmin}
          />
        ))}
      </div>

      {/* Sticky save bar */}
      {isAdmin && isDirty && (
        <div style={{
          position: "sticky", bottom: 0, marginTop: 8,
          background: "white", borderRadius: 12, padding: "14px 20px",
          border: "1px solid oklch(0.65 0.18 230)",
          boxShadow: "0 -4px 20px rgba(13,25,41,0.12)",
          display: "flex", alignItems: "center", gap: 14, zIndex: 10,
        }}>
          <div style={{
            width: 32, height: 32, borderRadius: "50%",
            background: "oklch(0.95 0.05 230)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 16,
          }}>●</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#0d1929" }}>Есть несохранённые изменения</div>
            <div style={{ fontSize: 12, color: "#64748b" }}>Нажмите «Сохранить» чтобы применить новые лимиты к ML-модели</div>
          </div>
          <Button variant="secondary" onClick={resetAll}>Отменить</Button>
          <Button variant="primary" onClick={saveAll}>Сохранить</Button>
        </div>
      )}

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}

// ── Summary Card ──────────────────────────────────────────────────────────────
function SummaryCard({ label, value, sub, icon, color }) {
  return (
    <div style={{ background: "white", borderRadius: 12, padding: "18px 22px", border: "1px solid #e8edf4" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 4 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>{label}</div>
        <div style={{ fontSize: 18, opacity: 0.6 }}>{icon}</div>
      </div>
      <div style={{ fontSize: 26, fontWeight: 700, color, fontFamily: "'JetBrains Mono', monospace", letterSpacing: "-0.5px", marginTop: 4 }}>{value}</div>
      <div style={{ fontSize: 12, color: "#94a3b8", marginTop: 2 }}>{sub}</div>
    </div>
  );
}

// ── Segment Limit Card ────────────────────────────────────────────────────────
function SegmentLimitCard({ segment, limit, color, onUpdate, readOnly }) {
  const fmtNum = n => n >= 1000 ? (n/1000).toFixed(0) + "К" : n;
  const fmtRub = n => "₽" + n.toLocaleString("ru");

  // Validation
  const minMaxConflict = limit.minCashback >= limit.maxCashback;

  return (
    <div style={{ background: "white", borderRadius: 14, border: "1px solid #e8edf4", overflow: "hidden" }}>
      {/* Header */}
      <div style={{
        padding: "16px 20px",
        background: color.bg,
        borderBottom: `1px solid ${color.accent}25`,
        display: "flex", alignItems: "center", gap: 14,
      }}>
        <div style={{
          width: 40, height: 40, borderRadius: 10,
          background: color.accent,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "white", fontWeight: 700, fontSize: 14, flexShrink: 0,
        }}>{segment.name[0]}</div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>{segment.name}</div>
          <div style={{ fontSize: 12, color: color.color, fontWeight: 600 }}>
            {fmtNum(segment.count)} клиентов · текущий лимит ставки {limit.maxCashback}%
          </div>
        </div>
        <RiskBadge value={limit.riskLevel} />
      </div>

      {/* Body */}
      <div style={{ padding: "18px 20px", display: "grid", gridTemplateColumns: "2fr 1fr 1fr", gap: 24, alignItems: "flex-start" }}>

        {/* Range slider — min/max cashback */}
        <div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 12 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#374151", textTransform: "uppercase", letterSpacing: "0.5px" }}>
              Диапазон ставки кэшбэка
            </span>
            <span style={{ fontSize: 13, fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: color.color }}>
              {limit.minCashback}% — {limit.maxCashback}%
            </span>
          </div>

          <DualSlider
            min={1} max={30}
            valueMin={limit.minCashback}
            valueMax={limit.maxCashback}
            onChangeMin={v => onUpdate("minCashback", v)}
            onChangeMax={v => onUpdate("maxCashback", v)}
            color={color.accent}
            disabled={readOnly}
          />

          {minMaxConflict && (
            <div style={{ fontSize: 11, color: "#dc2626", marginTop: 6, fontWeight: 600 }}>
              ⚠ Минимальная ставка должна быть меньше максимальной
            </div>
          )}

          <div style={{ display: "flex", gap: 16, marginTop: 10, fontSize: 11, color: "#94a3b8" }}>
            <span>Мин. — нижний порог рекомендации</span>
            <span>Макс. — потолок, выше которого модель не предложит</span>
          </div>
        </div>

        {/* Daily budget */}
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#374151", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 8 }}>
            Дневной бюджет
          </div>
          <NumericInput
            value={limit.dailyBudget}
            onChange={v => onUpdate("dailyBudget", v)}
            min={0} step={10000}
            suffix="₽"
            disabled={readOnly}
          />
          <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>
            Лимит расхода на ML-предложения в сегменте
          </div>
        </div>

        {/* Auto-approve + risk */}
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#374151", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 8 }}>
            Авто-одобрение
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
            <ToggleSwitch
              value={limit.autoApprove}
              onChange={readOnly ? null : v => onUpdate("autoApprove", v)}
              size="sm"
            />
            <span style={{ fontSize: 12, color: "#64748b" }}>
              {limit.autoApprove ? "Без модерации маркетолога" : "Требует ручного подтверждения"}
            </span>
          </div>

          <div style={{ fontSize: 12, fontWeight: 700, color: "#374151", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 6 }}>
            Уровень риска
          </div>
          <RiskSelector
            value={limit.riskLevel}
            onChange={readOnly ? null : v => onUpdate("riskLevel", v)}
          />
        </div>
      </div>
    </div>
  );
}

// ── Dual Slider — single track with two thumbs ────────────────────────────────
function DualSlider({ min, max, valueMin, valueMax, onChangeMin, onChangeMax, color, disabled }) {
  const pctMin = ((valueMin - min) / (max - min)) * 100;
  const pctMax = ((valueMax - min) / (max - min)) * 100;

  return (
    <div style={{ position: "relative", height: 30, padding: "12px 0" }}>
      {/* Track */}
      <div style={{
        position: "absolute", left: 0, right: 0, top: "50%", transform: "translateY(-50%)",
        height: 6, background: "#e2e8f0", borderRadius: 99,
      }} />
      {/* Active range */}
      <div style={{
        position: "absolute", top: "50%", transform: "translateY(-50%)",
        left: pctMin + "%", width: (pctMax - pctMin) + "%",
        height: 6, background: color, borderRadius: 99,
        opacity: disabled ? 0.4 : 1, transition: "opacity 0.15s",
      }} />
      {/* Min input */}
      <input
        type="range" min={min} max={max} step={0.5} value={valueMin}
        disabled={disabled}
        onChange={e => onChangeMin(Math.min(parseFloat(e.target.value), valueMax - 0.5))}
        style={{
          position: "absolute", left: 0, right: 0, top: 0, bottom: 0,
          width: "100%", height: "100%",
          appearance: "none", background: "none",
          pointerEvents: "none",
          accentColor: color,
        }}
        className="dual-slider-thumb"
      />
      {/* Max input */}
      <input
        type="range" min={min} max={max} step={0.5} value={valueMax}
        disabled={disabled}
        onChange={e => onChangeMax(Math.max(parseFloat(e.target.value), valueMin + 0.5))}
        style={{
          position: "absolute", left: 0, right: 0, top: 0, bottom: 0,
          width: "100%", height: "100%",
          appearance: "none", background: "none",
          pointerEvents: "none",
          accentColor: color,
        }}
        className="dual-slider-thumb"
      />
      <style>{`
        .dual-slider-thumb::-webkit-slider-thumb {
          appearance: none;
          width: 18px; height: 18px;
          border-radius: 50%;
          background: white;
          border: 3px solid ${color};
          cursor: ${disabled ? "not-allowed" : "pointer"};
          pointer-events: auto;
          box-shadow: 0 1px 3px rgba(13,25,41,0.2);
          transition: transform 0.1s;
        }
        .dual-slider-thumb::-webkit-slider-thumb:hover { transform: scale(1.15); }
        .dual-slider-thumb::-moz-range-thumb {
          width: 16px; height: 16px;
          border-radius: 50%;
          background: white;
          border: 3px solid ${color};
          cursor: ${disabled ? "not-allowed" : "pointer"};
          pointer-events: auto;
          box-shadow: 0 1px 3px rgba(13,25,41,0.2);
        }
      `}</style>
      {/* Scale ticks */}
      <div style={{ position: "absolute", left: 0, right: 0, bottom: -16, display: "flex", justifyContent: "space-between", fontSize: 10, color: "#94a3b8", fontFamily: "'JetBrains Mono', monospace" }}>
        <span>1%</span>
        <span>15%</span>
        <span>30%</span>
      </div>
    </div>
  );
}

// ── Toggle Switch ─────────────────────────────────────────────────────────────
function ToggleSwitch({ value, onChange, size = "md" }) {
  const dims = size === "sm" ? { w: 36, h: 20, thumb: 14, offset: 18 } : { w: 44, h: 24, thumb: 18, offset: 22 };
  const disabled = !onChange;

  return (
    <div
      onClick={() => onChange && onChange(!value)}
      style={{
        width: dims.w, height: dims.h, borderRadius: 99,
        background: value ? "oklch(0.55 0.18 230)" : "#cbd5e1",
        position: "relative", cursor: disabled ? "not-allowed" : "pointer",
        flexShrink: 0, transition: "background 0.2s",
        opacity: disabled ? 0.6 : 1,
      }}
    >
      <div style={{
        width: dims.thumb, height: dims.thumb, borderRadius: "50%",
        background: "white", position: "absolute",
        top: "50%", transform: "translateY(-50%)",
        left: value ? dims.offset : 3,
        transition: "left 0.2s",
        boxShadow: "0 1px 3px rgba(13,25,41,0.2)",
      }} />
    </div>
  );
}

// ── Numeric Input with stepper ────────────────────────────────────────────────
function NumericInput({ value, onChange, min = 0, step = 1, suffix, disabled }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 0,
      border: "1px solid #e2e8f0", borderRadius: 8,
      background: disabled ? "#f8fafc" : "white",
      overflow: "hidden",
    }}>
      <button
        onClick={() => !disabled && onChange(Math.max(min, value - step))}
        disabled={disabled}
        style={{
          padding: "8px 12px", background: "none", border: "none",
          cursor: disabled ? "not-allowed" : "pointer", color: "#64748b", fontSize: 14, fontWeight: 700,
        }}
      >−</button>
      <input
        type="text"
        value={value.toLocaleString("ru")}
        onChange={e => {
          const num = parseInt(e.target.value.replace(/\D/g, "")) || 0;
          if (!disabled) onChange(Math.max(min, num));
        }}
        disabled={disabled}
        style={{
          flex: 1, padding: "8px 4px", textAlign: "center",
          border: "none", outline: "none", fontFamily: "'JetBrains Mono', monospace",
          fontSize: 13, fontWeight: 700, color: "#0d1929",
          background: "transparent",
        }}
      />
      {suffix && <span style={{ paddingRight: 12, color: "#94a3b8", fontSize: 13 }}>{suffix}</span>}
      <button
        onClick={() => !disabled && onChange(value + step)}
        disabled={disabled}
        style={{
          padding: "8px 12px", background: "none", border: "none",
          cursor: disabled ? "not-allowed" : "pointer", color: "#64748b", fontSize: 14, fontWeight: 700,
        }}
      >+</button>
    </div>
  );
}

// ── Risk Badge ─────────────────────────────────────────────────────────────
function RiskBadge({ value }) {
  const opt = RISK_OPTIONS.find(o => o.value === value);
  if (!opt) return null;
  return (
    <div style={{
      padding: "5px 12px", borderRadius: 99,
      background: opt.color + "20", color: opt.color,
      fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.5px",
      display: "inline-flex", alignItems: "center", gap: 5,
    }}>
      <div style={{ width: 6, height: 6, borderRadius: "50%", background: opt.color }} />
      {opt.label}
    </div>
  );
}

// ── Risk Selector — 3 buttons ─────────────────────────────────────────────────
function RiskSelector({ value, onChange }) {
  const disabled = !onChange;
  return (
    <div style={{ display: "flex", gap: 0, background: "#f8fafc", borderRadius: 8, padding: 3, border: "1px solid #e2e8f0" }}>
      {RISK_OPTIONS.map(o => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            onClick={() => !disabled && onChange(o.value)}
            disabled={disabled}
            style={{
              flex: 1, padding: "5px 6px", borderRadius: 6,
              fontSize: 11, fontWeight: 700,
              background: active ? o.color : "transparent",
              color: active ? "white" : "#64748b",
              border: "none", cursor: disabled ? "not-allowed" : "pointer",
              fontFamily: "inherit", transition: "all 0.12s",
            }}
            title={o.desc}
          >{o.label.split(" ")[0]}</button>
        );
      })}
    </div>
  );
}

export default MlLimits;
