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



// ── Campaigns List ─────────────────────────────────────────────────────────────
function Campaigns({ currentUser, wizardVariant, campaigns, ops, segments, mccCategories }) {
  // Фаза 21: справочники из единого источника (live → API, иначе mock).
  // AppData здесь — страничный namespace-объект; проставляем до чтения ниже,
  // чтобы все подкомпоненты (визард, шаги) видели актуальный справочник.
  if (segments) AppData.SEGMENTS = segments;
  if (mccCategories) AppData.MCC_CATEGORIES = mccCategories;
  const { PERMISSIONS, SEGMENTS, MCC_CATEGORIES } = AppData;
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(null);
  const [showWizard, setShowWizard] = useState(false);
  const [editCampaign, setEditCampaign] = useState(null);
  const [toast, setToast] = useState(null);
  const perms = PERMISSIONS[currentUser.role];
  const isLive = !!ops?.isLive;

  const fmtRub = n => n >= 1000000 ? "₽" + (n/1000000).toFixed(1) + "М" : "₽" + (n/1000).toFixed(0) + "К";
  const fmt = n => n >= 1000000 ? (n/1000000).toFixed(1) + "М" : n >= 1000 ? (n/1000).toFixed(0) + "К" : n;

  const STATUS_FILTERS = [
    { id: "all", label: "Все" },
    { id: "active", label: "Активные" },
    { id: "paused", label: "Приостановленные" },
    { id: "draft", label: "Черновики" },
    { id: "completed", label: "Завершённые" },
  ];

  const filtered = campaigns.filter(c => {
    const matchStatus = filter === "all" || c.status === filter;
    const matchSearch = !search || c.name.toLowerCase().includes(search.toLowerCase());
    return matchStatus && matchSearch;
  });

  async function handleSaveCampaign(data) {
    const isEdit = !!editCampaign;
    const ok = await ops.save(data, isEdit);
    if (!ok) return; // ошибка уже показана toast-интерцептором API-клиента
    setToast({
      msg: isEdit ? "Кампания обновлена" : "Кампания создана и добавлена в список",
      type: "success",
    });
    setShowWizard(false);
    setEditCampaign(null);
  }

  async function handleStatusChange(id, newStatus) {
    const ok = await ops.changeStatus(id, newStatus);
    if (!ok) return;
    const labels = { active: "запущена", paused: "приостановлена", completed: "завершена" };
    setToast({ msg: `Кампания ${labels[newStatus] || "обновлена"}`, type: "success" });
    if (selected?.id === id) setSelected(s => ({ ...s, status: newStatus }));
  }

  async function handleDelete(id) {
    const ok = await ops.remove(id);
    if (!ok) return;
    setSelected(null);
    setToast({ msg: "Кампания удалена", type: "info" });
  }

  const segName = id => SEGMENTS.find(s => s.id === id)?.name || id;
  const mccName = code => MCC_CATEGORIES.find(m => m.code === code)?.name || code;

  return (
    <div style={{ display: "flex", gap: 20, height: "calc(100vh - 120px)" }}>

      {/* Left panel */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
        {/* Toolbar */}
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <div style={{ flex: 1, position: "relative" }}>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
              <circle cx="6" cy="6" r="4.5" stroke="#94a3b8" strokeWidth="1.5"/>
              <path d="M9.5 9.5l2.5 2.5" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Поиск кампаний…" style={{
              width: "100%", padding: "9px 12px 9px 32px", border: "1px solid #e2e8f0",
              borderRadius: 8, fontSize: 13, fontFamily: "inherit", outline: "none",
              background: "white", boxSizing: "border-box", color: "#0d1929",
            }} />
          </div>
          {perms.campaigns_create && (
            <Button variant="primary" onClick={() => { setEditCampaign(null); setShowWizard(true); }}>
              <span style={{ fontSize: 16 }}>+</span> Новая кампания
            </Button>
          )}
        </div>

        {/* Status tabs */}
        <div style={{ display: "flex", gap: 4, background: "white", borderRadius: 10, padding: 4, border: "1px solid #e2e8f0" }}>
          {STATUS_FILTERS.map(f => (
            <button key={f.id} onClick={() => setFilter(f.id)} style={{
              flex: 1, padding: "7px 4px", borderRadius: 7, fontSize: 12, fontWeight: 600,
              border: "none", cursor: "pointer", transition: "all 0.15s",
              background: filter === f.id ? "oklch(0.55 0.18 230)" : "transparent",
              color: filter === f.id ? "white" : "#64748b",
            }}>{f.label} <span style={{ opacity: 0.7 }}>({campaigns.filter(c => f.id === "all" || c.status === f.id).length})</span>
            </button>
          ))}
        </div>

        {/* Campaign cards */}
        <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 10 }}>
          {filtered.length === 0 && (
            <div style={{ textAlign: "center", color: "#94a3b8", padding: 48, fontSize: 14 }}>Кампании не найдены</div>
          )}
          {filtered.map(c => (
            <CampaignCard key={c.id} campaign={c} selected={selected?.id === c.id}
              onClick={() => setSelected(c)} fmtRub={fmtRub} fmt={fmt} />
          ))}
        </div>
      </div>

      {/* Right detail panel */}
      {selected ? (
        <CampaignDetail
          campaign={selected}
          onClose={() => setSelected(null)}
          onEdit={() => { setEditCampaign(selected); setShowWizard(true); }}
          onStatusChange={handleStatusChange}
          onDelete={handleDelete}
          perms={perms}
          segName={segName}
          mccName={mccName}
          fmtRub={fmtRub}
          fmt={fmt}
          isLive={isLive}
        />
      ) : (
        <div style={{
          width: 360, background: "white", borderRadius: 12, border: "1px solid #e2e8f0",
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "#94a3b8", fontSize: 14, flexDirection: "column", gap: 12,
        }}>
          <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
            <rect x="6" y="8" width="28" height="24" rx="4" stroke="#e2e8f0" strokeWidth="2"/>
            <path d="M12 16h16M12 22h10" stroke="#e2e8f0" strokeWidth="2" strokeLinecap="round"/>
          </svg>
          <span>Выберите кампанию</span>
        </div>
      )}

      {/* Wizard Modal */}
      {showWizard && (
        <CampaignWizard
          variant={wizardVariant || "steps"}
          initial={editCampaign}
          onSave={handleSaveCampaign}
          onClose={() => { setShowWizard(false); setEditCampaign(null); }}
        />
      )}

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}

