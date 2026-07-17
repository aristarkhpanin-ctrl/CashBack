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
import { useLiveChannels, useLiveFunnel, useLiveMatrix } from "@/shared/api/live";

const AppData = {
  USERS, ROLE_LABELS, PERMISSIONS, MCC_CATEGORIES, SEGMENTS,
  CAMPAIGNS, generateTrendData, MCC_HEATMAP, FUNNEL_DATA,
  MATRIX_DATA, CHANNEL_DATA, computeActivityMatrix,
};



// ── Helpers ───────────────────────────────────────────────────────────────────

function seededRng(seed) {
  let s = seed;
  return () => { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646; };
}

// Determine if a campaign has real activity data
function campaignHasData(campaign) {
  if (!campaign) return true; // "all campaigns" always has aggregate data
  return campaign.spent > 0 && campaign.ctr > 0;
}

// Compute segment-based audience multiplier (fraction of total)
function getSegmentMultiplier(segmentId) {
  if (!segmentId || segmentId === "all") return 1;
  const SEGMENTS = AppData.SEGMENTS;
  const totalAll = SEGMENTS.reduce((s, x) => s + x.count, 0);
  const seg = SEGMENTS.find(s => s.id === segmentId);
  if (!seg) return 1;
  return seg.count / totalAll;
}

// Build funnel — for new/draft campaigns only show top 2 stages, rest = 0
function buildFunnel(campaign, periodMult, segmentId = "all") {
  const hasData = campaignHasData(campaign);
  const baseReach = campaign ? campaign.reach : 892000;
  const segmentMult = getSegmentMultiplier(segmentId);
  // Audience grows with period (cumulative reach over the window) and shrinks with segment filter
  const scaled = Math.round(baseReach * periodMult * segmentMult);

  const STAGE_DEFS = [
    { stage: "Целевая аудитория",      color: "oklch(0.55 0.18 230)" },
    { stage: "Получили предложение",   color: "oklch(0.55 0.18 220)" },
    { stage: "Открыли",                color: "oklch(0.55 0.18 210)" },
    { stage: "Приняли",                color: "oklch(0.55 0.18 200)" },
    { stage: "Совершили транзакцию",   color: "oklch(0.55 0.18 190)" },
    { stage: "Получили кэшбэк",        color: "oklch(0.60 0.18 160)" },
  ];

  if (!hasData) {
    const rng = seededRng(campaign ? campaign.id * 7 : 99);
    const sent = Math.round(scaled * 0.8 * (0.9 + rng() * 0.1));
    return STAGE_DEFS.map((s, i) => ({
      ...s,
      value: i === 0 ? scaled : i === 1 ? sent : 0,
      pending: i >= 2,
    }));
  }

  // Segments differ in conversion quality
  const segmentBoost = {
    premium: 1.28, business: 1.18, young: 1.05, mass: 0.95, senior: 0.78, all: 1.0,
  };
  const boost = segmentBoost[segmentId] || 1.0;

  // Base conversion percentages; boost only applied to post-delivery stages
  const pcts = [1.000, 0.800, 0.480 * boost, 0.183 * boost, 0.110 * boost, 0.102 * boost];
  const rng = seededRng(campaign ? campaign.id * 7 : 99);
  return STAGE_DEFS.map((s, i) => ({
    ...s,
    value: Math.round(scaled * Math.min(pcts[i], 1) * (0.85 + rng() * 0.30)),
    pending: false,
  }));
}

// Build channel data
function buildChannels(campaign, periodMult, segmentId = "all") {
  const hasData = campaignHasData(campaign);
  const reachBase = campaign ? campaign.reach : 1245000;
  const segmentMult = getSegmentMultiplier(segmentId);
  const scaledReach = reachBase * periodMult * segmentMult;
  const rng = seededRng(campaign ? campaign.id * 13 : 42);

  // Different segments use different channel mixes
  const segChannelMix = {
    premium:  [0.20, 0.10, 0.45, 0.25], // Email-heavy for premium
    business: [0.18, 0.12, 0.50, 0.20],
    young:    [0.45, 0.08, 0.15, 0.32], // Push + App for young
    mass:     [0.38, 0.30, 0.20, 0.12],
    senior:   [0.20, 0.55, 0.18, 0.07], // SMS-heavy for senior
    all:      [0.36, 0.26, 0.22, 0.16],
  };
  const mix = segChannelMix[segmentId] || segChannelMix.all;
  const channels = ["Push", "SMS", "Email", "App"];

  // Conversion rates also differ by segment
  const segConvBoost = { premium: 1.3, business: 1.2, young: 1.05, mass: 0.95, senior: 0.85, all: 1.0 };
  const cBoost = segConvBoost[segmentId] || 1.0;

  return channels.map((channel, i) => {
    const sent = Math.round(scaledReach * mix[i] * (0.9 + rng()*0.2));
    if (!hasData) return { channel, sent, opened: 0, converted: 0, pending: true };
    const openRates = [0.44, 0.35, 0.30, 0.67];
    const convRates = [0.338, 0.402, 0.333, 0.40];
    const rng2 = seededRng(i * 31 + (campaign?.id || 0) + (segmentId === "all" ? 0 : segmentId.length));
    const opened    = Math.round(sent * openRates[i]  * cBoost * (0.9 + rng2() * 0.2));
    const converted = Math.round(opened * convRates[i] * cBoost * (0.9 + rng2() * 0.2));
    return { channel, sent, opened, converted, pending: false };
  });
}

