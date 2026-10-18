/**
 * Campaign Manager — typed wrappers around `/campaigns/*`.
 */
import { campaignClient } from '@/shared/api/client';
import type {
  AudienceEstimate, Campaign, CampaignCreatePayload, CampaignStats,
  CampaignSummary, StatusActionResponse, ApplicableCampaign,
} from '@/shared/api/types';

export const campaignApi = {
  list: async (): Promise<CampaignSummary[]> => {
    const { data } = await campaignClient.get<CampaignSummary[]>('/campaigns/active');
    return data;
  },

  /** Full list for the admin UI — any status, newest first; archived
   *  campaigns (B24) only with `includeArchived`. */
  listAll: async (status?: string, limit = 200, includeArchived = false): Promise<Campaign[]> => {
    const { data } = await campaignClient.get<Campaign[]>('/campaigns', {
      params: {
        ...(status ? { status } : {}),
        limit,
        ...(includeArchived ? { include_archived: true } : {}),
      },
    });
    return data;
  },

  /** Field edit — backend allows this only while the campaign is DRAFT. */
  update: async (id: string, payload: Partial<CampaignCreatePayload>): Promise<Campaign> => {
    const { data } = await campaignClient.patch<Campaign>(`/campaigns/${id}`, payload);
    return data;
  },

  create: async (payload: CampaignCreatePayload): Promise<Campaign> => {
    const { data } = await campaignClient.post<Campaign>('/campaigns', payload);
    return data;
  },

  /** Удаление — только черновик без истории (право campaigns_delete);
   *  кампанию с историей бэкенд отклонит 409 (B24). */
  remove: async (id: string): Promise<void> => {
    await campaignClient.delete(`/campaigns/${id}`);
  },

  /** Архив PAUSED/COMPLETED-кампании (B24): история остаётся, кампания
   *  скрыта из списков и больше никому не положена. */
  archive: async (id: string): Promise<Campaign> => {
    const { data } = await campaignClient.post<Campaign>(`/campaigns/${id}/archive`);
    return data;
  },

  get: async (id: string): Promise<Campaign> => {
    const { data } = await campaignClient.get<Campaign>(`/campaigns/${id}`);
    return data;
  },

  updateStatus: async (
    id: string,
    action: 'activate' | 'pause' | 'complete',
  ): Promise<StatusActionResponse> => {
    const { data } = await campaignClient.patch<StatusActionResponse>(
      `/campaigns/${id}/status`,
      null,
      { params: { action } },
    );
    return data;
  },

  estimateAudience: async (filters: {
    segment_ids: number[];
    rfmR?: number | null;
    rfmF?: number | null;
    rfmM?: number | null;
  }): Promise<AudienceEstimate> => {
    const params = new URLSearchParams();
    for (const id of filters.segment_ids) params.append('segment_ids', String(id));
    if (filters.rfmR != null) params.set('rfmR', String(filters.rfmR));
    if (filters.rfmF != null) params.set('rfmF', String(filters.rfmF));
    if (filters.rfmM != null) params.set('rfmM', String(filters.rfmM));
    const { data } = await campaignClient.get<AudienceEstimate>(
      '/campaigns/audience/estimate',
      { params },
    );
    return data;
  },

  getStats: async (id: string): Promise<CampaignStats> => {
    const { data } = await campaignClient.get<CampaignStats>(`/campaigns/${id}/stats`);
    return data;
  },

  applicable: async (userId: string, limit = 20): Promise<ApplicableCampaign[]> => {
    const { data } = await campaignClient.get<ApplicableCampaign[]>(
      `/campaigns/applicable/${userId}`,
      { params: { limit } },
    );
    return data;
  },
};

export const campaignKeys = {
  all:        ['campaigns'] as const,
  list:       (includeArchived: boolean) => ['campaigns', 'list', { includeArchived }] as const,
  active:     ['campaigns', 'active'] as const,
  detail:     (id: string)    => ['campaigns', 'detail', id] as const,
  stats:      (id: string)    => ['campaigns', 'stats', id]  as const,
  audience:   (
    segments: number[], rfmR?: number | null, rfmF?: number | null, rfmM?: number | null,
  ) => ['campaigns', 'audience', segments.join(','), rfmR, rfmF, rfmM] as const,
};
