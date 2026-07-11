/**
 * Адаптеры backend ↔ UI для второго дизайна (Cashback2).
 *
 * Бэкенд оперирует UUID-кампаниями, децильными сегментами (int 1–10) и
 * Decimal-строками; UI — 5 именованными сегментами и числами. Все
 * преобразования собраны здесь, чтобы страницы не знали о wire-формате.
 */
/* eslint-disable */
import type {
  Campaign,
  CampaignCreatePayload,
  CampaignStats,
  FunnelResponse,
  RecommendationResponse,
  SegmentMatrixCell,
} from '@/shared/api/types';
import { MCC_CATEGORIES, SEGMENTS } from '@/data/mockData';

// ── Сегменты: децили 1–10 (БД) ↔ 5 именованных корзин (UI) ─────────────────
// Decile 10 — самые ценные клиенты. Группировка — витринная условность
// демо-стенда: сумма клиентов по корзинам покрывает все децили.
export const SEGMENT_BUCKETS: Record<string, number[]> = {
  premium:  [9, 10],
  mass:     [5, 6, 7, 8],
  young:    [3, 4],
  senior:   [2],
  business: [1],
};

const DECILE_TO_BUCKET: Record<number, string> = {};
for (const [bucket, ids] of Object.entries(SEGMENT_BUCKETS)) {
  for (const id of ids) DECILE_TO_BUCKET[id] = bucket;
}

export function bucketsFromSegmentIds(ids: number[]): string[] {
  const seen = new Set<string>();
  for (const id of ids || []) {
    const b = DECILE_TO_BUCKET[id];
    if (b) seen.add(b);
  }
  // порядок — как в mockData.SEGMENTS, чтобы чипы выглядели стабильно
  return SEGMENTS.map(s => s.id).filter(id => seen.has(id));
}

export function segmentIdsFromBuckets(buckets: string[]): number[] {
  const out: number[] = [];
  for (const b of buckets || []) out.push(...(SEGMENT_BUCKETS[b] ?? []));
  return [...new Set(out)].sort((a, b) => a - b);
}

// ── MCC-справочник (расширяет mockData категориями сидера) ──────────────────
const EXTRA_MCC: Record<string, string> = {
  '5732': 'Электроника',
  '5651': 'Одежда',
  '5411': 'Супермаркеты',
};

export function mccName(code: string): string {
  return (
    MCC_CATEGORIES.find(m => m.code === code)?.name ||
    EXTRA_MCC[code] ||
    `MCC ${code}`
  );
}

// ── Кампании ────────────────────────────────────────────────────────────────
const num = (v: string | number | null | undefined): number => {
  if (v == null) return 0;
  const n = typeof v === 'number' ? v : parseFloat(v);
  return Number.isFinite(n) ? n : 0;
};

/** Форма кампании, которую рендерят страницы (расширение MockCampaign). */
export interface UiCampaign {
  id: string | number;
  name: string;
  status: 'active' | 'paused' | 'completed' | 'draft';
  startDate: string;
  endDate: string;
  cashbackRate: number;
  budget: number;
  spent: number;
  dailyLimit: number;
  segments: string[];
  categories: string[];
  minTxAmount: number;
  reach: number;
  ctr: number;   // в процентах, как в mock (18.4 = 18.4%)
  roi: number;
  createdBy: number;
  /** UUID-кампания, пришедшая из API (не локальный мок). */
  live?: boolean;
  /** Сырые децили — нужны для round-trip при редактировании черновика. */
  targetSegmentIds?: number[];
  stats?: {
    accepted: number;
    transactions: number;
    cashbackPaid: number;
    revenue: number;
  };
}

export function campaignFromApi(c: Campaign, stats?: CampaignStats | null): UiCampaign {
  return {
    id: c.campaign_id,
    name: c.name,
    status: (c.status || 'DRAFT').toLowerCase() as UiCampaign['status'],
    startDate: (c.start_date || '').slice(0, 10),
    endDate: (c.end_date || '').slice(0, 10),
    cashbackRate: num(c.cashback_rate),
    budget: num(c.budget_total),
    spent: num(c.budget_spent),
    dailyLimit: 0, // дневной лимит не моделируется на бэкенде
    segments: bucketsFromSegmentIds(c.target_segment_ids),
    categories: c.mcc_codes || [],
    minTxAmount: num(c.min_transaction_amount),
    reach: stats?.impressions ?? 0,
    ctr: stats ? Math.round(stats.ctr * 1000) / 10 : 0,
    roi: stats ? Math.round(stats.roi * 10) / 10 : 0,
    createdBy: 0,
    live: true,
    targetSegmentIds: c.target_segment_ids,
    stats: stats
      ? {
          accepted: stats.accepted,
          transactions: stats.transactions,
          cashbackPaid: num(stats.cashback_paid),
          revenue: num(stats.revenue),
        }
      : undefined,
  };
}