// Build matrix data — filtered by campaign's segments and categories, scaled by period
function buildMatrix(campaign, period = "30d") {
  const BASE = AppData.MATRIX_DATA;
  const { SEGMENTS } = AppData;

  // Period maturity multiplier — same logic as Dashboard
  const periodMult = { "7d": 0.62, "30d": 1.00, "90d": 1.12 }[period] || 1;
  const periodRng = seededRng(period === "7d" ? 7 : period === "90d" ? 91 : 31);

  const applyPeriod = v => {
    const variation = 0.94 + periodRng() * 0.12;
    return Math.max(0, Math.min(99, Math.round(v * periodMult * variation)));
  };

  if (!campaign) {
    return {
      segments: BASE.segments,
      categories: BASE.categories,
      values: BASE.values.map(row => row.map(applyPeriod)),
    };
  }

  const catNameMap = {
    "5411":"Супермаркеты","5912":"Аптеки","5541":"АЗС",
    "5812":"Рестораны","5045":"Электроника","5600":"Одежда","4111":"Транспорт",
  };

  const activeCatNames = new Set(campaign.categories.map(c => catNameMap[c]).filter(Boolean));
  const activeSegNames = new Set(campaign.segments.map(id => {
    const seg = SEGMENTS.find(s => s.id === id);
    return seg ? seg.name : null;
  }).filter(Boolean));

  const rng = seededRng(campaign.id * 17);

  return {
    segments: BASE.segments,
    categories: BASE.categories,
    values: BASE.values.map((segRow, si) => {
      const segName = BASE.segments[si];
      const inSeg = activeSegNames.has(segName);
      return segRow.map((v, ci) => {
        const catName = BASE.categories[ci];
        const inCat = activeCatNames.has(catName);
        let adj = v;
        if (inSeg && inCat) adj = v * (1.1 + rng() * 0.15);
        else if (inSeg || inCat) adj = v * (0.95 + rng() * 0.08);
        else adj = v * (0.80 + rng() * 0.10);
        const variation = 0.94 + periodRng() * 0.12;
        return Math.max(0, Math.min(99, Math.round(adj * periodMult * variation)));
      });
    }),
    activeSegments: activeSegNames,
    activeCategories: activeCatNames,
  };
}

// Compute summary KPIs from funnel — pending stages show as null
function funnelKPIs(funnel, hasData) {
  const audience   = funnel[0]?.value || 0;
  const sent       = funnel[1]?.value || 0;
  const accepted   = funnel[3]?.value || 0;
  const transacted = funnel[4]?.value || 0;
  const cashbacked = funnel[5]?.value || 0;
  const acceptRate = audience > 0 && hasData ? ((accepted / audience) * 100).toFixed(1) : null;
  const transRate  = accepted > 0 && hasData ? ((transacted / accepted) * 100).toFixed(1) : null;
  const totalCashback = cashbacked * 46;
  const avgCashback   = 46;
  return { audience, sent, accepted, acceptRate, transacted, transRate, totalCashback, avgCashback, hasData };
}

const PERIOD_MULT = { "7d": 0.23, "30d": 1, "90d": 2.6 };