// ── Campaign Card ─────────────────────────────────────────────────────────────
function CampaignCard({ campaign: c, selected, onClick, fmtRub, fmt }) {
  return (
    <div onClick={onClick} style={{
      background: "white", borderRadius: 10, padding: "16px 18px",
      border: selected ? "2px solid oklch(0.65 0.18 230)" : "1px solid #e8edf4",
      cursor: "pointer", transition: "all 0.15s",
      boxShadow: selected ? "0 0 0 3px oklch(0.85 0.08 230)" : "0 1px 3px rgba(0,0,0,0.04)",
    }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 14, color: "#0d1929", marginBottom: 4, textWrap: "balance" }}>{c.name}</div>
          <StatusBadge status={c.status} />
        </div>
        <div style={{ textAlign: "right", flexShrink: 0, marginLeft: 12 }}>
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 18, color: "oklch(0.45 0.18 160)" }}>{c.cashbackRate}%</div>
          <div style={{ fontSize: 11, color: "#94a3b8" }}>кэшбэк</div>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, fontSize: 12 }}>
        <div>
          <div style={{ color: "#94a3b8", marginBottom: 1 }}>Охват</div>
          <div style={{ fontWeight: 600, color: "#374151" }}>{fmt(c.reach)}</div>
        </div>
        <div>
          <div style={{ color: "#94a3b8", marginBottom: 1 }}>Расход</div>
          <div style={{ fontWeight: 600, color: "#374151" }}>{fmtRub(c.spent)}</div>
        </div>
        <div>
          <div style={{ color: "#94a3b8", marginBottom: 1 }}>ROI</div>
          <div style={{ fontWeight: 600, color: c.roi >= 3 ? "oklch(0.45 0.18 160)" : "#374151" }}>{c.roi > 0 ? c.roi.toFixed(1) + "×" : "—"}</div>
        </div>
      </div>
      {c.status === "active" && (
        <div style={{ marginTop: 10 }}>
          <ProgressBar value={c.spent} max={c.budget} color="oklch(0.65 0.18 230)" height={4} />
        </div>
      )}
    </div>
  );
}

