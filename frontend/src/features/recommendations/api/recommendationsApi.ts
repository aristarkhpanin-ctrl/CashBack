/**
 * Recommendation API — typed wrappers.
 */
import { recommendationClient } from '@/shared/api/client';
import type { RecommendationResponse } from '@/shared/api/types';

export const recommendationsApi = {
  forUser: async (userId: string, topK = 5): Promise<RecommendationResponse> => {
    const { data } = await recommendationClient.get<RecommendationResponse>(
      `/recommendations/${userId}`,
      { params: { top_k: topK } },
    );
    return data;
  },
};

export const recommendationKeys = {
  forUser: (userId: string, topK: number) =>
    ['recommendations', 'user', userId, topK] as const,
};