/** Форма мастера кампаний (Campaigns.tsx wizard) → payload API. */
export function campaignToPayload(form: any): CampaignCreatePayload {
  const minTxValues = Object.values(form.minTxAmounts || {}).map(Number).filter(Number.isFinite);
  const minTx = minTxValues.length ? Math.min(...minTxValues) : (form.minTxAmount ?? null);
  return {
    name: form.name,
    target_segment_ids: segmentIdsFromBuckets(form.segments || []),
    cashback_rate: String(form.cashbackRate),
    min_transaction_amount: minTx != null ? String(minTx) : null,
    budget_total: String(form.budget),
    start_date: `${form.startDate}T00:00:00Z`,
    end_date: `${form.endDate}T23:59:59Z`,
    allowed_channels: ['ONLINE', 'POS', 'MOBILE'],
    require_existing_behavior: false,
    rate_tiers: null,
    mcc_codes: form.categories || [],
  };
}

// ── Воронка ─────────────────────────────────────────────────────────────────
const FUNNEL_STAGE_RU: Record<string, string> = {
  target_audience: 'Целевая аудитория',
  received: 'Получили предложение',
  opened: 'Открыли',
  accepted: 'Приняли',
  transacted: 'Совершили транзакцию',
  cashback_paid: 'Получили кэшбэк',
};

const FUNNEL_COLORS = [
  'oklch(0.55 0.18 230)',
  'oklch(0.55 0.18 220)',
  'oklch(0.55 0.18 210)',
  'oklch(0.55 0.18 200)',
  'oklch(0.55 0.18 190)',
  'oklch(0.60 0.18 160)',
];

export interface UiFunnelStage {
  stage: string;
  color: string;
  value: number;
  pending: boolean;
}

export function funnelFromApi(resp: FunnelResponse): UiFunnelStage[] {
  return (resp.steps || []).map((s, i) => ({
    stage: FUNNEL_STAGE_RU[s.name] || s.name,
    color: FUNNEL_COLORS[i] ?? FUNNEL_COLORS[FUNNEL_COLORS.length - 1],
    value: s.count,
    pending: false,
  }));
}

// ── Динамика принятых предложений (фаза 16) ─────────────────────────────────
/** Pivot точек API в строки Recharts: {date: 'DD.MM', premium: n, ...}. */
export function trendFromApi(resp: import('./types').DailyTrendResponse):
  Array<Record<string, string | number>> | null {
  const points = resp?.points || [];
  if (points.length === 0) return null;

  const byDate = new Map<string, Record<string, string | number>>();
  for (const p of points) {
    const [, m, d] = p.date.split('-');
    const label = `${d}.${m}`;
    if (!byDate.has(p.date)) {
      byDate.set(p.date, {
        date: label, premium: 0, mass: 0, young: 0, senior: 0, business: 0,
      });
    }
    (byDate.get(p.date) as any)[p.segment_bucket] = p.accepted;
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([, row]) => row);
}

// ── Каналы доставки (фаза 16) ───────────────────────────────────────────────
const CHANNEL_LABELS: Record<string, string> = {
  PUSH: 'Push', SMS: 'SMS', EMAIL: 'Email', IN_APP: 'App',
};

/** ChannelStats API → формат ChannelsView ({channel, sent, opened, converted}). */
export function channelsFromApi(stats: import('./types').ChannelStats[]):
  Array<{ channel: string; sent: number; opened: number; converted: number; pending: boolean }> | null {
  if (!stats || stats.length === 0) return null;
  const order = ['PUSH', 'SMS', 'EMAIL', 'IN_APP'];
  return [...stats]
    .sort((a, b) => order.indexOf(a.channel) - order.indexOf(b.channel))
    .map(s => ({
      channel: CHANNEL_LABELS[s.channel] ?? s.channel,
      sent: s.sent,
      opened: s.opened,
      converted: s.converted,
      pending: false,
    }));
}

// ── Матрица сегмент × категория ─────────────────────────────────────────────
export interface UiMatrix {
  segments: string[];
  categories: string[];
  values: number[][]; // values[segmentIdx][categoryIdx], проценты 0–99
  rows: Array<Record<string, string | number>>; // формат MccHeatmap
}

/**
 * Сворачивает децильные ячейки бэкенда в матрицу «5 корзин × категории»,
 * где значение — CTR (%) с весом по показам.
 */