// ── Campaign Detail Panel ─────────────────────────────────────────────────────
function CampaignDetail({ campaign: c, onClose, onEdit, onStatusChange, onDelete, perms, segName, mccName, fmtRub, fmt, isLive }) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  // В live-режиме бэкенд разрешает править поля только у черновиков (FSM);
  // удаление кампаний из БД не поддерживается ради аудита.
  const canEditFields = !isLive || c.status === "draft";
  const canDelete = perms.campaigns_delete && !isLive;

  const Row = ({ label, value }) => (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "10px 0", borderBottom: "1px solid #f8fafc" }}>
      <span style={{ fontSize: 13, color: "#8896a8" }}>{label}</span>
      <span style={{ fontSize: 13, fontWeight: 600, color: "#0d1929", textAlign: "right", maxWidth: "60%" }}>{value}</span>
    </div>
  );

  return (
    <div style={{ width: 360, display: "flex", flexDirection: "column", background: "white", borderRadius: 12, border: "1px solid #e2e8f0", overflow: "hidden", flexShrink: 0 }}>
      {/* Header */}
      <div style={{ padding: "18px 20px", borderBottom: "1px solid #f1f5f9", background: "linear-gradient(135deg, #f8fafc, #f0f7ff)" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", marginBottom: 8, textWrap: "balance" }}>{c.name}</div>
            <StatusBadge status={c.status} />
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "#94a3b8", fontSize: 18, padding: "0 0 0 8px" }}>×</button>
        </div>
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflowY: "auto", padding: "0 20px" }}>
        <Row label="Ставка кэшбэка" value={<span style={{ color: "oklch(0.45 0.18 160)", fontFamily: "'JetBrains Mono', monospace", fontSize: 16 }}>{c.cashbackRate}%</span>} />
        <Row label="Период" value={`${c.startDate} — ${c.endDate}`} />
        <Row label="Охват" value={fmt(c.reach) + " клиентов"} />
        <Row label="Бюджет" value={fmtRub(c.budget)} />
        <Row label="Израсходовано" value={<><span style={{ fontFamily: "'JetBrains Mono', monospace" }}>{fmtRub(c.spent)}</span> ({Math.round(c.spent/c.budget*100)}%)</>} />
        <Row label="Дневной лимит" value={fmtRub(c.dailyLimit)} />
        <Row label="Мин. сумма транзакции" value={`₽${c.minTxAmount.toLocaleString("ru")}`} />
        <Row label="CTR" value={c.ctr > 0 ? c.ctr + "%" : "—"} />
        <Row label="ROI" value={c.roi > 0 ? <span style={{ color: "oklch(0.45 0.18 160)", fontWeight: 700 }}>{c.roi.toFixed(1)}×</span> : "—"} />
        <Row label="Сегменты" value={c.segments.map(segName).join(", ")} />
        <Row label="Категории MCC" value={c.categories.map(mccName).join(", ")} />

        {/* Budget progress */}
        <div style={{ padding: "14px 0", borderBottom: "1px solid #f8fafc" }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8, fontSize: 12, color: "#64748b" }}>
            <span>Освоение бюджета</span>
            <span style={{ fontFamily: "'JetBrains Mono', monospace" }}>{Math.round(c.spent/c.budget*100)}%</span>
          </div>
          <ProgressBar value={c.spent} max={c.budget} color="oklch(0.65 0.18 230)" height={8} />
        </div>
      </div>

      {/* Actions */}
      {(perms.campaigns_edit || canDelete) && (
        <div style={{ padding: "14px 20px", borderTop: "1px solid #f1f5f9", display: "flex", flexDirection: "column", gap: 8 }}>
          {perms.campaigns_edit && (
            <div style={{ display: "flex", gap: 8 }}>
              <Button
                variant="primary" style={{ flex: 1 }}
                disabled={!canEditFields}
                title={canEditFields ? undefined : "В live-режиме редактируются только черновики — активные кампании неизменяемы для аудита"}
                onClick={() => { if (canEditFields) onEdit(); }}
              >Редактировать</Button>
              {c.status === "active" && <Button variant="secondary" onClick={() => onStatusChange(c.id, "paused")}>⏸</Button>}
              {c.status === "paused" && <Button variant="success" onClick={() => onStatusChange(c.id, "active")}>▶</Button>}
              {c.status === "draft" && <Button variant="success" style={{ flex: 1 }} onClick={() => onStatusChange(c.id, "active")}>Запустить</Button>}
            </div>
          )}
          {canDelete && (
            confirmDelete ? (
              <div style={{ display: "flex", gap: 8 }}>
                <Button variant="danger" style={{ flex: 1 }} onClick={() => onDelete(c.id)}>Подтвердить удаление</Button>
                <Button variant="ghost" onClick={() => setConfirmDelete(false)}>Отмена</Button>
              </div>
            ) : (
              <Button variant="ghost" onClick={() => setConfirmDelete(true)} style={{ color: "#ef4444", width: "100%" }}>Удалить кампанию</Button>
            )
          )}
        </div>
      )}
    </div>
  );
}

