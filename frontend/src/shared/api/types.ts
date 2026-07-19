/**
 * Hand-written TypeScript types for the backend payloads.
 *
 * For full type safety run `bash scripts/generate-api-types.sh` to refresh
 * `src/shared/api/generated/*.ts` from each FastAPI service's
 * `/openapi.json`. The hand-written types below stay as a stable contract
 * the UI can rely on while a backend service is offline.
 */

// ── Campaign Manager ────────────────────────────────────────────────────────
export type CampaignStatus = 'DRAFT' | 'ACTIVE' | 'PAUSED' | 'COMPLETED';

export interface RateTier {
  min_amount: number;
  max_amount: number | null;
  rate: number;
}

export interface CampaignCreatePayload {
  name: string;
  target_segment_ids: number[];
  cashback_rate: string | number;
  min_transaction_amount?: string | number | null;
  budget_total: string | number;
  start_date: string;     // ISO 8601
  end_date: string;
  allowed_channels: string[];
  require_existing_behavior?: boolean;
  rate_tiers?: RateTier[] | null;
  mcc_codes: string[];
  // Поля визарда (фаза 22).
  daily_limit?: string | number | null;
  auto_pause?: boolean;
  rfm_min?: number | null;
  rfm_max?: number | null;
  min_tx_amounts?: Record<string, string | number> | null;
}

export interface Campaign {
  campaign_id: string;
  name: string;
  target_segment_ids: number[];
  cashback_rate: string;
  min_transaction_amount: string | null;
  budget_total: string;
  budget_spent: string;
  status: CampaignStatus;
  start_date: string;
  end_date: string;
  allowed_channels: string[];
  require_existing_behavior: boolean;
  rate_tiers: RateTier[] | null;
  mcc_codes: string[];
  // Поля визарда (фаза 22).
  daily_limit: string | null;
  auto_pause: boolean;
  rfm_min: number | null;
  rfm_max: number | null;
  created_by: string | null;
  min_tx_amounts?: Record<string, string>;
}

export interface CampaignSummary {
  campaign_id: string;
  name: string;
  status: CampaignStatus;
  budget_total: string;
  budget_spent: string;
  cashback_rate: string;
  start_date: string;
  end_date: string;
  mcc_codes: string[];
}

export interface StatusActionResponse {
  campaign_id: string;
  previous: CampaignStatus;
  current: CampaignStatus;
  action: 'activate' | 'pause' | 'complete';
}

export interface CampaignStats {
  campaign_id: string;
  impressions: number;
  clicks: number;
  accepted: number;
  transactions: number;
  revenue: string;
  cashback_paid: string;
  ctr: number;
  conversion_rate: number;
  roi: number;
}

export interface AudienceEstimate {
  estimated_users: number;
  segments: number[];
  rfm_filters: { rfmR: number | null; rfmF: number | null; rfmM: number | null };
}

export interface ApplicableCampaign {
  campaign_id: string;
  name: string;
  cashback_rate: number | string;
  remaining_budget: number | string;
  mcc_codes: string[];
}

// ── Analytics ───────────────────────────────────────────────────────────────
export interface FunnelStep {
  name: string;
  count: number;
  drop_off_pct: number;
  pending?: boolean;          // фаза 24: стадия «ждёт данных»
}

export interface FunnelResponse {
  campaign_id: string | null;
  period_days: number;
  steps: FunnelStep[];
}

// ── Aggregated KPIs (фаза 24) ───────────────────────────────────────────────
export interface KpiTrends {
  reach: number;
  spent: number;
  ctr: number;
}

export interface KpiResponse {
  campaigns_count: number;
  reach: number;
  spent: string;
  budget: string;
  avg_ctr: number;            // доля 0..1
  trends: KpiTrends;
  has_data: boolean;
}

export interface SegmentMatrixCell {
  segment_id: number;
  mcc_code: string;
  impressions: number;
  accepted: number;
  ctr: number;
}

export interface CohortRetentionCell {
  cohort_month: string;
  period: number;
  active_users: number;
  retention: number;
}

export interface TopCampaignItem {
  campaign_id: string;
  name: string;
  metric: number;
}

export interface DailyTrendPoint {
  date: string;               // YYYY-MM-DD
  segment_bucket: string;     // premium|mass|young|senior|business
  accepted: number;
}

export interface DailyTrendResponse {
  campaign_id: string | null;
  period_days: number;
  points: DailyTrendPoint[];
}

export interface ChannelStats {
  channel: 'PUSH' | 'SMS' | 'EMAIL' | 'IN_APP';
  sent: number;
  opened: number;
  converted: number;
  pending?: boolean;          // фаза 24: отправлено, откликов нет
}

// ── Reference dictionaries (фаза 21) ────────────────────────────────────────
export interface SegmentRef {
  id: string;
  name: string;
  count: number;
  deciles: number[];
}

export interface MccCategoryRef {
  code: string;
  name: string;
  icon: string;   // имя Lucide-иконки (не emoji)
}

// ── A/B testing ─────────────────────────────────────────────────────────────
export type ABStatus = 'DRAFT' | 'ACTIVE' | 'STOPPED';

export interface ABVariantPayload {
  name: string;
  traffic_weight: number;
  strategy_class: string;
  strategy_params?: Record<string, unknown> | null;
}

export interface ABVariant extends ABVariantPayload {
  variant_id: string;
}

export interface ABExperimentCreatePayload {
  name: string;
  target_metric: string;
  start_date: string;
  end_date?: string | null;
  variants: ABVariantPayload[];
}

export interface ABExperiment {
  experiment_id: string;
  name: string;
  status: ABStatus;
  target_metric: string;
  start_date: string;
  end_date: string | null;
  variants: ABVariant[];
}

export interface ABVariantStats {
  variant_id: string;
  name: string;
  n: number;
  successes: number;
  rate: number;
}

export interface ABResults {
  experiment_id: string;
  target_metric: string;
  control: ABVariantStats;
  treatment: ABVariantStats;
  diff: number;
  z: number | null;
  p_value: number | null;
  confidence_interval: [number, number] | null;
  significance: 'significant' | 'trending' | 'no_data';
}

// ── Recommendations / SHAP ──────────────────────────────────────────────────
export interface RecommendationItem {
  mcc_code: string;
  score: number;
  campaign_id: string | null;
  top_factors: Record<string, number>;
  feature_values?: Record<string, number>;
}

export interface RecommendationResponse {
  user_id: string;
  recommendations: RecommendationItem[];
  model_version: string | null;
  candidates_considered: number;
  serving_group?: string;
  // Расширенный контракт ML-объяснений (фаза 25).
  base_value?: number;
  confidence?: number;
  expected_roi?: number | null;
  rationale?: string;
  alt_recs?: string[];
  feature_interpretations?: Record<string, string>;
}

// ── ML-объяснения: ростер клиентов (фаза 25) ────────────────────────────────
export interface MlCustomer {
  customer_id: string;
  name: string;
  segment: string;
  prediction: number;
}
