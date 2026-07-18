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
import { useLiveKpis, useLiveMatrix, useLiveTrend } from "@/shared/api/live";

const AppData = {
  USERS, ROLE_LABELS, PERMISSIONS, MCC_CATEGORIES, SEGMENTS,
  CAMPAIGNS, generateTrendData, MCC_HEATMAP, FUNNEL_DATA,
  MATRIX_DATA, CHANNEL_DATA, computeActivityMatrix,
};

// ── Seed-based random (deterministic per seed) ─────────────────────────────
function seededRng(seed) {
  let s = seed;
  return () => { s = (s * 16807 + 0) % 2147483647; return (s - 1) / 2147483646; };
}

// ── Generate trend data reactive to period + campaign filter ───────────────
function buildTrendData(days, campaignMultiplier) {
  const rng = seededRng(42 + days);
  const data = [];
  const base = new Date("2026-04-01");
  base.setDate(base.getDate() + (30 - days));
  for (let i = 0; i < days; i++) {
    const d = new Date(base);
    d.setDate(d.getDate() + i);
    const label = `${d.getDate().toString().padStart(2,"0")}.${(d.getMonth()+1).toString().padStart(2,"0")}`;
    const trend = 1 + i * 0.015;
    data.push({
      date: label,
      premium: Math.round((800  + rng()*400)  * trend * campaignMultiplier),
      mass:    Math.round((3200 + rng()*1200) * trend * campaignMultiplier),
      young:   Math.round((1500 + rng()*600)  * trend * campaignMultiplier),
    });
  }
  return data;
}

// ── Build heatmap reactive to campaign filter + period ──────────────────────
function buildHeatmap(campaignIds, allCampaigns, period = "30d") {
  const BASE_HEATMAP = AppData.MCC_HEATMAP;

  // Period maturity multiplier — shorter window = less mature response %
  const periodMult = { "7d": 0.62, "30d": 1.00, "90d": 1.12 }[period] || 1;
  const periodRng = seededRng(period === "7d" ? 7 : period === "90d" ? 91 : 31);

  function applyPeriod(row) {
    const segs = ["premium","mass","young","senior","business"];
    const newRow = { category: row.category };
    segs.forEach(s => {
      const variation = 0.94 + periodRng() * 0.12; // small per-cell variation
      newRow[s] = Math.max(0, Math.min(99, Math.round(row[s] * periodMult * variation)));
    });
    return newRow;
  }

  if (campaignIds === "all") return BASE_HEATMAP.map(applyPeriod);

  const selectedCats = allCampaigns
    .filter(c => campaignIds.includes(String(c.id)))
    .flatMap(c => c.categories);

  const catNameMap = {
    "5411": "Супермаркеты", "5912": "Аптеки", "5541": "АЗС",
    "5812": "Рестораны", "5045": "Электроника", "5600": "Одежда",
    "4111": "Транспорт", "5999": "Прочая розница",
  };
  const boostedNames = new Set(selectedCats.map(c => catNameMap[c]).filter(Boolean));
  const segs = ["premium","mass","young","senior","business"];

  return BASE_HEATMAP.map(row => {
    const catBoost = boostedNames.has(row.category) ? 1.15 : 0.80;
    const newRow = { category: row.category };
    segs.forEach(s => {
      const variation = 0.94 + periodRng() * 0.12;
      newRow[s] = Math.max(0, Math.min(99, Math.round(row[s] * catBoost * periodMult * variation)));
    });
    return newRow;
  });
}

// ── Compute KPIs from campaign + period ────────────────────────────────────
function computeKPIs(campaigns, campaignFilter, period) {
  const periodMultipliers = { "7d": 0.23, "30d": 1, "90d": 2.8 };
  const m = periodMultipliers[period] || 1;

  const filtered = campaignFilter === "all"
    ? campaigns.filter(c => c.status === "active" || c.status === "paused")
    : campaigns.filter(c => campaignFilter.includes(String(c.id)));

  if (!filtered.length) return { reach: 0, spent: 0, budget: 0, ctr: 0, count: 0 };

  const reach   = Math.round(filtered.reduce((s,c) => s + c.reach, 0) * Math.min(m, 1));
  const spent   = Math.round(filtered.reduce((s,c) => s + c.spent, 0) * Math.min(m, 1));
  const budget  = filtered.reduce((s,c) => s + c.budget, 0);
  const ctr     = filtered.reduce((s,c) => s + c.ctr, 0) / filtered.length;
  return { reach, spent, budget, ctr: isNaN(ctr) ? 0 : ctr, count: filtered.length };
}

