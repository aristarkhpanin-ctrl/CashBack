/**
 * ML-объяснения — typed wrappers.
 *
 * Админка не ходит в recommendation_api напрямую: его serving-эндпоинт
 * мутирующий (пишет показ, событие в Kafka, anti-fatigue) и принимает только
 * сервисный токен mobile_api. Объяснение без побочных эффектов отдаёт
 * campaign_manager `GET /ml/explain/{user_id}` (право `analytics`), проксируя
 * read-only `/recommendations/{user_id}/explain` (B27).
 */
import { campaignClient } from '@/shared/api/client';
import type { RecommendationResponse } from '@/shared/api/types';

export const recommendationsApi = {
  explain: async (userId: string, topK = 5): Promise<RecommendationResponse> => {
    const { data } = await campaignClient.get<RecommendationResponse>(
      `/ml/explain/${encodeURIComponent(userId)}`,
      { params: { top_k: topK } },
    );
    return data;
  },
};

export const recommendationKeys = {
  explain: (userId: string, topK: number) =>
    ['recommendations', 'explain', userId, topK] as const,
};