// ── Campaign Wizard ─────────────────────────────────────────────────────────────
const WIZARD_STEPS = [
  { id: "basics",     label: "Основные параметры",    short: "Основные" },
  { id: "audience",   label: "Целевая аудитория",     short: "Аудитория" },
  { id: "categories", label: "Категории кэшбэка",     short: "Категории" },
  { id: "budget",     label: "Бюджетирование",        short: "Бюджет" },
  { id: "review",     label: "Ревью и запуск",        short: "Ревью" },
];

const EMPTY_FORM = {
  name: "", startDate: "", endDate: "", cashbackRate: 5,
  segments: [], rfmMin: 1, rfmMax: 5,
  categories: [], minTxAmounts: {},
  budget: 1000000, dailyLimit: 50000, autoPause: true,
  status: "draft",
};

function CampaignWizard({ variant, initial, onSave, onClose }) {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState(initial ? {
    ...EMPTY_FORM, ...initial,
    minTxAmounts: initial.categories.reduce((acc, c) => ({ ...acc, [c]: initial.minTxAmount }), {}),
  } : EMPTY_FORM);
  const [jumpTo, setJumpTo] = useState(null);
  const { SEGMENTS, MCC_CATEGORIES } = AppData;

  const setField = (key, val) => setForm(f => ({ ...f, [key]: val }));

  const totalReach = SEGMENTS
    .filter(s => form.segments.includes(s.id))
    .reduce((sum, s) => sum + s.count, 0);

  const canNext = [
    form.name && form.startDate && form.endDate && form.cashbackRate > 0,
    form.segments.length > 0,
    form.categories.length > 0,
    form.budget > 0 && form.dailyLimit > 0,
    true,
  ][step];

  function handleSave() {
    const minTx = Object.values(form.minTxAmounts)[0] || 500;
    onSave({
      ...(initial || {}),
      ...form,
      reach: totalReach,
      minTxAmount: minTx,
    });
  }

  const isVertical = variant === "vertical";

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)",
      display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000,
    }}>
      <div style={{
        background: "white", borderRadius: 16,
        width: isVertical ? 720 : 820,
        maxHeight: "90vh", display: "flex", flexDirection: "column",
        boxShadow: "0 24px 64px rgba(0,0,0,0.2)", overflow: "hidden",
      }}>
        {/* Wizard Header */}
        <div style={{
          background: "linear-gradient(135deg, #0d1929, #0f2040)",
          padding: "20px 28px", display: "flex", alignItems: "center", gap: 16,
        }}>
          <div style={{ flex: 1 }}>
            <div style={{ color: "white", fontSize: 17, fontWeight: 700 }}>
              {initial ? "Редактировать кампанию" : "Новая кампания"}
            </div>
            <div style={{ color: "rgba(255,255,255,0.5)", fontSize: 12, marginTop: 2 }}>
              Шаг {step + 1} из {WIZARD_STEPS.length} — {WIZARD_STEPS[step].label}
            </div>
          </div>
          <button onClick={onClose} style={{ background: "rgba(255,255,255,0.1)", border: "none", color: "white", borderRadius: 8, width: 32, height: 32, cursor: "pointer", fontSize: 18 }}>×</button>
        </div>

        {/* Step Indicators */}
        <div style={{
          display: "flex", background: "#f8fafc", borderBottom: "1px solid #e8edf4",
          padding: "0 20px",
        }}>
          {WIZARD_STEPS.map((s, i) => {
            const done = i < step;
            const active = i === step;
            return (
              <button key={s.id} onClick={() => { if (done) setStep(i); }} style={{
                flex: 1, padding: "13px 8px", display: "flex", flexDirection: "column", alignItems: "center", gap: 4,
                background: "none", border: "none",
                borderBottom: active ? "2px solid oklch(0.55 0.18 230)" : "2px solid transparent",
                cursor: done ? "pointer" : "default",
                opacity: !done && !active ? 0.5 : 1,
                transition: "all 0.15s",
              }}>
                <div style={{
                  width: 24, height: 24, borderRadius: "50%", fontSize: 11, fontWeight: 700,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  background: done ? "oklch(0.65 0.18 160)" : active ? "oklch(0.55 0.18 230)" : "#e2e8f0",
                  color: done || active ? "white" : "#94a3b8",
                }}>{done ? "✓" : i + 1}</div>
                <span style={{ fontSize: 11, fontWeight: 600, color: active ? "oklch(0.45 0.18 230)" : done ? "#16a34a" : "#94a3b8" }}>{s.short}</span>
              </button>
            );
          })}
        </div>

        {/* Step Content */}
        <div style={{ flex: 1, overflowY: "auto", padding: "28px 32px" }}>
          {step === 0 && <StepBasics form={form} setField={setField} />}
          {step === 1 && <StepAudience form={form} setField={setField} segments={SEGMENTS} totalReach={totalReach} />}
          {step === 2 && <StepCategories form={form} setField={setField} categories={MCC_CATEGORIES} />}
          {step === 3 && <StepBudget form={form} setField={setField} />}
          {step === 4 && <StepReview form={form} segments={SEGMENTS} categories={MCC_CATEGORIES} totalReach={totalReach} onJump={i => setStep(i)} />}
        </div>

        {/* Footer Nav */}
        <div style={{ padding: "16px 32px", borderTop: "1px solid #f1f5f9", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <Button variant="secondary" onClick={() => step === 0 ? onClose() : setStep(s => s - 1)}>
            {step === 0 ? "Отмена" : "← Назад"}
          </Button>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {form.status === "draft" && step === 4 && (
              <Button variant="secondary" onClick={() => handleSave()}>Сохранить черновик</Button>
            )}
            {step < 4 ? (
              <Button variant="primary" disabled={!canNext} onClick={() => setStep(s => s + 1)}>
                Далее →
              </Button>
            ) : (
              <Button variant="success" onClick={() => { setField("status", "active"); setTimeout(handleSave, 0); }}>
                🚀 Запустить кампанию
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Step 1: Basics ─────────────────────────────────────────────────────────────
function StepBasics({ form, setField }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <Input label="Название кампании" value={form.name} onChange={v => setField("name", v)} placeholder="Например: Летний кэшбэк — Супермаркеты" required />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <Input label="Дата начала" type="date" value={form.startDate} onChange={v => setField("startDate", v)} required />
        <Input label="Дата окончания" type="date" value={form.endDate} onChange={v => setField("endDate", v)} required />
      </div>
      <div>
        <label style={{ fontSize: 12, fontWeight: 600, color: "#374151", display: "block", marginBottom: 8 }}>
          Ставка кэшбэка: <span style={{ color: "oklch(0.45 0.18 160)", fontFamily: "'JetBrains Mono', monospace", fontSize: 16 }}>{form.cashbackRate}%</span>
        </label>
        <input type="range" min={1} max={30} step={0.5} value={form.cashbackRate}
          onChange={e => setField("cashbackRate", parseFloat(e.target.value))}
          style={{ width: "100%", accentColor: "oklch(0.55 0.18 230)", height: 4 }} />
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#94a3b8", marginTop: 4 }}>
          <span>1%</span><span>15%</span><span>30%</span>
        </div>
      </div>
      <div style={{ background: "#f0f7ff", borderRadius: 10, padding: "14px 16px", fontSize: 13, color: "#374151" }}>
        <strong>Рекомендация:</strong> Средняя ставка по рынку — 5–7%. Ставка выше 15% существенно увеличивает стоимость программы.
      </div>
    </div>
  );
}

// ── Step 2: Audience ───────────────────────────────────────────────────────────
function StepAudience({ form, setField, segments, totalReach }) {
  function toggleSegment(id) {
    const cur = form.segments;
    setField("segments", cur.includes(id) ? cur.filter(s => s !== id) : [...cur, id]);
  }
  const fmt = n => n >= 1000000 ? (n/1000000).toFixed(1) + "М" : n >= 1000 ? (n/1000).toFixed(0) + "К" : n;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#374151", marginBottom: 12 }}>Выберите сегменты аудитории</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {segments.map(seg => {
            const sel = form.segments.includes(seg.id);
            return (
              <div key={seg.id} onClick={() => toggleSegment(seg.id)} style={{
                display: "flex", alignItems: "center", gap: 12,
                padding: "12px 16px", borderRadius: 10,
                border: sel ? "2px solid oklch(0.65 0.18 230)" : "1px solid #e2e8f0",
                background: sel ? "oklch(0.97 0.03 230)" : "white",
                cursor: "pointer", transition: "all 0.15s",
              }}>
                <div style={{
                  width: 20, height: 20, borderRadius: 4, flexShrink: 0,
                  background: sel ? "oklch(0.55 0.18 230)" : "white",
                  border: sel ? "none" : "2px solid #cbd5e1",
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}>
                  {sel && <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2 6l3 3 5-5" stroke="white" strokeWidth="2" strokeLinecap="round"/></svg>}
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 700, fontSize: 14, color: "#0d1929" }}>{seg.name}</div>
                  <div style={{ fontSize: 12, color: "#8896a8" }}>Клиентов: {fmt(seg.count)}</div>
                </div>
                <div style={{
                  background: sel ? "oklch(0.65 0.18 230)" : "#f1f5f9",
                  color: sel ? "white" : "#94a3b8",
                  borderRadius: 20, padding: "3px 10px", fontSize: 12, fontWeight: 600,
                }}>{fmt(seg.count)}</div>
              </div>
            );
          })}
        </div>
      </div>

      {/* RFM Filter */}
      <div style={{ background: "#f8fafc", borderRadius: 10, padding: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#374151", marginBottom: 12 }}>RFM-фильтр (квантили)</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <div>
            <label style={{ fontSize: 12, color: "#64748b", display: "block", marginBottom: 4 }}>Квантиль от: {form.rfmMin}</label>
            <input type="range" min={1} max={5} step={1} value={form.rfmMin}
              onChange={e => setField("rfmMin", parseInt(e.target.value))}
              style={{ width: "100%", accentColor: "oklch(0.55 0.18 230)" }} />
          </div>
          <div>
            <label style={{ fontSize: 12, color: "#64748b", display: "block", marginBottom: 4 }}>Квантиль до: {form.rfmMax}</label>
            <input type="range" min={1} max={5} step={1} value={form.rfmMax}
              onChange={e => setField("rfmMax", parseInt(e.target.value))}
              style={{ width: "100%", accentColor: "oklch(0.55 0.18 230)" }} />
          </div>
        </div>
      </div>

      {/* Reach preview */}
      <div style={{
        background: totalReach > 0 ? "linear-gradient(135deg, oklch(0.97 0.04 230), oklch(0.97 0.04 160))" : "#f8fafc",
        borderRadius: 12, padding: "16px 20px",
        border: "1px solid " + (totalReach > 0 ? "oklch(0.85 0.08 230)" : "#e2e8f0"),
        transition: "all 0.3s",
      }}>
        <div style={{ fontSize: 12, color: "#64748b", marginBottom: 4 }}>Предварительный охват аудитории</div>
        <div style={{ fontSize: 32, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: "oklch(0.45 0.18 230)" }}>
          {fmt(totalReach)}
        </div>
        <div style={{ fontSize: 12, color: "#8896a8", marginTop: 4 }}>
          {form.segments.length === 0 ? "Выберите хотя бы один сегмент" : `клиентов в ${form.segments.length} сегмент(ах)`}
        </div>
      </div>
    </div>
  );
}

// ── Step 3: Categories ─────────────────────────────────────────────────────────
function StepCategories({ form, setField, categories }) {
  const [search, setSearch] = useState("");
  function toggleCat(code) {
    const cur = form.categories;
    if (cur.includes(code)) {
      setField("categories", cur.filter(c => c !== code));
      const { [code]: _, ...rest } = form.minTxAmounts;
      setField("minTxAmounts", rest);
    } else {
      setField("categories", [...cur, code]);
      setField("minTxAmounts", { ...form.minTxAmounts, [code]: 500 });
    }
  }
  const filtered = categories.filter(c =>
    !search || c.name.toLowerCase().includes(search.toLowerCase()) || c.code.includes(search)
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Поиск MCC-кодов…" style={{
        padding: "9px 12px", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 13,
        fontFamily: "inherit", outline: "none", color: "#0d1929",
      }} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
        {filtered.map(cat => {
          const sel = form.categories.includes(cat.code);
          return (
            <div key={cat.code} onClick={() => toggleCat(cat.code)} style={{
              padding: "12px", borderRadius: 10,
              border: sel ? "2px solid oklch(0.65 0.18 230)" : "1px solid #e2e8f0",
              background: sel ? "oklch(0.97 0.03 230)" : "white",
              cursor: "pointer", transition: "all 0.15s",
              display: "flex", flexDirection: "column", alignItems: "center", gap: 6,
            }}>
              <span style={{ fontSize: 22 }}>{cat.icon}</span>
              <span style={{ fontSize: 12, fontWeight: 600, color: "#374151", textAlign: "center" }}>{cat.name}</span>
              <span style={{ fontSize: 10, color: "#94a3b8", fontFamily: "'JetBrains Mono', monospace" }}>MCC {cat.code}</span>
            </div>
          );
        })}
      </div>
      {form.categories.length > 0 && (
        <div style={{ background: "#f8fafc", borderRadius: 10, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#374151", marginBottom: 12 }}>Мин. сумма транзакции по категориям</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {form.categories.map(code => {
              const cat = categories.find(c => c.code === code);
              return (
                <div key={code} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <span style={{ fontSize: 16 }}>{cat?.icon}</span>
                  <span style={{ fontSize: 13, flex: 1, color: "#374151" }}>{cat?.name}</span>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{ fontSize: 13, color: "#94a3b8" }}>₽</span>
                    <input type="number" value={form.minTxAmounts[code] || 500} min={0}
                      onChange={e => setField("minTxAmounts", { ...form.minTxAmounts, [code]: parseInt(e.target.value) })}
                      style={{ width: 80, padding: "6px 8px", border: "1px solid #e2e8f0", borderRadius: 6, fontSize: 13, fontFamily: "inherit", textAlign: "right" }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Step 4: Budget ────────────────────────────────────────────────────────────
function StepBudget({ form, setField }) {
  const daysLeft = form.startDate && form.endDate
    ? Math.max(0, Math.ceil((new Date(form.endDate) - new Date(form.startDate)) / 86400000))
    : 0;
  const projectedSpend = form.dailyLimit * daysLeft;
  const fmtRub = n => "₽" + n.toLocaleString("ru");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div>
        <label style={{ fontSize: 12, fontWeight: 600, color: "#374151", display: "block", marginBottom: 8 }}>
          Общий бюджет кампании
        </label>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <input type="number" value={form.budget} min={10000} step={10000}
            onChange={e => setField("budget", parseInt(e.target.value))}
            style={{ flex: 1, padding: "10px 12px", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 16, fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: "#0d1929", outline: "none" }} />
          <span style={{ fontSize: 13, color: "#64748b" }}>₽</span>
        </div>
        <input type="range" min={100000} max={10000000} step={100000} value={form.budget}
          onChange={e => setField("budget", parseInt(e.target.value))}
          style={{ width: "100%", marginTop: 8, accentColor: "oklch(0.55 0.18 230)" }} />
      </div>
      <div>
        <label style={{ fontSize: 12, fontWeight: 600, color: "#374151", display: "block", marginBottom: 8 }}>Дневной лимит</label>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <input type="number" value={form.dailyLimit} min={1000} step={1000}
            onChange={e => setField("dailyLimit", parseInt(e.target.value))}
            style={{ flex: 1, padding: "10px 12px", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 16, fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: "#0d1929", outline: "none" }} />
          <span style={{ fontSize: 13, color: "#64748b" }}>₽ / день</span>
        </div>
        <input type="range" min={10000} max={500000} step={10000} value={form.dailyLimit}
          onChange={e => setField("dailyLimit", parseInt(e.target.value))}
          style={{ width: "100%", marginTop: 8, accentColor: "oklch(0.55 0.18 230)" }} />
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "12px 16px", background: "#f8fafc", borderRadius: 10 }}>
        <div onClick={() => setField("autoPause", !form.autoPause)} style={{
          width: 40, height: 22, borderRadius: 11,
          background: form.autoPause ? "oklch(0.55 0.18 230)" : "#e2e8f0",
          position: "relative", cursor: "pointer", transition: "all 0.2s", flexShrink: 0,
        }}>
          <div style={{
            width: 18, height: 18, borderRadius: "50%", background: "white",
            position: "absolute", top: 2,
            left: form.autoPause ? 20 : 2,
            transition: "left 0.2s",
            boxShadow: "0 1px 4px rgba(0,0,0,0.2)",
          }} />
        </div>
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#374151" }}>Авто-приостановка при исчерпании бюджета</div>
          <div style={{ fontSize: 12, color: "#8896a8" }}>Кампания автоматически остановится при достижении лимита</div>
        </div>
      </div>
      {daysLeft > 0 && (
        <div style={{ background: projectedSpend > form.budget ? "#fef2f2" : "#f0fdf4", borderRadius: 10, padding: "14px 16px", border: `1px solid ${projectedSpend > form.budget ? "#fca5a5" : "#86efac"}` }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: projectedSpend > form.budget ? "#dc2626" : "#16a34a", marginBottom: 4 }}>
            {projectedSpend > form.budget ? "⚠ Дневной лимит превышает бюджет" : "✓ Параметры бюджетирования корректны"}
          </div>
          <div style={{ fontSize: 12, color: "#64748b" }}>
            Длительность: {daysLeft} дн. · Прогноз расхода: {fmtRub(Math.min(projectedSpend, form.budget))} из {fmtRub(form.budget)}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Step 5: Review ─────────────────────────────────────────────────────────────
function StepReview({ form, segments, categories, totalReach, onJump }) {
  const fmt = n => n >= 1000000 ? (n/1000000).toFixed(1) + "М" : n >= 1000 ? (n/1000).toFixed(0) + "К" : n;
  const fmtRub = n => "₽" + n.toLocaleString("ru");
  const segName = id => segments.find(s => s.id === id)?.name || id;
  const catName = code => categories.find(c => c.code === code)?.name || code;

  const Section = ({ title, stepIdx, children }) => (
    <div style={{ background: "#f8fafc", borderRadius: 12, padding: "16px 20px", marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#374151" }}>{title}</div>
        <button onClick={() => onJump(stepIdx)} style={{ fontSize: 12, color: "oklch(0.55 0.18 230)", background: "none", border: "none", cursor: "pointer", fontWeight: 600 }}>Изменить</button>
      </div>
      {children}
    </div>
  );

  const Row = ({ label, value }) => (
    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
      <span style={{ fontSize: 13, color: "#8896a8" }}>{label}</span>
      <span style={{ fontSize: 13, fontWeight: 600, color: "#0d1929" }}>{value}</span>
    </div>
  );

  return (
    <div>
      <Section title="Основные параметры" stepIdx={0}>
        <Row label="Название" value={form.name || "—"} />
        <Row label="Период" value={form.startDate && form.endDate ? `${form.startDate} — ${form.endDate}` : "—"} />
        <Row label="Ставка кэшбэка" value={<span style={{ color: "oklch(0.45 0.18 160)", fontFamily: "'JetBrains Mono', monospace", fontWeight: 700 }}>{form.cashbackRate}%</span>} />
      </Section>
      <Section title="Аудитория" stepIdx={1}>
        <Row label="Сегменты" value={form.segments.length > 0 ? form.segments.map(segName).join(", ") : "—"} />
        <Row label="RFM-квантили" value={`${form.rfmMin} — ${form.rfmMax}`} />
        <Row label="Охват" value={<span style={{ color: "oklch(0.45 0.18 230)", fontFamily: "'JetBrains Mono', monospace", fontWeight: 700 }}>{fmt(totalReach)} клиентов</span>} />
      </Section>
      <Section title="Категории MCC" stepIdx={2}>
        <Row label="Категории" value={form.categories.length > 0 ? form.categories.map(catName).join(", ") : "—"} />
        <Row label="Мин. транзакция" value={form.categories.length > 0 ? `от ₽${Math.min(...Object.values(form.minTxAmounts || {500:500})).toLocaleString("ru")}` : "—"} />
      </Section>
      <Section title="Бюджет" stepIdx={3}>
        <Row label="Общий бюджет" value={fmtRub(form.budget)} />
        <Row label="Дневной лимит" value={fmtRub(form.dailyLimit) + " / день"} />
        <Row label="Авто-приостановка" value={form.autoPause ? "Включена" : "Выключена"} />
      </Section>
      <div style={{ background: "linear-gradient(135deg, oklch(0.97 0.04 230), oklch(0.97 0.04 160))", borderRadius: 12, padding: "16px 20px", border: "1px solid oklch(0.85 0.08 230)" }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#0d1929", marginBottom: 4 }}>🚀 Готово к запуску</div>
        <div style={{ fontSize: 13, color: "#64748b" }}>После нажатия «Запустить» кампания станет активной и предложения начнут рассылаться клиентам.</div>
      </div>
    </div>
  );
}

export default Campaigns;