function Dashboard({ onNavigate, currentUser, campaigns: CAMPAIGNS, isLive }) {
  const { PERMISSIONS } = AppData;
  const perms = PERMISSIONS[currentUser.role];

  const [period, setPeriod] = useState("30d");
  const [campaignFilter, setCampaignFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("active");

  const periodDays = { "7d": 7, "30d": 30, "90d": 90 };

  // Live-матрица отклика из /analytics/segment-matrix (только когда API онлайн)
  const liveMatrixQ = useLiveMatrix(periodDays[period] || 30, !!isLive);
  // Live-динамика принятых из /analytics/daily-trend (фаза 16)
  const liveTrendQ = useLiveTrend(
    campaignFilter !== "all" ? String(campaignFilter) : null,
    periodDays[period] || 30,
    !!isLive,
  );

  // Campaigns available for filter
  const filterableCampaigns = CAMPAIGNS.filter(c => c.status !== "draft");
  const displayedCampaigns = useMemo(() => {
    if (campaignFilter !== "all") return CAMPAIGNS.filter(c => campaignFilter.includes(String(c.id)));
    if (statusFilter === "all") return CAMPAIGNS;
    return CAMPAIGNS.filter(c => c.status === statusFilter);
  }, [CAMPAIGNS, campaignFilter, statusFilter]);

  // Reactive KPIs: live — из /analytics/kpis (единый источник с аналитикой),
  // иначе клиентский расчёт по mock-кампаниям (фаза 24).
  const liveKpisQ = useLiveKpis(
    campaignFilter !== "all" ? String(campaignFilter) : null,
    periodDays[period] || 30, null, !!isLive,
  );
  const liveKpis = isLive ? liveKpisQ.data : null;
  const kpis = useMemo(() =>
    liveKpis ?? computeKPIs(CAMPAIGNS, campaignFilter === "all" ? "all" : [campaignFilter], period),
    [CAMPAIGNS, campaignFilter, period, liveKpis]
  );

  // Reactive trend data: live-ряды из API, иначе — модельная динамика
  const liveTrend = isLive ? liveTrendQ.data : null;
  const trendData = useMemo(() => {
    if (liveTrend?.length) return liveTrend;
    const days = periodDays[period] || 30;
    const selectedCampaigns = campaignFilter === "all"
      ? CAMPAIGNS.filter(c => c.status === "active")
      : CAMPAIGNS.filter(c => String(c.id) === campaignFilter);
    // New campaign with no data → flat zero line
    const hasData = selectedCampaigns.length === 0 || selectedCampaigns.every(c => c.spent === 0 && c.ctr === 0);
    if (hasData) return buildTrendData(days, 0);
    const reachMultiplier = selectedCampaigns.reduce((s,c) => s + c.reach, 0) / 1016500;
    return buildTrendData(days, Math.max(0.1, reachMultiplier));
  }, [CAMPAIGNS, period, campaignFilter, liveTrend]);

  // Reactive heatmap: live-строки из API, иначе mock-матрица
  const heatmapData = useMemo(() => {
    if (isLive && liveMatrixQ.data?.rows?.length) return liveMatrixQ.data.rows;
    const selectedCampaign = campaignFilter === "all"
      ? null
      : CAMPAIGNS.find(c => String(c.id) === campaignFilter);
    return AppData.computeActivityMatrix(selectedCampaign, period).rows;
  }, [CAMPAIGNS, campaignFilter, period, isLive, liveMatrixQ.data]);

  const fmt = n => n >= 1000000 ? (n/1000000).toFixed(1) + "М" : n >= 1000 ? (n/1000).toFixed(0) + "К" : String(n);
  const fmtRub = n => n >= 1000000 ? "₽"+(n/1000000).toFixed(1)+"М" : "₽"+(n/1000).toFixed(0)+"К";

  // Trend direction changes with period
  const trendReach = period === "7d" ? 4.2  : period === "30d" ? 12.4  : 28.6;
  const trendSpent = period === "7d" ? -1.1 : period === "30d" ? -3.1  : -8.4;
  const trendCTR   = period === "7d" ? 2.1  : period === "30d" ? 5.8   : 14.2;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24, maxWidth: 1400 }}>

      {/* Filter Bar */}
      <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap", background: "white", borderRadius: 12, padding: "14px 20px", border: "1px solid #e8edf4" }}>
        {/* Campaign select */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>Кампания</label>
          <select value={campaignFilter} onChange={e => { setCampaignFilter(e.target.value); }}
            style={{ padding: "8px 12px", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 13, fontFamily: "inherit", background: "white", color: "#0d1929", outline: "none", minWidth: 200 }}>
            <option value="all">Все активные</option>
            {filterableCampaigns.map(c => <option key={c.id} value={String(c.id)}>{c.name}</option>)}
          </select>
        </div>

        {/* Status filter (only when "all" campaigns) */}
        {campaignFilter === "all" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>Статус</label>
            <div style={{ display: "flex", background: "#f8fafc", borderRadius: 8, padding: 3, border: "1px solid #e2e8f0" }}>
              {[{ v: "active", l: "Активные" }, { v: "paused", l: "Паузa" }, { v: "all", l: "Все" }].map(f => (
                <button key={f.v} onClick={() => setStatusFilter(f.v)} style={{
                  padding: "6px 14px", borderRadius: 6, fontSize: 12, fontWeight: 600,
                  border: "none", cursor: "pointer", transition: "all 0.15s",
                  background: statusFilter === f.v ? "oklch(0.55 0.18 230)" : "transparent",
                  color: statusFilter === f.v ? "white" : "#64748b",
                }}>{f.l}</button>
              ))}
            </div>
          </div>
        )}

        {/* Period */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>Период</label>
          <div style={{ display: "flex", background: "#f8fafc", borderRadius: 8, padding: 3, border: "1px solid #e2e8f0" }}>
            {[{ v: "7d", l: "7 дн." }, { v: "30d", l: "30 дн." }, { v: "90d", l: "90 дн." }].map(p => (
              <button key={p.v} onClick={() => setPeriod(p.v)} style={{
                padding: "6px 14px", borderRadius: 6, fontSize: 12, fontWeight: 600,
                border: "none", cursor: "pointer", transition: "all 0.15s",
                background: period === p.v ? "oklch(0.55 0.18 230)" : "transparent",
                color: period === p.v ? "white" : "#64748b",
              }}>{p.l}</button>
            ))}
          </div>
        </div>

        <div style={{ marginLeft: "auto", fontSize: 12, color: "#94a3b8" }}>
          Обновлено: {new Date().toLocaleDateString("ru")} {new Date().toLocaleTimeString("ru", { hour: "2-digit", minute: "2-digit" })}
        </div>
      </div>

      {/* KPI Row — reactive. Фиктивные %-тренды скрываем в live-режиме. */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
        <StatCard
          label="Кампаний в выборке"
          value={displayedCampaigns.length}
          sub={`из ${CAMPAIGNS.length} всего`}
          color="oklch(0.65 0.18 230)" icon="📢"
        />
        <StatCard
          label="Охват аудитории"
          value={fmt(kpis.reach)}
          sub="уникальных клиентов"
          trend={isLive ? liveKpis?.trends.reach : trendReach}
          color="oklch(0.65 0.18 200)" icon="👥"
        />
        <StatCard
          label="Израсходовано"
          value={fmtRub(kpis.spent)}
          sub={kpis.budget > 0 ? `из ${fmtRub(kpis.budget)} бюджета` : "бюджет не задан"}
          trend={isLive ? liveKpis?.trends.spent : trendSpent}
          color="oklch(0.65 0.18 30)" icon="💸"
        />
        <StatCard
          label="Средний CTR"
          value={kpis.ctr.toFixed(1) + "%"}
          sub="по выбранным кампаниям"
          trend={isLive ? liveKpis?.trends.ctr : trendCTR}
          color="oklch(0.65 0.18 160)" icon="🎯"
        />
      </div>

      {/* Trend Chart + Heatmap — reactive */}
      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16 }}>
        <TrendChart data={trendData} period={period} campaignFilter={campaignFilter} campaigns={CAMPAIGNS} isLive={isLive} isLiveData={!!liveTrend?.length} />
        <MccHeatmap data={heatmapData} campaignFilter={campaignFilter} campaigns={CAMPAIGNS} isLive={isLive} />
      </div>

      {/* Top Campaigns — reactive to status filter */}
      <TopCampaigns
        campaigns={displayedCampaigns}
        onNavigate={onNavigate}
        canCreate={perms.campaigns_create}
        statusFilter={statusFilter}
        campaignFilter={campaignFilter}
      />
    </div>
  );
}

