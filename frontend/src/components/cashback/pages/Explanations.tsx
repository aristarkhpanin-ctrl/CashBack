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
import { useLiveExplanation, useMlCustomers } from "@/shared/api/live";

const AppData = {
  USERS, ROLE_LABELS, PERMISSIONS, MCC_CATEGORIES, SEGMENTS,
  CAMPAIGNS, generateTrendData, MCC_HEATMAP, FUNNEL_DATA,
  MATRIX_DATA, CHANNEL_DATA, computeActivityMatrix,
};



// ── Sample customers + their model output ────────────────────────────────────
const CUSTOMERS = [
  {
    id: "c-001",
    name: "Иван Кузнецов",
    segment: "Премиум",
    age: 38,
    city: "Москва",
    ltv: "₽4.2М",
    tenure: "5 лет",
    avatar: "ИК",
  },
  {
    id: "c-002",
    name: "Анна Романова",
    segment: "Молодежь",
    age: 24,
    city: "Санкт-Петербург",
    ltv: "₽280К",
    tenure: "1.5 года",
    avatar: "АР",
  },
  {
    id: "c-003",
    name: "Виктор Соловьёв",
    segment: "Бизнес",
    age: 45,
    city: "Москва",
    ltv: "₽12.5М",
    tenure: "9 лет",
    avatar: "ВС",
  },
  {
    id: "c-004",
    name: "Елена Морозова",
    segment: "Средний класс",
    age: 58,
    city: "Казань",
    ltv: "₽680К",
    tenure: "12 лет",
    avatar: "ЕМ",
  },
  {
    id: "c-005",
    name: "Дмитрий Орлов",
    segment: "Массовый",
    age: 32,
    city: "Новосибирск",
    ltv: "₽420К",
    tenure: "3 года",
    avatar: "ДО",
  },
];