export function matrixFromApi(cells: SegmentMatrixCell[]): UiMatrix | null {
  if (!cells || cells.length === 0) return null;

  const bucketNames = SEGMENTS.map(s => s.name);
  const bucketKeys = SEGMENTS.map(s => s.id);

  const mccCodes = [...new Set(cells.map(c => c.mcc_code))].sort();
  const categories = mccCodes.map(mccName);

  // (bucket, mcc) → {impressions, accepted}
  const agg = new Map<string, { imp: number; acc: number }>();
  for (const cell of cells) {
    const bucket = DECILE_TO_BUCKET[cell.segment_id];
    if (!bucket) continue;
    const key = `${bucket}|${cell.mcc_code}`;
    const cur = agg.get(key) ?? { imp: 0, acc: 0 };
    cur.imp += cell.impressions;
    cur.acc += cell.accepted;
    agg.set(key, cur);
  }

  const values = bucketKeys.map(bucket =>
    mccCodes.map(code => {
      const cur = agg.get(`${bucket}|${code}`);
      if (!cur || cur.imp === 0) return 0;
      return Math.max(0, Math.min(99, Math.round((cur.acc / cur.imp) * 100)));
    }),
  );

  const rows = mccCodes.map((code, ci) => {
    const row: Record<string, string | number> = { category: mccName(code) };
    bucketKeys.forEach((bucket, si) => { row[bucket] = values[si][ci]; });
    return row;
  });

  return { segments: bucketNames, categories, values, rows };
}

// ── SHAP-объяснения ─────────────────────────────────────────────────────────
const FEATURE_RU: Array<[RegExp, (m: RegExpMatchArray) => string]> = [
  [/^mcc_(\d{4})_cnt(?:_(\d+)d)?$/, m => `Число покупок: ${mccName(m[1])}${m[2] ? ` (${m[2]} дн.)` : ''}`],
  [/^mcc_(\d{4})_sum(?:_(\d+)d)?$/, m => `Сумма покупок: ${mccName(m[1])}${m[2] ? ` (${m[2]} дн.)` : ''}`],
  [/^mcc_(\d{4})_.*$/, m => `Активность: ${mccName(m[1])}`],
  [/^recency_days?$/, () => 'Дней с последней покупки'],
  [/^frequency(_total)?(_\d+d)?$/, () => 'Частота транзакций'],
  [/^monetary(_total)?(_\d+d)?$/, () => 'Суммарные траты'],
  [/^txn_cnt(_(\d+)d)?$/, m => `Число транзакций${m[2] ? ` (${m[2]} дн.)` : ''}`],
  [/^avg_(txn|check|amount).*$/, () => 'Средний чек'],
  [/^evening_ratio$/, () => 'Доля вечерних покупок'],
  [/^weekend_ratio$/, () => 'Доля покупок в выходные'],
  [/^online_ratio$/, () => 'Доля онлайн-покупок'],
  [/^segment(_id)?$/, () => 'Сегмент клиента'],
];

export function humanizeFeature(name: string): string {
  for (const [re, fmt] of FEATURE_RU) {
    const m = name.match(re);
    if (m) return fmt(m);
  }
  return name;
}

/** Форма, которую рендерит Explanations.tsx (RECOMMENDATIONS[id]). */
export interface UiExplanation {
  title: string;
  rationale: string;
  baseValue: number;
  prediction: number;
  confidence: number;
  expectedROI: number | null;
  altRecs: string[];
  shap: Array<{ feature: string; value: string; shap: number; desc: string }>;
  modelVersion?: string | null;
}

export function explanationFromApi(resp: RecommendationResponse): UiExplanation | null {
  const items = resp.recommendations || [];
  if (items.length === 0) return null;
  const top = items[0];

  const shap = Object.entries(top.top_factors || {})
    .sort(([, a], [, b]) => Math.abs(b) - Math.abs(a))
    .map(([feature, impact]) => ({
      feature: humanizeFeature(feature),
      value: '—', // сырые значения признаков не входят в ответ API
      shap: Math.round(impact * 1000) / 1000,
      desc: impact >= 0 ? 'повышает вероятность принятия' : 'снижает вероятность принятия',
    }));

  const prediction = Math.max(0.01, Math.min(0.99, top.score));
  const shapSum = shap.reduce((s, f) => s + f.shap, 0);
  const baseValue = Math.max(0.01, Math.min(0.99, prediction - shapSum));
  const margin = items.length > 1 ? top.score - items[1].score : 0.2;
  const confidence = Math.max(0.5, Math.min(0.99, 0.5 + margin * 2));

  return {
    title: `Кэшбэк на «${mccName(top.mcc_code)}»`,
    rationale: top.campaign_id
      ? 'Топ-рекомендация ранжирующей модели, привязана к активной кампании'
      : 'Топ-рекомендация ранжирующей модели (LightGBM, SHAP top-факторы)',
    baseValue,
    prediction,
    confidence,
    expectedROI: null,
    altRecs: items.slice(1, 4).map(
      it => `Кэшбэк на «${mccName(it.mcc_code)}» — score ${(it.score * 100).toFixed(0)}%`,
    ),
    shap,
    modelVersion: resp.model_version,
  };
}