// ── Trend Chart ───────────────────────────────────────────────────────────────
function TrendChart({ data, period, campaignFilter, campaigns, isLive, isLiveData }) {
  const colors = {
    premium:  "oklch(0.65 0.18 230)",
    mass:     "oklch(0.65 0.18 160)",
    young:    "oklch(0.65 0.18 40)",
    senior:   "oklch(0.65 0.12 300)",
    business: "oklch(0.55 0.10 220)",
  };
  const labels = {
    premium: "Премиум", mass: "Массовый", young: "Молодежь",
    senior: "Средний класс", business: "Бизнес",
  };
  // Модельные (демо) ряды содержат только 3 корзины — рисуем то, что есть.
  const activeKeys = Object.keys(colors).filter(k => data.some(row => row[k] != null));
  const intervalMap = { 7: 1, 30: 4, 90: 9 };
  const interval = intervalMap[data.length] || 4;

  const campaignName = campaignFilter !== "all"
    ? (campaigns || []).find(c => String(c.id) === campaignFilter)?.name
    : null;

  return (
    <Card style={{ padding: 24 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>Динамика принятых предложений</div>
          <div style={{ fontSize: 12, color: "#8896a8", marginTop: 2 }}>
            {campaignName ? `Кампания: ${campaignName}` : `Последние ${data.length} дней`}
            {isLive && isLiveData && <> · <strong style={{ color: "oklch(0.45 0.15 160)" }}>live API</strong></>}
            {isLive && !isLiveData ? " · модельная динамика (демо)" : ""}
          </div>
        </div>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          {activeKeys.map(k => (
            <div key={k} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12, color: "#64748b" }}>
              <div style={{ width: 12, height: 3, borderRadius: 2, background: colors[k] }}></div>
              {labels[k]}
            </div>
          ))}
        </div>
      </div>
      <Recharts.ResponsiveContainer width="100%" height={220}>
        <Recharts.LineChart data={data} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
          <Recharts.CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" vertical={false} />
          <Recharts.XAxis dataKey="date" tick={{ fontSize: 10, fill: "#94a3b8" }} tickLine={false} axisLine={false} interval={interval} />
          <Recharts.YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} tickLine={false} axisLine={false} tickFormatter={v => v >= 1000 ? (v/1000).toFixed(0)+"К" : String(v)} />
          <Recharts.Tooltip
            contentStyle={{ background: "white", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 12 }}
            formatter={(v, n) => [v.toLocaleString("ru"), labels[n]]}
          />
          {activeKeys.map(k => (
            <Recharts.Line key={k} type="monotone" dataKey={k} stroke={colors[k]} strokeWidth={2} dot={false} activeDot={{ r: 4 }} />
          ))}
        </Recharts.LineChart>
      </Recharts.ResponsiveContainer>
    </Card>
  );
}