// ── Recommendations + SHAP values per customer ───────────────────────────────
// Each SHAP feature: name, value, shapValue (positive = pushes toward acceptance)
const RECOMMENDATIONS = {
  "c-001": {
    title: "Кэшбэк 7% на Аптеки",
    rationale: "Высокая регулярность покупок в категории + растущая частота за последний квартал",
    baseValue: 0.18,           // model baseline (avg acceptance prob)
    prediction: 0.73,          // predicted acceptance prob
    confidence: 0.91,           // model confidence
    expectedROI: 3.4,
    altRecs: ["Кэшбэк 5% на Рестораны", "Бонус 500₽ на отели"],
    shap: [
      { feature: "Покупки в аптеках за 90 дней",       value: "12 транзакций",    shap:  0.18, desc: "Выше среднего по сегменту в 3 раза" },
      { feature: "Сумма покупок в аптеках",            value: "₽42 800",          shap:  0.14, desc: "Топ-15% по сегменту" },
      { feature: "Открытие push-уведомлений (30д)",   value: "78%",              shap:  0.09, desc: "Высокая отзывчивость на канал" },
      { feature: "Возраст",                            value: "38 лет",           shap:  0.06, desc: "Целевой возраст для категории" },
      { feature: "Дней с последней покупки",           value: "3 дня",            shap:  0.05, desc: "Свежий клиент" },
      { feature: "LTV-кластер",                         value: "Premium",          shap:  0.04, desc: "Высокая ценность клиента" },
      { feature: "Использовал кэшбэк в прошлом",       value: "Да, 7 раз",        shap:  0.03, desc: "Engaged user" },
      { feature: "Геолокация (близость аптек)",        value: "< 500м",           shap:  0.02, desc: "В пешей доступности" },
      { feature: "Активность ночью (22–06)",           value: "18%",              shap: -0.01, desc: "Лёгкое расхождение с категорией" },
      { feature: "Последняя кампания принята",         value: "Нет",              shap: -0.05, desc: "Не открыл прошлое предложение" },
    ],
  },
  "c-002": {
    title: "Кэшбэк 10% на Рестораны выходного дня",
    rationale: "Активные покупки в категории по пятницам и субботам + молодой сегмент с высокой отзывчивостью",
    baseValue: 0.18,
    prediction: 0.81,
    confidence: 0.88,
    expectedROI: 4.2,
    altRecs: ["Кэшбэк 5% на Кофейни", "Бонус за привод друга"],
    shap: [
      { feature: "Транзакции в ресторанах по выходным", value: "8 за месяц",      shap:  0.22, desc: "Топ-5% по сегменту" },
      { feature: "Открытие push-уведомлений",          value: "92%",              shap:  0.16, desc: "Очень высокая отзывчивость" },
      { feature: "Возраст",                            value: "24 года",          shap:  0.09, desc: "Целевой сегмент молодёжи" },
      { feature: "Средний чек в категории",            value: "₽3 200",           shap:  0.07, desc: "Выше среднего" },
      { feature: "Использовал кэшбэк в прошлом",       value: "Да, 12 раз",       shap:  0.06, desc: "Highly engaged" },
      { feature: "Активность в приложении (30д)",       value: "26 заходов",       shap:  0.05, desc: "Регулярное использование" },
      { feature: "Геолокация (центр города)",          value: "Да",               shap:  0.02, desc: "Близко к ресторанам" },
      { feature: "Покупки в категории за 90 дней",     value: "₽38 400",          shap:  0.01, desc: "В рамках сегмента" },
      { feature: "Tenure (срок обслуживания)",          value: "1.5 года",         shap: -0.04, desc: "Меньше среднего" },
    ],
  },
  "c-003": {
    title: "Корпоративная карта с кэшбэк 4% на Электронику",
    rationale: "Регулярные крупные покупки техники + Бизнес-сегмент с предсказуемой потребностью",
    baseValue: 0.18,
    prediction: 0.64,
    confidence: 0.79,
    expectedROI: 5.1,
    altRecs: ["Кэшбэк 3% на Бизнес-услуги", "Lounge-доступ в аэропортах"],
    shap: [
      { feature: "Покупки электроники за 12 мес.",      value: "₽340К",            shap:  0.21, desc: "В 4 раза выше среднего" },
      { feature: "Сегмент Бизнес",                     value: "Активен",          shap:  0.12, desc: "Базовая характеристика" },
      { feature: "Tenure",                              value: "9 лет",            shap:  0.08, desc: "Очень лояльный клиент" },
      { feature: "LTV-кластер",                         value: "Top-1%",           shap:  0.07, desc: "Премиальный сегмент" },
      { feature: "Использовал корп. услуги",            value: "Да",               shap:  0.04, desc: "Готов к B2B-продуктам" },
      { feature: "Возраст",                            value: "45 лет",           shap:  0.02, desc: "Средний для сегмента" },
      { feature: "Открытие email-рассылок",            value: "32%",              shap: -0.04, desc: "Ниже среднего" },
      { feature: "Активность в приложении",             value: "8 заходов/мес",    shap: -0.06, desc: "Низкая по сегменту" },
    ],
  },
  "c-004": {
    title: "Кэшбэк 5% на Супермаркеты + Аптеки",
    rationale: "Стабильный профиль покупок в обеих категориях + долгая лояльность",
    baseValue: 0.18,
    prediction: 0.58,
    confidence: 0.84,
    expectedROI: 2.6,
    altRecs: ["Скидка на коммунальные платежи", "Бонус на годовщину"],
    shap: [
      { feature: "Tenure",                              value: "12 лет",           shap:  0.15, desc: "Высокая лояльность" },
      { feature: "Покупки в супермаркетах (90д)",      value: "₽58К",             shap:  0.11, desc: "Стабильный паттерн" },
      { feature: "Покупки в аптеках (90д)",            value: "₽18К",             shap:  0.09, desc: "Регулярные" },
      { feature: "Возраст",                            value: "58 лет",           shap:  0.07, desc: "Целевой для категорий" },
      { feature: "Использовал кэшбэк ранее",            value: "Да, 23 раза",      shap:  0.05, desc: "Habit formed" },
      { feature: "Открытие SMS",                       value: "84%",              shap:  0.04, desc: "Предпочитает SMS" },
      { feature: "Открытие push",                      value: "12%",              shap: -0.04, desc: "Push не работает" },
      { feature: "Использование онлайн-каналов",        value: "Редко",            shap: -0.07, desc: "Преимущественно офлайн" },
    ],
  },
  "c-005": {
    title: "Кэшбэк 3% на АЗС",
    rationale: "Регулярные заправки + расположение в городе с автомобильной культурой",
    baseValue: 0.18,
    prediction: 0.42,
    confidence: 0.72,
    expectedROI: 1.9,
    altRecs: ["Кэшбэк 5% на ТО и автозапчасти", "Дисконт на каршеринг"],
    shap: [
      { feature: "Транзакции на АЗС (90д)",            value: "24",               shap:  0.13, desc: "Регулярные заправки" },
      { feature: "Геолокация (Новосибирск)",            value: "Авто-город",        shap:  0.06, desc: "Высокий уровень автомобилизации" },
      { feature: "Возраст",                            value: "32 года",          shap:  0.04, desc: "Активный потребитель" },
      { feature: "Средний чек на АЗС",                 value: "₽2 800",           shap:  0.03, desc: "Средний" },
      { feature: "Использовал кэшбэк ранее",            value: "Да, 4 раза",       shap:  0.02, desc: "Знаком с механикой" },
      { feature: "Tenure",                              value: "3 года",           shap:  0.00, desc: "Средний" },
      { feature: "LTV-кластер",                         value: "Массовый",         shap: -0.02, desc: "Базовый сегмент" },
      { feature: "Открытие push (30д)",                value: "28%",              shap: -0.04, desc: "Низкая отзывчивость" },
    ],
  },
};