// ── Main Analytics Component ──────────────────────────────────────────────────
function Analytics({ currentUser, campaigns: CAMPAIGNS, isLive, segments, mccCategories }) {
  // Фаза 21: справочники из единого источника (проставляем до чтения в
  // подкомпонентах, которые обращаются к AppData.SEGMENTS/MCC_CATEGORIES).
  if (segments) AppData.SEGMENTS = segments;
  if (mccCategories) AppData.MCC_CATEGORIES = mccCategories;
  const [selectedCampaign, setSelectedCampaign] = useState("all");
  const [period, setPeriod] = useState("30d");
  const [segment, setSegment] = useState("all");
  const [activeTab, setActiveTab] = useState("funnel");

  const activeCampaigns = CAMPAIGNS.filter(c => c.status !== "draft");

  const campaign = selectedCampaign === "all"
    ? null
    : CAMPAIGNS.find(c => String(c.id) === selectedCampaign);

  const periodMult = PERIOD_MULT[period] || 1;
  const periodDays = { "7d": 7, "30d": 30, "90d": 90 }[period] || 30;

  // Live-данные: /analytics/funnel и /analytics/segment-matrix
  const funnelQ = useLiveFunnel(campaign?.live ? String(campaign.id) : null, periodDays, !!isLive);
  const matrixQ = useLiveMatrix(periodDays, !!isLive);
  const channelsQ = useLiveChannels(campaign?.live ? String(campaign.id) : null, periodDays, !!isLive);
  const liveFunnel = isLive ? funnelQ.data : null;
  const liveMatrix = isLive ? matrixQ.data : null;
  const liveChannels = isLive ? channelsQ.data : null;

  const hasData = useMemo(() => {
    // live: данные есть, если хоть один этап после «получили» ненулевой
    if (liveFunnel) return liveFunnel.slice(2).some(s => s.value > 0);
    return campaignHasData(campaign);
  }, [selectedCampaign, liveFunnel]);

  // All derived data — recomputed on filter change (campaign, period, segment)
  const funnel   = useMemo(
    () => liveFunnel ?? buildFunnel(campaign, periodMult, segment),
    [selectedCampaign, period, segment, liveFunnel],
  );
  const channels = useMemo(
    () => liveChannels ?? buildChannels(campaign, periodMult, segment),
    [selectedCampaign, period, segment, liveChannels],
  );
  const matrix   = useMemo(() => {
    if (liveMatrix) {
      // подсветка активных сегментов/категорий выбранной кампании
      const activeSegments = new Set(
        (campaign?.segments || []).map(id => AppData.SEGMENTS.find(s => s.id === id)?.name).filter(Boolean),
      );
      const activeCategories = new Set(
        (campaign?.categories || []).map(code => {
          const m = AppData.MCC_CATEGORIES.find(x => x.code === code);
          return m ? m.name : `MCC ${code}`;
        }),
      );
      return { ...liveMatrix, activeSegments, activeCategories };
    }
    return AppData.computeActivityMatrix(campaign, period);
  }, [selectedCampaign, period, liveMatrix]);
  const kpis     = useMemo(() => {
    const base = funnelKPIs(funnel, hasData);
    if (!liveFunnel) return base;
    // в live-режиме сумма кэшбэка берётся из статистики кампаний, а не из эвристики
    const cashbackPaid = campaign
      ? (campaign.stats?.cashbackPaid ?? 0)
      : CAMPAIGNS.reduce((s, c) => s + (c.stats?.cashbackPaid ?? 0), 0);
    const paidUsers = funnel[5]?.value || 0;
    return {
      ...base,
      totalCashback: cashbackPaid,
      avgCashback: paidUsers > 0 ? Math.round(cashbackPaid / paidUsers) : 0,
    };
  }, [funnel, hasData, liveFunnel, selectedCampaign, CAMPAIGNS]);

  const fmt    = n => n >= 1000000 ? (n/1000000).toFixed(1)+"М" : n >= 1000 ? (n/1000).toFixed(0)+"К" : String(n);
  const fmtRub = n => n >= 1000000 ? "₽"+(n/1000000).toFixed(1)+"М" : "₽"+(n/1000).toFixed(0)+"К";

  // Trend deltas shift with period
  const trendAudience  = period === "7d" ? 3.1  : period === "30d" ? 8.2  : 21.4;
  const trendAccepted  = period === "7d" ? 1.8  : period === "30d" ? 4.1  : 10.6;
  const trendTransact  = period === "7d" ? 1.2  : period === "30d" ? 2.8  : 7.9;
  const trendCashback  = period === "7d" ? 4.3  : period === "30d" ? 11.5 : 28.2;

  // Pending banner for new campaigns
  const PendingBanner = () => (
    <div style={{
      background: "#fef9c3", border: "1px solid #fde68a", borderRadius: 10,
      padding: "12px 16px", display: "flex", alignItems: "center", gap: 10, fontSize: 13, color: "#92400e",
    }}>
      <span style={{ fontSize: 18 }}>⏳</span>
      <div>
        <strong>Данные ещё собираются.</strong> Кампания недавно запущена — статистика по принятию предложений, транзакциям и кэшбэку появится после первых активностей клиентов.
      </div>
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24, maxWidth: 1400 }}>

      {/* Filters bar */}
      <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap", background: "white", borderRadius: 12, padding: "14px 20px", border: "1px solid #e8edf4" }}>
        {/* Campaign */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>Кампания</label>
          <select value={selectedCampaign} onChange={e => setSelectedCampaign(e.target.value)} style={{
            padding: "8px 12px", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 13,
            fontFamily: "inherit", background: "white", color: "#0d1929", outline: "none", minWidth: 240,
          }}>
            <option value="all">Все кампании</option>
            {activeCampaigns.map(c => <option key={c.id} value={String(c.id)}>{c.name}</option>)}
          </select>
        </div>

        {/* Period */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>Период</label>
          <div style={{ display: "flex", background: "#f8fafc", borderRadius: 8, padding: 3, border: "1px solid #e2e8f0" }}>
            {[{ v: "7d", l: "7 дн." }, { v: "30d", l: "30 дн." }, { v: "90d", l: "90 дн." }].map(p => (
              <button key={p.v} onClick={() => setPeriod(p.v)} style={{
                padding: "6px 14px", borderRadius: 6, fontSize: 12, fontWeight: 600, border: "none",
                cursor: "pointer", transition: "all 0.15s",
                background: period === p.v ? "oklch(0.55 0.18 230)" : "transparent",
                color: period === p.v ? "white" : "#64748b",
              }}>{p.l}</button>
            ))}
          </div>
        </div>

        {/* Segment filter */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>Сегмент</label>
          <select value={segment} onChange={e => setSegment(e.target.value)} style={{
            padding: "8px 12px", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 13,
            fontFamily: "inherit", background: "white", color: "#0d1929", outline: "none",
          }}>
            <option value="all">Все сегменты</option>
            {AppData.SEGMENTS.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </div>

        {/* Active filters indicator */}
        {(selectedCampaign !== "all" || period !== "30d" || segment !== "all") && (
          <div style={{ display: "flex", alignItems: "flex-end", gap: 6, paddingBottom: 2 }}>
            <div style={{ background: "#fef9c3", border: "1px solid #fde68a", borderRadius: 20, padding: "4px 12px", fontSize: 12, fontWeight: 600, color: "#92400e" }}>
              Фильтры активны
            </div>
            <button onClick={() => { setSelectedCampaign("all"); setPeriod("30d"); setSegment("all"); }} style={{
              background: "none", border: "none", cursor: "pointer", fontSize: 12, color: "#94a3b8", textDecoration: "underline",
            }}>сбросить</button>
          </div>
        )}

        <div style={{ marginLeft: "auto", fontSize: 12, color: "#94a3b8", alignSelf: "flex-end", paddingBottom: 2 }}>
          Обновлено: {new Date().toLocaleDateString("ru")} {new Date().toLocaleTimeString("ru", { hour: "2-digit", minute: "2-digit" })}
        </div>
      </div>

      {/* Summary KPIs — fully reactive. Фиктивные %-тренды скрываем в live. */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 }}>
        <StatCard label="Целевая аудитория"    value={fmt(kpis.audience)}     sub="клиентов в выборке"           trend={isLive ? undefined : trendAudience} color="oklch(0.65 0.18 230)" icon="👥" />
        <StatCard label="Приняли предложение"  value={hasData ? fmt(kpis.accepted)  : "—"} sub={hasData ? `${kpis.acceptRate}% от охвата`    : "нет данных"} trend={hasData && !isLive ? trendAccepted : undefined} color="oklch(0.65 0.18 160)" icon="✅" />
        <StatCard label="Совершили транзакцию" value={hasData ? fmt(kpis.transacted) : "—"} sub={hasData ? `${kpis.transRate}% от принявших`  : "нет данных"} trend={hasData && !isLive ? trendTransact : undefined} color="oklch(0.65 0.18 40)"  icon="💳" />
        <StatCard label="Выдано кэшбэка"       value={hasData ? fmtRub(kpis.totalCashback) : "—"} sub={hasData ? `ср. ₽${kpis.avgCashback} на клиента` : "нет данных"} trend={hasData && !isLive ? trendCashback : undefined} color="oklch(0.65 0.18 200)" icon="💰" />
      </div>

      {/* Pending banner for new campaigns */}
      {!hasData && campaign && <PendingBanner />}

      {/* Tabs */}
      <Tabs
        tabs={[
          { id: "funnel",   label: "Воронка предложения" },
          { id: "matrix",   label: "Матрица сегмент × категория" },
          { id: "channels", label: "Эффективность каналов" },
        ]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {activeTab === "funnel"   && <FunnelView   data={funnel}    campaign={campaign} period={period} segment={segment} hasData={hasData} isLive={!!liveFunnel} />}
      {activeTab === "matrix"   && <MatrixView   data={matrix}    campaign={campaign} segment={segment} period={period} hasData={hasData} isLive={!!liveMatrix} />}
      {activeTab === "channels" && <ChannelsView data={channels}  campaign={campaign} period={period} segment={segment} hasData={hasData} isLive={isLive} isLiveData={!!liveChannels} />}
    </div>
  );
}

// ── Funnel View ───────────────────────────────────────────────────────────────
function FunnelView({ data, campaign, period, segment, hasData, isLive }) {
  const activeData = data.filter(d => !d.pending);
  const maxVal = activeData[0]?.value || data[0]?.value || 1;
  const fmt = n => n >= 1000000 ? (n/1000000).toFixed(2)+"М" : n >= 1000 ? (n/1000).toFixed(0)+"К" : String(n);
  const segName = segment && segment !== "all"
    ? AppData.SEGMENTS.find(s => s.id === segment)?.name
    : null;
  const periodLabel = period === "7d" ? "7 дней" : period === "30d" ? "30 дней" : "90 дней";

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
      <Card style={{ padding: 28 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", marginBottom: 2 }}>Воронка кэшбэк-предложения</div>
        <div style={{ fontSize: 12, color: "#8896a8", marginBottom: 24 }}>
          {campaign ? campaign.name : "Все кампании"} · {periodLabel}
          {!isLive && segName && <> · <strong style={{ color: "oklch(0.45 0.18 230)" }}>сегмент {segName}</strong></>}
          {isLive && <> · <strong style={{ color: "oklch(0.45 0.15 160)" }}>live API</strong></>}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 0 }}>
          {data.map((stage, i) => {
            const isPending = stage.pending;
            const widthPct = isPending ? 0 : Math.round((stage.value / maxVal) * 100);
            const prevVal = i > 0 ? data[i-1].value : null;
            const prevPending = i > 0 ? data[i-1].pending : false;
            const convPct = prevVal && !isPending && !prevPending
              ? ((stage.value / prevVal) * 100).toFixed(1)
              : null;

            return (
              <div key={i}>
                {i > 0 && (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", justifyContent: "center" }}>
                    <div style={{ height: 1, flex: 1, background: "#f1f5f9" }} />
                    <div style={{
                      fontSize: 11, fontWeight: 700,
                      color: convPct ? "#94a3b8" : "#d1d5db",
                      background: "#f8fafc", padding: "2px 8px", borderRadius: 10,
                    }}>
                      {convPct ? `↓ ${convPct}%` : "· · ·"}
                    </div>
                    <div style={{ height: 1, flex: 1, background: "#f1f5f9" }} />
                  </div>
                )}
                <div style={{ opacity: isPending ? 0.45 : 1, transition: "opacity 0.3s" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5, fontSize: 12 }}>
                    <span style={{ fontWeight: 600, color: isPending ? "#94a3b8" : "#374151" }}>{stage.stage}</span>
                    <span style={{
                      fontFamily: "'JetBrains Mono', monospace", fontWeight: 700,
                      color: isPending ? "#94a3b8" : "#0d1929",
                      fontSize: isPending ? 11 : 12,
                    }}>
                      {isPending ? "ожидание данных" : fmt(stage.value)}
                    </span>
                  </div>
                  <div style={{ background: isPending ? "#f8fafc" : "#f1f5f9", borderRadius: 6, height: 28, overflow: "hidden", position: "relative", border: isPending ? "1px dashed #e2e8f0" : "none" }}>
                    {isPending ? (
                      <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", paddingLeft: 12 }}>
                        <span style={{ fontSize: 11, color: "#cbd5e1" }}>—</span>
                      </div>
                    ) : (
                      <div style={{
                        position: "absolute", inset: 0,
                        width: `${widthPct}%`,
                        background: stage.color,
                        borderRadius: 6,
                        transition: "width 0.5s ease",
                        display: "flex", alignItems: "center", paddingLeft: 10,
                      }}>
                        <span style={{ fontSize: 11, fontWeight: 700, color: "white", whiteSpace: "nowrap" }}>{widthPct}%</span>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        {!hasData && campaign && (
          <div style={{ marginTop: 16, padding: "10px 14px", background: "#fef9c3", borderRadius: 8, fontSize: 12, color: "#92400e" }}>
            ⏳ Статистика появится после первых активностей клиентов
          </div>
        )}
      </Card>

      <Card style={{ padding: 28 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", marginBottom: 2 }}>Конверсии по этапам</div>
        <div style={{ fontSize: 12, color: "#8896a8", marginBottom: 24 }}>Абсолютные значения</div>
        <Recharts.ResponsiveContainer width="100%" height={280}>
          <Recharts.BarChart
            data={data.map(d => ({ name: d.stage.split(" ").slice(0,2).join(" "), value: d.pending ? 0 : d.value, pending: d.pending }))}
            layout="vertical" margin={{ top: 0, right: 40, left: 0, bottom: 0 }}>
            <Recharts.CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" horizontal={false} />
            <Recharts.XAxis type="number" tick={{ fontSize: 10, fill: "#94a3b8" }} tickLine={false} axisLine={false} tickFormatter={v => fmt(v)} />
            <Recharts.YAxis type="category" dataKey="name" tick={{ fontSize: 10, fill: "#64748b" }} width={90} tickLine={false} axisLine={false} />
            <Recharts.Tooltip
              contentStyle={{ background: "white", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 12 }}
              formatter={(v, _, props) => [props.payload.pending ? "ожидание данных" : v.toLocaleString("ru"), "Клиентов"]}
            />
            <Recharts.Bar dataKey="value" radius={[0, 4, 4, 0]}>
              {data.map((d, i) => (
                <Recharts.Cell key={i} fill={d.pending ? "#e2e8f0" : d.color} />
              ))}
            </Recharts.Bar>
          </Recharts.BarChart>
        </Recharts.ResponsiveContainer>
      </Card>
    </div>
  );
}

// ── Matrix View ───────────────────────────────────────────────────────────────
function MatrixView({ data, campaign, segment, period, hasData, isLive }) {
  const [metric, setMetric] = useState("response");
  const [hovered, setHovered] = useState(null);
  const periodLabel = period === "7d" ? "7 дней" : period === "30d" ? "30 дней" : "90 дней";

  function heatColor(v) {
    if (v >= 80) return { bg: "oklch(0.85 0.14 160)", color: "oklch(0.25 0.18 160)", fw: 700 };
    if (v >= 60) return { bg: "oklch(0.90 0.10 230)", color: "oklch(0.30 0.18 230)", fw: 700 };
    if (v >= 40) return { bg: "oklch(0.95 0.05 230)", color: "oklch(0.45 0.12 230)", fw: 600 };
    if (v >= 20) return { bg: "#f1f5f9", color: "#64748b", fw: 500 };
    return { bg: "white", color: "#cbd5e1", fw: 400 };
  }

  // Filter columns if segment is selected (compare via id → name lookup)
  const segName = segment !== "all"
    ? AppData.SEGMENTS.find(s => s.id === segment)?.name
    : null;
  const displaySegments = segment === "all"
    ? data.segments
    : data.segments.filter(s => s === segName);

  // Compute insights from current matrix
  let bestVal = 0, bestCat = "", bestSeg = "";
  let worstVal = 100, worstCat = "", worstSeg = "";
  data.categories.forEach((cat, ci) => {
    data.segments.forEach((seg, si) => {
      const v = metric === "conversion" ? Math.round(data.values[si][ci] * 0.65) : data.values[si][ci];
      if (v > bestVal) { bestVal = v; bestCat = cat; bestSeg = seg; }
      if (v < worstVal) { worstVal = v; worstCat = cat; worstSeg = seg; }
    });
  });

  // Most-reached: highest total across all segments
  let maxTotalCat = "", maxTotal = 0;
  data.categories.forEach((cat, ci) => {
    const total = data.segments.reduce((s, _, si) => s + data.values[si][ci], 0);
    if (total > maxTotal) { maxTotal = total; maxTotalCat = cat; }
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <Card style={{ padding: 28, overflow: "auto" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24, flexWrap: "wrap", gap: 12 }}>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>Матрица сегмент × категория</div>
            <div style={{ fontSize: 12, color: "#8896a8", marginTop: 2 }}>
              {campaign ? campaign.name : "Все кампании"} · {periodLabel} — показатель: {metric === "response" ? "отклик" : "конверсия"}, %
              {isLive && <> · <strong style={{ color: "oklch(0.45 0.15 160)" }}>live API</strong></>}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {[{ v: "response", l: "Отклик" }, { v: "conversion", l: "Конверсия" }].map(m => (
              <button key={m.v} onClick={() => setMetric(m.v)} style={{
                padding: "6px 14px", borderRadius: 8, fontSize: 12, fontWeight: 600,
                background: metric === m.v ? "oklch(0.55 0.18 230)" : "white",
                color: metric === m.v ? "white" : "#64748b",
                border: "1px solid " + (metric === m.v ? "transparent" : "#e2e8f0"), cursor: "pointer",
              }}>{m.l}</button>
            ))}
          </div>
        </div>

        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "separate", borderSpacing: 4, minWidth: 500 }}>
            <thead>
              <tr>
                <th style={{ width: 130, textAlign: "left", padding: "8px 12px", fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>Категория / Сегмент</th>
                {displaySegments.map(s => {
                  const isActive = data.activeSegments?.has(s);
                  return (
                    <th key={s} style={{ textAlign: "center", padding: "8px 6px", fontSize: 12, fontWeight: 700, color: isActive ? "oklch(0.40 0.18 230)" : "#374151", minWidth: 80 }}>
                      {isActive ? "● " : ""}{s}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {data.categories.map((cat, ci) => {
                const isActiveCat = data.activeCategories?.has(cat);
                return (
                  <tr key={ci}>
                    <td style={{ padding: "4px 12px", fontSize: 13, fontWeight: isActiveCat ? 700 : 600, color: isActiveCat ? "oklch(0.40 0.18 230)" : "#374151", whiteSpace: "nowrap" }}>
                      {isActiveCat ? "● " : ""}{cat}
                    </td>
                    {displaySegments.map((seg, si) => {
                      const realSi = data.segments.indexOf(seg);
                      const v = data.values[realSi][ci];
                      const adj = metric === "conversion" ? Math.round(v * 0.65) : v;
                      const c = heatColor(adj);
                      const isHov = hovered === `${ci}-${si}`;
                      return (
                        <td key={si} style={{ padding: "4px" }}>
                          <div
                            onMouseEnter={() => setHovered(`${ci}-${si}`)}
                            onMouseLeave={() => setHovered(null)}
                            style={{
                              background: (!hasData && campaign) ? "#f8fafc" : c.bg,
                              color:      (!hasData && campaign) ? "#cbd5e1" : c.color,
                              borderRadius: 8, padding: "10px 6px",
                              textAlign: "center", fontFamily: "'JetBrains Mono', monospace",
                              fontWeight: (!hasData && campaign) ? 400 : c.fw, fontSize: 14,
                              transition: "all 0.15s",
                              outline: isHov && hasData ? "2px solid oklch(0.65 0.18 230)" : "none",
                              transform: isHov && hasData ? "scale(1.05)" : "none",
                            }}
                          >{(!hasData && campaign) ? "—" : adj + "%"}</div>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {!hasData && campaign && (
          <div style={{ marginTop: 16, padding: "10px 14px", background: "#fef9c3", borderRadius: 8, fontSize: 12, color: "#92400e" }}>
            ⏳ Матрица отклика будет заполнена после того, как клиенты начнут реагировать на предложения
          </div>
        )}

        <div style={{ display: "flex", gap: 16, marginTop: 20, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>ЛЕГЕНДА:</span>
          {[
            { l: "≥80% — Высокий",   bg: "oklch(0.85 0.14 160)", color: "oklch(0.25 0.18 160)" },
            { l: "60–79% — Хороший", bg: "oklch(0.90 0.10 230)", color: "oklch(0.30 0.18 230)" },
            { l: "40–59% — Средний", bg: "oklch(0.95 0.05 230)", color: "oklch(0.45 0.12 230)" },
            { l: "<40% — Низкий",    bg: "#f1f5f9",              color: "#64748b" },
          ].map(item => (
            <div key={item.l} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: item.color }}>
              <div style={{ width: 16, height: 16, borderRadius: 4, background: item.bg, border: "1px solid #e2e8f0" }}></div>
              {item.l}
            </div>
          ))}
          {campaign && (
            <span style={{ fontSize: 11, color: "oklch(0.40 0.18 230)", fontWeight: 600 }}>● активные сегменты / категории кампании</span>
          )}
        </div>
      </Card>

      {/* Dynamic insights */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16 }}>
        {[
          { icon: "🏆", label: "Лучшая комбинация",  value: `${bestSeg} × ${bestCat}`,  sub: `${bestVal}% отклик`,               color: "oklch(0.85 0.14 160)" },
          { icon: "📈", label: "Наибольший охват",    value: maxTotalCat,                 sub: "суммарно по сегментам",             color: "oklch(0.90 0.10 230)" },
          { icon: "⚠️", label: "Требует внимания",   value: `${worstSeg} × ${worstCat}`, sub: `${worstVal}% — пересмотреть`,      color: "#fef9c3" },
        ].map(item => (
          <Card key={item.label} style={{ padding: "16px 20px", background: item.color, border: "none" }}>
            <div style={{ fontSize: 20, marginBottom: 6 }}>{item.icon}</div>
            <div style={{ fontSize: 11, color: "#64748b", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 4 }}>{item.label}</div>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>{item.value}</div>
            <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>{item.sub}</div>
          </Card>
        ))}
      </div>
    </div>
  );
}

// ── Channels View ─────────────────────────────────────────────────────────────
function ChannelsView({ data, campaign, period, segment, hasData, isLive, isLiveData }) {
  const fmt = n => n >= 1000000 ? (n/1000000).toFixed(1)+"М" : n >= 1000 ? (n/1000).toFixed(0)+"К" : String(n);
  const colors = ["oklch(0.55 0.18 230)", "oklch(0.55 0.18 160)", "oklch(0.55 0.18 40)"];
  const segName = segment && segment !== "all"
    ? AppData.SEGMENTS.find(s => s.id === segment)?.name
    : null;
  const periodLabel = period === "7d" ? "7 дней" : period === "30d" ? "30 дней" : "90 дней";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {isLive && !isLiveData && (
        <div style={{ background: "#fef9c3", border: "1px solid #fde68a", borderRadius: 10, padding: "10px 14px", fontSize: 12, color: "#92400e" }}>
          ⚠ Канал доставки ещё не накопился в БД (recommendations.channel) — ниже модельные (демо) данные.
        </div>
      )}
      {isLive && isLiveData && (
        <div style={{ background: "oklch(0.95 0.05 160)", border: "1px solid oklch(0.85 0.08 160)", borderRadius: 10, padding: "10px 14px", fontSize: 12, color: "oklch(0.35 0.15 160)" }}>
          ✓ Данные каналов — из /analytics/channels (live API)
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
        <Card style={{ padding: 28 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", marginBottom: 2 }}>Отправлено / Открыто / Конверсия</div>
          <div style={{ fontSize: 12, color: "#8896a8", marginBottom: 24 }}>
            {campaign ? campaign.name : "Все кампании"} · {periodLabel}{segName && <> · <strong style={{ color: "oklch(0.45 0.18 230)" }}>сегмент {segName}</strong></>}
          </div>
          <Recharts.ResponsiveContainer width="100%" height={240}>
            <Recharts.BarChart data={data} margin={{ top: 0, right: 10, left: -10, bottom: 0 }}>
              <Recharts.CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" vertical={false} />
              <Recharts.XAxis dataKey="channel" tick={{ fontSize: 12, fill: "#64748b" }} tickLine={false} axisLine={false} />
              <Recharts.YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} tickLine={false} axisLine={false} tickFormatter={fmt} />
              <Recharts.Tooltip
                contentStyle={{ background: "white", border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 12 }}
                formatter={(v, n) => [v.toLocaleString("ru"), { sent:"Отправлено", opened:"Открыто", converted:"Конвертировано" }[n]]}
              />
              <Recharts.Legend formatter={v => ({ sent:"Отправлено", opened:"Открыто", converted:"Конвертировано" }[v])} wrapperStyle={{ fontSize: 12 }} />
              <Recharts.Bar dataKey="sent"      fill={colors[0]} radius={[3,3,0,0]} />
              <Recharts.Bar dataKey="opened"    fill={colors[1]} radius={[3,3,0,0]} />
              <Recharts.Bar dataKey="converted" fill={colors[2]} radius={[3,3,0,0]} />
            </Recharts.BarChart>
          </Recharts.ResponsiveContainer>
        </Card>

        <Card style={{ padding: 28 }}>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929", marginBottom: 2 }}>Конверсия по каналам</div>
          <div style={{ fontSize: 12, color: "#8896a8", marginBottom: 24 }}>Доля конвертированных от открывших</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 18, marginTop: 8 }}>
            {data.map((d, i) => {
              const openRate = d.sent > 0 && !d.pending ? ((d.opened / d.sent) * 100).toFixed(1) : null;
              const convRate = d.opened > 0 && !d.pending ? ((d.converted / d.opened) * 100).toFixed(1) : null;
              return (
                <div key={d.channel}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                    <span style={{ fontSize: 14, fontWeight: 700, color: "#374151" }}>{d.channel}</span>
                    <div style={{ display: "flex", gap: 16, fontSize: 12 }}>
                      <span style={{ color: "#8896a8" }}>Open: <strong style={{ color: "#374151" }}>{openRate ? openRate+"%" : "—"}</strong></span>
                      <span style={{ color: "#8896a8" }}>Conv: <strong style={{ color: "oklch(0.45 0.18 160)" }}>{convRate ? convRate+"%" : "—"}</strong></span>
                    </div>
                  </div>
                  <div style={{ position: "relative", height: 8, background: "#f1f5f9", borderRadius: 99, border: d.pending ? "1px dashed #e2e8f0" : "none" }}>
                    {!d.pending && openRate && <div style={{ position: "absolute", inset: 0, width: openRate + "%", background: colors[1], borderRadius: 99, opacity: 0.4, transition: "width 0.4s" }} />}
                    {!d.pending && convRate && <div style={{ position: "absolute", inset: 0, width: convRate + "%", background: colors[2], borderRadius: 99, transition: "width 0.4s" }} />}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      </div>

      {/* Channel KPI summary cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        {data.map((d, i) => {
          const openRate = d.sent > 0 && !d.pending ? ((d.opened / d.sent) * 100).toFixed(1) : null;
          const convRate = d.opened > 0 && !d.pending ? ((d.converted / d.opened) * 100).toFixed(1) : null;
          const channelColors = [
            "oklch(0.55 0.18 230)", "oklch(0.55 0.18 160)",
            "oklch(0.55 0.18 40)",  "oklch(0.55 0.18 280)",
          ];
          return (
            <div key={d.channel} style={{ background: "white", borderRadius: 12, padding: "16px 18px", border: "1px solid #e8edf4", opacity: d.pending ? 0.6 : 1 }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: channelColors[i], marginBottom: 10 }}>{d.channel}</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {[
                  { l: "Отправлено",  v: d.sent.toLocaleString("ru") },
                  { l: "Open rate",   v: openRate ? openRate+"%" : "—" },
                  { l: "Conv rate",   v: convRate ? convRate+"%" : "—" },
                  { l: "Конверсий",   v: d.pending ? "—" : d.converted.toLocaleString("ru") },
                ].map(r => (
                  <div key={r.l} style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                    <span style={{ color: "#94a3b8" }}>{r.l}</span>
                    <span style={{ fontWeight: 700, color: r.v === "—" ? "#cbd5e1" : "#374151", fontFamily: "'JetBrains Mono', monospace" }}>{r.v}</span>
                  </div>
                ))}
              </div>
              {d.pending && (
                <div style={{ marginTop: 8, fontSize: 11, color: "#ca8a04", fontWeight: 600 }}>⏳ ожидание данных</div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default Analytics;
