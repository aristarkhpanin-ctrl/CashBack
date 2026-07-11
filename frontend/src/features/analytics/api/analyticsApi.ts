/**
 * Analytics — typed wrappers around `/analytics/*`.
 *
 * Note: there is no dedicated `mcc-heatmap` endpoint on the backend.
 * The dashboard heatmap is built from `/analytics/segment-matrix`,
 * which returns the same data in a more flexible shape.
 */
import { campaignClient } from '@/shared/api/client';
import type {
  ChannelStats, CohortRetentionCell, DailyTrendResponse, FunnelResponse,
  SegmentMatrixCell, TopCampaignItem,
} from '@/shared/api/types';

export type TopMetric = 'roi' | 'ctr' | 'conversion_rate' | 'cashback_paid';

export const analyticsApi = {
  getFunnel: async (
    campaignId?: string | null,
    period = 30,
  ): Promise<FunnelResponse> => {
    const { data } = await campaignClient.get<FunnelResponse>('/analytics/funnel', {
      params: campaignId ? { campaign_id: campaignId, period } : { period },
    });
    return data;
  },

  /** Принятые предложения по дням и сегментным корзинам (фаза 16). */
  getDailyTrend: async (
    campaignId?: string | null,
    period = 30,
  ): Promise<DailyTrendResponse> => {
    const { data } = await campaignClient.get<DailyTrendResponse>('/analytics/daily-trend', {
      params: campaignId ? { campaign_id: campaignId, period } : { period },
    });
    return data;
  },

  /** Эффективность каналов доставки (фаза 16). */
  getChannels: async (
    campaignId?: string | null,
    period = 30,
  ): Promise<ChannelStats[]> => {
    const { data } = await campaignClient.get<ChannelStats[]>('/analytics/channels', {
      params: campaignId ? { campaign_id: campaignId, period } : { period },
    });
    return data;
  },

  getSegmentMatrix: async (period = 30): Promise<SegmentMatrixCell[]> => {
    const { data } = await campaignClient.get<SegmentMatrixCell[]>(
      '/analytics/segment-matrix',
      { params: { period } },
    );
    return data;
  },

  /** Alias kept for the dashboard heatmap (uses segment-matrix under the hood). */
  getMccHeatmap: async (period = 30): Promise<SegmentMatrixCell[]> => {
    return analyticsApi.getSegmentMatrix(period);
  },

  getCohortRetention: async (
    cohortMonth: string,
    periods = 6,
  ): Promise<CohortRetentionCell[]> => {
    const { data } = await campaignClient.get<CohortRetentionCell[]>(
      '/analytics/cohort-retention',
      { params: { cohort_month: cohortMonth, periods } },
    );
    return data;
  },

  getTopCampaigns: async (
    metric: TopMetric = 'roi',
    limit = 5,
  ): Promise<TopCampaignItem[]> => {
    const { data } = await campaignClient.get<TopCampaignItem[]>(
      '/analytics/top-campaigns',
      { params: { metric, limit } },
    );
    return data;
  },
};

export const analyticsKeys = {
  funnel:        (cid: string | null, period: number)   => ['analytics', 'funnel', cid, period] as const,
  matrix:        (period: number)                       => ['analytics', 'matrix', period] as const,
  heatmap:       (period: number)                       => ['analytics', 'heatmap', period] as const,
  cohort:        (month: string, periods: number)       => ['analytics', 'cohort', month, periods] as const,
  topCampaigns:  (metric: TopMetric, limit: number)     => ['analytics', 'top', metric, limit] as const,
};