// ── Main page ────────────────────────────────────────────────────────────────

function Explanations({ recApiOnline, campaignsOnline }) {
  const [selectedId, setSelectedId] = useState("c-001");
  const [sortBy, setSortBy] = useState("impact"); // impact | direction
  const [userIdInput, setUserIdInput] = useState("");
  const [queriedId, setQueriedId] = useState(null);

  // Фаза 25: ростер клиентов из /ml/customers (кампании онлайн).
  const customersQ = useMlCustomers(!!campaignsOnline);
  const liveCustomers = customersQ.data;

  const liveQ = useLiveExplanation(queriedId, !!recApiOnline);
  const liveExplanation = liveQ.data?.explanation ?? null;

  // Ручной lookup по UUID → синтетический клиент, если его нет в ростере/моке.
  const inRoster = (id) => !!(liveCustomers && liveCustomers.some(c => c.id === id));
  const manualLive = (liveExplanation && queriedId
      && !inRoster(queriedId) && !CUSTOMERS.some(c => c.id === queriedId))
    ? {
        id: queriedId, name: `Клиент ${String(liveQ.data.userId).slice(0, 8)}…`,
        segment: "Live API", age: "—", city: "из feature store",
        ltv: "—", tenure: "—", avatar: "API", prediction: liveExplanation.prediction,
      }
    : null;

  // Список клиентов: live-ростер из API, иначе мок-набор; ручной lookup — сверху.
  const baseCustomers = (liveCustomers && liveCustomers.length) ? liveCustomers : CUSTOMERS;
  const customers = manualLive ? [manualLive, ...baseCustomers] : baseCustomers;

  // При загрузке live-ростера выбираем первого и тянем его SHAP.
  React.useEffect(() => {
    if (liveCustomers && liveCustomers.length) {
      setSelectedId(liveCustomers[0].id);
      setQueriedId(liveCustomers[0].id);
    }
  }, [liveCustomers]);

  function selectCustomer(id) {
    setSelectedId(id);
    // live-клиент (ростер) → запрос SHAP; мок-клиент использует RECOMMENDATIONS.
    if (inRoster(id)) setQueriedId(id);
  }

  const customer = customers.find(c => c.id === selectedId) ?? customers[0];
  const isLiveCustomer = inRoster(customer.id) || (manualLive && customer.id === manualLive.id);

  // rec: мок-клиент → RECOMMENDATIONS; live-клиент → liveExplanation (для него).
  const rec = isLiveCustomer
    ? (liveExplanation && liveQ.data?.userId === customer.id ? liveExplanation : null)
    : RECOMMENDATIONS[customer.id];

  const sortedShap = useMemo(() => {
    const arr = [...(rec?.shap ?? [])];
    if (sortBy === "impact") return arr.sort((a, b) => Math.abs(b.shap) - Math.abs(a.shap));
    if (sortBy === "direction") return arr.sort((a, b) => b.shap - a.shap);
    return arr;
  }, [rec, sortBy]);

  function handleLookup() {
    const id = userIdInput.trim();
    if (id) { setQueriedId(id); setSelectedId(id); }
  }

  return (
    <div style={{ display: "flex", gap: 20, maxWidth: 1400 }}>
      {/* Left: live lookup + customer selector */}
      <div style={{ width: 280, flexShrink: 0, display: "flex", flexDirection: "column", gap: 12 }}>
        {recApiOnline && (
          <div style={{ background: "white", borderRadius: 12, padding: 14, border: "1px solid #e8edf4" }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 8 }}>
              Live-запрос к ML API
            </div>
            <input
              value={userIdInput}
              onChange={e => setUserIdInput(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") handleLookup(); }}
              placeholder="UUID клиента из БД…"
              style={{
                width: "100%", boxSizing: "border-box", padding: "8px 10px",
                border: "1px solid #e2e8f0", borderRadius: 8, fontSize: 12,
                fontFamily: "'JetBrains Mono', monospace", outline: "none", color: "#0d1929",
              }}
            />
            <Button
              variant="primary" size="sm" style={{ width: "100%", marginTop: 8, justifyContent: "center" }}
              disabled={!userIdInput.trim() || liveQ.isFetching}
              onClick={handleLookup}
            >
              {liveQ.isFetching ? "Запрос…" : "Получить SHAP-объяснение"}
            </Button>
            {liveQ.isError && (
              <div style={{ marginTop: 8, fontSize: 11, color: "#dc2626" }}>
                Клиент не найден в feature store (или модель не загружена)
              </div>
            )}
            {liveExplanation?.modelVersion && (
              <div style={{ marginTop: 8, fontSize: 11, color: "#8896a8" }}>
                Модель: v{liveExplanation.modelVersion}
              </div>
            )}
          </div>
        )}
        <CustomerList customers={customers} selectedId={selectedId} onSelect={selectCustomer} recommendations={RECOMMENDATIONS} />
      </div>

      {/* Right: explanation viz */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
        {rec ? (
          <>
            <CustomerHeader customer={customer} rec={rec} />
            <ForcePlot rec={rec} />
            <WaterfallPlot shap={sortedShap} rec={rec} sortBy={sortBy} onSortChange={setSortBy} />
            <FeatureTable shap={sortedShap} />
            <AlternativeRecs alts={rec.altRecs} />
          </>
        ) : (
          <Card style={{ padding: 48, textAlign: "center", color: "#94a3b8" }}>
            {isLiveCustomer && liveQ.isError
              ? "Клиент не найден в feature store (или модель не загружена)"
              : isLiveCustomer && (liveQ.isFetching || !recApiOnline)
                ? (recApiOnline ? "Загрузка SHAP-объяснения…" : "ML API офлайн — SHAP недоступен")
                : "Выберите клиента"}
          </Card>
        )}
      </div>
    </div>
  );
}

// ── Customer List ─────────────────────────────────────────────────────────────
function CustomerList({ customers, selectedId, onSelect, recommendations }) {
  return (
    <div style={{
      width: 280, flexShrink: 0,
      background: "white", borderRadius: 12, padding: 14,
      border: "1px solid #e8edf4",
      height: "fit-content",
    }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 12, padding: "0 4px" }}>
        Клиенты с рекомендациями
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {customers.map(c => {
          const isSel = c.id === selectedId;
          // live-ростер несёт prediction на самом клиенте; мок — из RECOMMENDATIONS.
          const pred = c.prediction ?? recommendations[c.id]?.prediction ?? 0;
          return (
            <button key={c.id} onClick={() => onSelect(c.id)} style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "10px 12px", borderRadius: 10,
              border: isSel ? "2px solid oklch(0.55 0.18 230)" : "1px solid #f1f5f9",
              background: isSel ? "oklch(0.97 0.03 230)" : "white",
              cursor: "pointer", textAlign: "left", width: "100%",
              fontFamily: "inherit", transition: "all 0.12s",
            }}>
              <div style={{
                width: 36, height: 36, borderRadius: "50%",
                background: isSel ? "oklch(0.55 0.18 230)" : "#e2e8f0",
                color: isSel ? "white" : "#64748b",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 12, fontWeight: 700, flexShrink: 0,
              }}>{c.avatar}</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#0d1929", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.name}</div>
                <div style={{ fontSize: 11, color: "#8896a8" }}>{c.segment}</div>
              </div>
              <div style={{ fontSize: 12, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: predColor(pred) }}>
                {Math.round(pred*100)}%
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function predColor(p) {
  if (p >= 0.7) return "oklch(0.45 0.18 160)";
  if (p >= 0.5) return "oklch(0.55 0.18 230)";
  if (p >= 0.3) return "oklch(0.55 0.18 30)";
  return "#94a3b8";
}

// ── Customer Header — recommendation + customer info ────────────────────────
function CustomerHeader({ customer, rec }) {
  const predPct = Math.round(rec.prediction * 100);
  const confPct = Math.round(rec.confidence * 100);

  return (
    <Card style={{ padding: 0, overflow: "hidden" }}>
      <div style={{
        background: "linear-gradient(135deg, oklch(0.96 0.03 230), oklch(0.95 0.04 200))",
        padding: "20px 24px", borderBottom: "1px solid #e8edf4",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 4 }}>
          <div style={{
            width: 52, height: 52, borderRadius: "50%",
            background: "oklch(0.55 0.18 230)",
            display: "flex", alignItems: "center", justifyContent: "center",
            color: "white", fontSize: 17, fontWeight: 700, flexShrink: 0,
          }}>{customer.avatar}</div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 700, color: "#0d1929" }}>{customer.name}</div>
            <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
              {customer.segment} · {customer.age} лет · {customer.city} · LTV: {customer.ltv} · клиент {customer.tenure}
            </div>
          </div>
        </div>
      </div>

      <div style={{ padding: "20px 24px", display: "grid", gridTemplateColumns: "1fr auto auto auto", gap: 20, alignItems: "center" }}>
        <div>
          <div style={{ fontSize: 11, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 6 }}>
            Рекомендация модели
          </div>
          <div style={{ fontSize: 17, fontWeight: 700, color: "#0d1929", lineHeight: 1.3 }}>{rec.title}</div>
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 4, lineHeight: 1.4 }}>{rec.rationale}</div>
        </div>
        <Metric label="Вероятность принятия" value={predPct + "%"} color={predColor(rec.prediction)} />
        <Metric label="Уверенность модели" value={confPct + "%"} color="oklch(0.55 0.18 230)" />
        <Metric label="Ожидаемый ROI" value={rec.expectedROI != null ? rec.expectedROI.toFixed(1) + "×" : "—"} color="oklch(0.45 0.18 160)" />
      </div>
    </Card>
  );
}

function Metric({ label, value, color }) {
  return (
    <div style={{ textAlign: "center", minWidth: 110 }}>
      <div style={{ fontSize: 24, fontWeight: 700, color, fontFamily: "'JetBrains Mono', monospace", letterSpacing: "-0.5px" }}>{value}</div>
      <div style={{ fontSize: 10, fontWeight: 600, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px", marginTop: 2 }}>{label}</div>
    </div>
  );
}

// ── Force Plot — horizontal stacked bar from baseValue to prediction ────────
function ForcePlot({ rec }) {
  // Sum positive and negative SHAP contributions
  const positives = rec.shap.filter(s => s.shap > 0).sort((a,b) => b.shap - a.shap);
  const negatives = rec.shap.filter(s => s.shap < 0).sort((a,b) => a.shap - b.shap);
  const POS_COLOR = "oklch(0.55 0.18 30)";   // red-orange — pushing toward acceptance
  const NEG_COLOR = "oklch(0.55 0.18 240)";  // blue — pulling away

  // Layout: render a 0..1 horizontal axis showing baseline → prediction
  // with positive segments stacking right and negatives stacking left from middle baseline marker
  // We'll show a 600px scale where prediction is highlighted

  return (
    <Card style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 18 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>Force plot — вклад факторов</div>
          <div style={{ fontSize: 12, color: "#8896a8", marginTop: 2 }}>Как факторы сдвигают предсказание от среднего значения к итоговой вероятности</div>
        </div>
        <Legend />
      </div>

      {/* Axis */}
      <div style={{ position: "relative", padding: "8px 0 50px", marginTop: 8 }}>
        {/* Tick marks 0.0 - 1.0 */}
        <div style={{ position: "absolute", inset: "16px 0 30px", borderBottom: "1px solid #e2e8f0" }}>
          {[0, 0.2, 0.4, 0.6, 0.8, 1.0].map(t => (
            <div key={t} style={{
              position: "absolute", left: `${t * 100}%`, top: 0, bottom: 0,
              borderLeft: "1px dashed #f1f5f9",
            }}>
              <div style={{ position: "absolute", left: -16, bottom: -22, fontSize: 10, color: "#94a3b8", fontFamily: "'JetBrains Mono', monospace", width: 32, textAlign: "center" }}>{t.toFixed(1)}</div>
            </div>
          ))}
        </div>

        {/* The force bar */}
        <ForceBar
          baseValue={rec.baseValue}
          prediction={rec.prediction}
          positives={positives}
          negatives={negatives}
          posColor={POS_COLOR}
          negColor={NEG_COLOR}
        />
      </div>

      {/* Anchors */}
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginTop: 4 }}>
        <div>
          <span style={{ color: "#8896a8" }}>База модели (средняя по сегменту): </span>
          <strong style={{ color: "#0d1929", fontFamily: "'JetBrains Mono', monospace" }}>{(rec.baseValue * 100).toFixed(0)}%</strong>
        </div>
        <div>
          <span style={{ color: "#8896a8" }}>Итоговое предсказание: </span>
          <strong style={{ color: predColor(rec.prediction), fontFamily: "'JetBrains Mono', monospace" }}>{(rec.prediction * 100).toFixed(0)}%</strong>
        </div>
      </div>
    </Card>
  );
}

function ForceBar({ baseValue, prediction, positives, negatives, posColor, negColor }) {
  // Scale: 0 to 1 = 100%
  // Layout each contribution as a chevron/arrow segment
  // Negatives appear left of baseValue, positives push right toward prediction.

  // For visualization: positives accumulate from baseValue to prediction
  // Negatives accumulate from (baseValue + sum_pos) backward to prediction
  // Simpler approach: just stack positives right of baseValue, negatives left of baseValue
  // Then prediction = baseValue + sum_pos + sum_neg

  const HEIGHT = 36;
  const sumPos = positives.reduce((s, p) => s + p.shap, 0);
  const sumNeg = negatives.reduce((s, n) => s + n.shap, 0);

  return (
    <div style={{ position: "relative", height: HEIGHT, marginTop: 16 }}>
      {/* Baseline tick */}
      <div style={{
        position: "absolute", left: `${baseValue * 100}%`, top: -8, bottom: -8,
        width: 2, background: "#94a3b8",
      }}>
        <div style={{
          position: "absolute", top: -22, left: "50%", transform: "translateX(-50%)",
          fontSize: 10, fontWeight: 700, color: "#64748b", whiteSpace: "nowrap",
          background: "white", padding: "1px 6px", borderRadius: 8, border: "1px solid #e2e8f0",
        }}>База {(baseValue*100).toFixed(0)}%</div>
      </div>

      {/* Prediction tick */}
      <div style={{
        position: "absolute", left: `${prediction * 100}%`, top: -8, bottom: -8,
        width: 2, background: predColor(prediction),
      }}>
        <div style={{
          position: "absolute", bottom: -22, left: "50%", transform: "translateX(-50%)",
          fontSize: 10, fontWeight: 700, color: predColor(prediction), whiteSpace: "nowrap",
          background: "white", padding: "1px 6px", borderRadius: 8, border: `1px solid ${predColor(prediction)}`,
        }}>= {(prediction*100).toFixed(0)}%</div>
      </div>

      {/* Negatives — left of baseline (going backward) */}
      {(() => {
        const segments = [];
        let cursor = baseValue;
        negatives.forEach((n, i) => {
          const width = Math.abs(n.shap);
          const left = (cursor + n.shap) * 100;
          segments.push(
            <ChevronSegment
              key={"neg-"+i}
              left={left + "%"} width={(width * 100) + "%"} height={HEIGHT}
              color={negColor} direction="left"
              label={n.feature} delta={n.shap}
            />
          );
          cursor += n.shap;
        });
        return segments;
      })()}

      {/* Positives — right of baseline */}
      {(() => {
        const segments = [];
        let cursor = baseValue;
        positives.forEach((p, i) => {
          const width = p.shap;
          const left = cursor * 100;
          segments.push(
            <ChevronSegment
              key={"pos-"+i}
              left={left + "%"} width={(width * 100) + "%"} height={HEIGHT}
              color={posColor} direction="right"
              label={p.feature} delta={p.shap}
            />
          );
          cursor += p.shap;
        });
        return segments;
      })()}
    </div>
  );
}

function ChevronSegment({ left, width, height, color, direction, label, delta }) {
  const [hov, setHov] = React.useState(false);
  const isRight = direction === "right";
  const arrowSize = 8;
  return (
    <div
      onMouseEnter={() => setHov(true)}
      onMouseLeave={() => setHov(false)}
      style={{
        position: "absolute", left, top: 0,
        width, height,
        background: color,
        clipPath: isRight
          ? `polygon(0 0, calc(100% - ${arrowSize}px) 0, 100% 50%, calc(100% - ${arrowSize}px) 100%, 0 100%, ${arrowSize}px 50%)`
          : `polygon(${arrowSize}px 0, 100% 0, calc(100% - ${arrowSize}px) 50%, 100% 100%, ${arrowSize}px 100%, 0 50%)`,
        opacity: hov ? 1 : 0.85,
        transition: "opacity 0.15s",
        cursor: "help",
        borderRight: "1px solid rgba(255,255,255,0.4)",
      }}
    >
      {hov && (
        <div style={{
          position: "absolute", top: -56, left: "50%", transform: "translateX(-50%)",
          background: "#0d1929", color: "white",
          padding: "6px 10px", borderRadius: 6,
          fontSize: 11, whiteSpace: "nowrap", zIndex: 10,
          pointerEvents: "none",
        }}>
          <div style={{ fontWeight: 700, marginBottom: 2 }}>{label}</div>
          <div style={{ opacity: 0.85, fontFamily: "'JetBrains Mono', monospace" }}>
            {delta >= 0 ? "+" : ""}{(delta*100).toFixed(1)}%
          </div>
        </div>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div style={{ display: "flex", gap: 14, fontSize: 11 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
        <div style={{ width: 12, height: 8, background: "oklch(0.55 0.18 30)", borderRadius: 2 }} />
        <span style={{ color: "#64748b", fontWeight: 600 }}>Усиливает</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
        <div style={{ width: 12, height: 8, background: "oklch(0.55 0.18 240)", borderRadius: 2 }} />
        <span style={{ color: "#64748b", fontWeight: 600 }}>Ослабляет</span>
      </div>
    </div>
  );
}

// ── Waterfall — features ranked by impact, bars with values ─────────────────
function WaterfallPlot({ shap, rec, sortBy, onSortChange }) {
  const maxAbs = Math.max(...shap.map(s => Math.abs(s.shap)));
  const POS = "oklch(0.55 0.18 30)";
  const NEG = "oklch(0.55 0.18 240)";

  return (
    <Card style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>Waterfall — ранжирование признаков</div>
          <div style={{ fontSize: 12, color: "#8896a8", marginTop: 2 }}>Признаки отсортированы по {sortBy === "impact" ? "силе" : "направлению"} влияния</div>
        </div>
        <div style={{ display: "flex", gap: 4, background: "#f8fafc", borderRadius: 8, padding: 3, border: "1px solid #e2e8f0" }}>
          {[{ v: "impact", l: "По силе" }, { v: "direction", l: "По знаку" }].map(o => (
            <button key={o.v} onClick={() => onSortChange(o.v)} style={{
              padding: "5px 12px", borderRadius: 6, fontSize: 12, fontWeight: 600,
              border: "none", cursor: "pointer", fontFamily: "inherit",
              background: sortBy === o.v ? "oklch(0.55 0.18 230)" : "transparent",
              color:      sortBy === o.v ? "white" : "#64748b",
              transition: "all 0.12s",
            }}>{o.l}</button>
          ))}
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {shap.map((s, i) => {
          const pct = (Math.abs(s.shap) / maxAbs) * 100;
          const isPos = s.shap >= 0;
          return (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "240px 1fr 80px", gap: 12, alignItems: "center", padding: "6px 0" }}>
              <div>
                <div style={{ fontSize: 13, fontWeight: 600, color: "#0d1929", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {s.feature}
                </div>
                <div style={{ fontSize: 11, color: "#8896a8", fontFamily: "'JetBrains Mono', monospace" }}>{s.value}</div>
              </div>
              <div style={{ position: "relative", height: 24, display: "flex", alignItems: "center" }}>
                {/* center line */}
                <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "#cbd5e1" }} />
                {/* bar */}
                <div style={{
                  position: "absolute",
                  left: isPos ? "50%" : `calc(50% - ${pct/2}%)`,
                  width: `${pct/2}%`,
                  height: 18,
                  background: isPos ? POS : NEG,
                  borderRadius: isPos ? "0 4px 4px 0" : "4px 0 0 4px",
                  transition: "all 0.3s",
                }} />
              </div>
              <div style={{
                fontSize: 13, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace",
                color: isPos ? POS : NEG, textAlign: "right",
              }}>
                {isPos ? "+" : ""}{(s.shap * 100).toFixed(1)}%
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ── Feature Table — detailed view with descriptions ──────────────────────────
function FeatureTable({ shap }) {
  const [expanded, setExpanded] = useState(true);
  return (
    <Card style={{ padding: 0, overflow: "hidden" }}>
      <button onClick={() => setExpanded(e => !e)} style={{
        width: "100%", padding: "16px 24px",
        background: "white", border: "none", cursor: "pointer",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        borderBottom: expanded ? "1px solid #f1f5f9" : "none",
        fontFamily: "inherit", textAlign: "left",
      }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "#0d1929" }}>Детализация признаков</div>
          <div style={{ fontSize: 12, color: "#8896a8", marginTop: 2 }}>{shap.length} факторов с описаниями</div>
        </div>
        <span style={{ fontSize: 14, color: "#94a3b8", transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.2s" }}>▼</span>
      </button>
      {expanded && (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#fafbfc" }}>
              {["Признак", "Значение", "Влияние", "Интерпретация"].map(h => (
                <th key={h} style={{ textAlign: "left", padding: "10px 18px", fontSize: 11, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px", borderBottom: "1px solid #f1f5f9" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shap.map((s, i) => {
              const isPos = s.shap >= 0;
              return (
                <tr key={i} style={{ borderBottom: "1px solid #f8fafc" }}>
                  <td style={{ padding: "10px 18px", color: "#374151", fontWeight: 600 }}>{s.feature}</td>
                  <td style={{ padding: "10px 18px", color: "#64748b", fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }}>{s.value}</td>
                  <td style={{ padding: "10px 18px" }}>
                    <span style={{
                      display: "inline-block",
                      padding: "2px 9px", borderRadius: 99,
                      background: isPos ? "oklch(0.95 0.05 30)" : "oklch(0.95 0.05 240)",
                      color: isPos ? "oklch(0.45 0.18 30)" : "oklch(0.45 0.18 240)",
                      fontSize: 12, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace",
                    }}>
                      {isPos ? "+" : ""}{(s.shap * 100).toFixed(1)}%
                    </span>
                  </td>
                  <td style={{ padding: "10px 18px", color: "#64748b", fontSize: 12 }}>{s.desc}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Card>
  );
}

// ── Alternative Recommendations ─────────────────────────────────────────────
function AlternativeRecs({ alts }) {
  return (
    <Card style={{ padding: 24 }}>
      <div style={{ fontSize: 13, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 12 }}>
        Альтернативные рекомендации
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {alts.map((alt, i) => (
          <div key={i} style={{
            display: "flex", alignItems: "center", gap: 10,
            padding: "10px 14px", borderRadius: 8,
            background: "#fafbfc", border: "1px solid #f1f5f9",
          }}>
            <div style={{
              width: 22, height: 22, borderRadius: "50%",
              background: "white", border: "1px solid #e2e8f0",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 11, fontWeight: 700, color: "#94a3b8",
              flexShrink: 0,
            }}>{i + 2}</div>
            <span style={{ fontSize: 13, color: "#374151", flex: 1 }}>{alt}</span>
            <span style={{ fontSize: 11, color: "#94a3b8", fontWeight: 600 }}>ниже по score</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default Explanations;
