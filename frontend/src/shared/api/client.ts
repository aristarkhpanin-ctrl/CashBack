/**
 * Axios HTTP clients for the three backend services.
 *
 * The base URLs come from Vite environment variables:
 *   - VITE_RECOMMENDATION_API_URL
 *   - VITE_CAMPAIGN_API_URL
 *   - VITE_MOBILE_API_URL
 *
 * Production layout: nginx proxies `/api/recommendations`, `/api/campaigns`
 * and `/api/mobile` to the corresponding FastAPI services, so all CORS
 * handling stays on the gateway.
 *
 * A single response interceptor surfaces upstream errors via Sonner toasts.
 */
import axios, { AxiosError, type AxiosInstance } from 'axios';
import { toast } from 'sonner';

const RECOMMENDATION_BASE = (import.meta.env.VITE_RECOMMENDATION_API_URL as string) || '/api/recommendations';
const CAMPAIGN_BASE       = (import.meta.env.VITE_CAMPAIGN_API_URL as string)       || '/api/campaigns';
const MOBILE_BASE         = (import.meta.env.VITE_MOBILE_API_URL as string)         || '/api/mobile';

function makeClient(baseURL: string, label: string): AxiosInstance {
  const instance = axios.create({
    baseURL,
    timeout: 8000,
    headers: { 'Content-Type': 'application/json' },
  });

  instance.interceptors.response.use(
    (response) => response,
    (error: AxiosError<{ detail?: unknown }>) => {
      const status = error.response?.status;
      const detail = error.response?.data?.detail;
      const reason =
        typeof detail === 'string'
          ? detail
          : (detail && typeof detail === 'object' && 'reason' in detail
              ? String((detail as { reason?: unknown }).reason)
              : error.message);

      if (status === 0 || status === undefined) {
        toast.error(`${label}: соединение недоступно`);
      } else if (status >= 500) {
        toast.error(`${label}: ошибка сервера (${status})`);
      } else if (status === 404) {
        // 404s are often expected (resource not seeded yet) — a soft notice.
        toast.message(`${label}: данные не найдены`, { description: reason });
      } else if (status === 409) {
        toast.warning(`${label}: конфликт`, { description: reason });
      } else if (status === 422) {
        toast.error(`${label}: невалидные данные`, { description: reason });
      } else {
        toast.error(`${label}: ${status}`, { description: reason });
      }
      return Promise.reject(error);
    },
  );

  return instance;
}

export const recommendationClient = makeClient(RECOMMENDATION_BASE, 'Recommendation API');
export const campaignClient       = makeClient(CAMPAIGN_BASE,       'Campaign Manager');
export const mobileClient         = makeClient(MOBILE_BASE,         'Mobile API');

/** Convenience type — `T` if `data` is present, otherwise `null`. */
export type ApiResult<T> = T | null;

export const API_BASES = {
  recommendation: RECOMMENDATION_BASE,
  campaign:       CAMPAIGN_BASE,
  mobile:         MOBILE_BASE,
};