// ── MCC Heatmap ───────────────────────────────────────────────────────────────
function MccHeatmap({ data, campaignFilter, campaigns, isLive }) {
  const segments = ["premium", "mass", "young", "senior", "business"];
  const segLabels = { premium: "Прем", mass: "Масс", young: "Мол", senior: "Сред", business: "Бизн" };

  function heatColor(v) {
    if (v >= 80) return { bg: "oklch(0.92 0.08 160)", color: "oklch(0.35 0.15 160)" };
    if (v >= 60) return { bg: "oklch(0.93 0.06 230)", color: "oklch(0.40 0.14 230)" };
    if (v >= 40) return { bg: "#f8fafc", color: "#64748b" };
    return { bg: "#fff", color: "#cbd5e1" };
  }

  // Highlight categories from selected campaign
  const selectedCats = new Set();
  if (campaignFilter !== "all") {
    const camp = (campaigns || []).find(c => String(c.id) === campaignFilter);
    const catNameMap = { "5411":"Супермаркеты","5912":"Аптеки","5541":"АЗС","5812":"Рестораны","5045":"Электроника","5600":"Одежда","4111":"Транспорт" };
    camp?.categories.forEach(code => { if (catNameMap[code]) selectedCats.add(catNameMap[code]); });
  }

  return (
    <Card style={{ padding: 24 }}>
      <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", marginBottom: 4 }}>Активность по MCC</div>
      <div style={{ fontSize: 12, color: "#8896a8", marginBottom: 16 }}>
        {isLive ? "CTR по данным API, %" : campaignFilter !== "all" ? "По выбранной кампании" : "Тепловая карта отклика, %"}
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "separate", borderSpacing: "2px", fontSize: 11 }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left", color: "#94a3b8", fontWeight: 600, paddingRight: 8, paddingBottom: 6, fontSize: 10 }}>Категория</th>
              {segments.map(s => (
                <th key={s} style={{ color: "#94a3b8", fontWeight: 600, textAlign: "center", fontSize: 10, paddingBottom: 6 }}>{segLabels[s]}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((row, i) => {
              const isHighlighted = selectedCats.has(row.category);
              return (
                <tr key={i} style={{ background: isHighlighted ? "oklch(0.97 0.03 230)" : "transparent" }}>
                  <td style={{
                    fontSize: 11, color: isHighlighted ? "oklch(0.40 0.18 230)" : "#374151",
                    paddingRight: 8, paddingBottom: 3, whiteSpace: "nowrap",
                    fontWeight: isHighlighted ? 700 : 500,
                  }}>
                    {isHighlighted && "● "}{row.category}
                  </td>
                  {segments.map(s => {
                    const v = row[s];
                    const c = heatColor(v);
                    return (
                      <td key={s} style={{ textAlign: "center", paddingBottom: 3 }}>
                        <div style={{
                          background: c.bg, color: c.color,
                          borderRadius: 4, padding: "3px 4px",
                          fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, fontSize: 11,
                          minWidth: 30, display: "inline-block",
                          outline: isHighlighted ? "1px solid oklch(0.75 0.10 230)" : "none",
                        }}>{v}</div>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ display: "flex", gap: 12, marginTop: 16, flexWrap: "wrap" }}>
        {[{l:"≥80%",bg:"oklch(0.92 0.08 160)"},{l:"60-79%",bg:"oklch(0.93 0.06 230)"},{l:"40-59%",bg:"#f8fafc"},{l:"<40%",bg:"#fff"}].map(item => (
          <div key={item.l} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 10, color: "#94a3b8" }}>
            <div style={{ width: 14, height: 14, borderRadius: 3, background: item.bg, border: "1px solid #e2e8f0" }}></div>
            {item.l}
          </div>
        ))}
        {campaignFilter !== "all" && (
          <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 10, color: "oklch(0.40 0.18 230)", fontWeight: 600 }}>
            ● категории кампании
          </div>
        )}
      </div>
    </Card>
  );
}

// ── Top Campaigns ─────────────────────────────────────────────────────────────
function TopCampaigns({ campaigns, onNavigate, canCreate, statusFilter, campaignFilter }) {
  const sorted = [...campaigns].sort((a, b) => b.roi - a.roi).slice(0, 5);
  const fmtRub = n => n >= 1000000 ? "₽"+(n/1000000).toFixed(1)+"М" : "₽"+(n/1000).toFixed(0)+"К";

  const title = campaignFilter !== "all"
    ? "Детали выбранной кампании"
    : statusFilter === "active" ? "Топ-5 активных кампаний по ROI"
    : statusFilter === "paused" ? "Приостановленные кампании"
    : "Все кампании по ROI";

  const columns = [
    { key: "name", label: "Кампания", render: (v, r) => (
      <div>
        <div style={{ fontWeight: 600, color: "#0d1929", marginBottom: 2 }}>{v}</div>
        <StatusBadge status={r.status} />
      </div>
    )},
    { key: "cashbackRate", label: "Кэшбэк", align: "center", render: v => (
      <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color: "oklch(0.45 0.18 160)" }}>{v}%</span>
    )},
    { key: "spent", label: "Расход / Бюджет", render: (v, r) => (
      <div style={{ minWidth: 140 }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#64748b", marginBottom: 4 }}>
          <span>{fmtRub(v)}</span><span>{fmtRub(r.budget)}</span>
        </div>
        <ProgressBar value={v} max={r.budget} color="oklch(0.65 0.18 230)" />
      </div>
    )},
    { key: "ctr", label: "CTR", align: "center", render: v => (
      <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 600, color: "#374151" }}>{v > 0 ? v + "%" : "—"}</span>
    )},
    { key: "roi", label: "ROI", align: "center", render: v => {
      const color = v >= 3 ? "oklch(0.45 0.18 160)" : v >= 2 ? "oklch(0.55 0.18 230)" : "#64748b";
      return <span style={{ fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, color }}>{v > 0 ? v.toFixed(1)+"×" : "—"}</span>;
    }},
  ];

  return (
    <Card style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ padding: "20px 24px 0", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>{title}</div>
          <div style={{ fontSize: 12, color: "#8896a8", marginTop: 2 }}>
            {sorted.length} кампани{sorted.length === 1 ? "я" : sorted.length < 5 ? "и" : "й"}
          </div>
        </div>
        {canCreate && (
          <Button variant="primary" size="sm" onClick={() => onNavigate("campaigns")}>
            <span>+</span> Новая кампания
          </Button>
        )}
      </div>
      <div style={{ marginTop: 16 }}>
        {sorted.length > 0
          ? <Table columns={columns} rows={sorted} onRowClick={() => onNavigate("campaigns")} />
          : <div style={{ textAlign: "center", padding: "32px", color: "#94a3b8", fontSize: 14 }}>Нет кампаний для отображения</div>
        }
      </div>
    </Card>
  );
}

export default Dashboard;
