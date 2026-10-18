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
import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios';
import { toast } from 'sonner';

const RECOMMENDATION_BASE = (import.meta.env.VITE_RECOMMENDATION_API_URL as string) || '/api/recommendations';
const CAMPAIGN_BASE       = (import.meta.env.VITE_CAMPAIGN_API_URL as string)       || '/api/campaigns';
const MOBILE_BASE         = (import.meta.env.VITE_MOBILE_API_URL as string)         || '/api/mobile';

declare module 'axios' {
  interface AxiosRequestConfig {
    /** Без toast на ошибку: вызывающий показывает её сам (формы входа, фоновый SSE).
     *  Функция — без toast только на ошибки, которые она опознала (остальные — как обычно). */
    silent?: boolean | ((error: AxiosError) => boolean);
  }
}

// ── JWT (фаза 15, ротация — этап B) ─────────────────────────────────────────
import {
  clearTokens, getAccessToken, getRefreshToken, requirePasswordChange, setTokens,
} from './tokenStore';

/** detail 403-ответа, когда токен выдан до обязательной смены пароля. */
export const PASSWORD_CHANGE_REQUIRED = 'password change required';

/** Обмен учётных данных/refresh-токена: их 401 — ответ по существу, не «протух access». */
const CREDENTIAL_CALLS = ['/auth/login', '/auth/refresh', '/auth/logout'];

const REFRESH_LOCK = 'cashback.auth.refresh';

/**
 * Обмен refresh-токена — «голый» axios, без интерцепторов (иначе цикл).
 * Refresh-токен одноразовый: повторное предъявление сервер считает кражей и
 * отзывает всё семейство. Поэтому токен читается из хранилища только внутри
 * межвкладочного Web Lock — вкладки обменивают его строго по очереди, и
 * каждая берёт уже ротированный предыдущей.
 */
async function exchangeRefreshToken(): Promise<boolean> {
  const refresh = getRefreshToken();
  if (!refresh) {
    clearTokens();
    return false;
  }
  try {
    const { data } = await axios.post(`${CAMPAIGN_BASE}/auth/refresh`, {
      refresh_token: refresh,
    }, { timeout: 8000 });
    setTokens(data.access_token, data.refresh_token);
    return true;
  } catch (err) {
    const status = (err as AxiosError).response?.status;
    if (status !== undefined && status < 500) {
      // 401: токен уже обменян/отозван, пользователь отключён, сменилась
      // token_version — сессия мертва, подписчик в App покажет логин.
      clearTokens();
    } else {
      // 503 (хранилище сессий недоступно) / сеть: сессию не рвём — следующий
      // запрос попробует продлить её снова.
      toast.error('Campaign Manager: не удалось продлить сессию', {
        description: status ? `ошибка сервера (${status})` : 'соединение недоступно',
      });
    }
    return false;
  }
}

async function withRefreshLock(fn: () => Promise<boolean>): Promise<boolean> {
  // Web Locks есть только в secure context (https, localhost); иначе —
  // single-flight в пределах вкладки.
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  return locks ? await locks.request(REFRESH_LOCK, fn) : fn();
}

let refreshInFlight: Promise<boolean> | null = null;

/**
 * Single-flight продление сессии: параллельные 401 ждут один и тот же обмен,
 * а не шлют один refresh-токен несколько раз (это отозвало бы семейство).
 */
export function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = withRefreshLock(exchangeRefreshToken).finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

type RetriableConfig = InternalAxiosRequestConfig & { _retried?: boolean };

/** 401 смены пароля без вызова Bearer — неверный текущий пароль, а не протухший токен. */
function isFormRejection(error: AxiosError, url: string): boolean {
  return url.endsWith('/auth/change-password')
    && !String(error.response?.headers?.['www-authenticate'] ?? '').startsWith('Bearer');
}

function makeClient(baseURL: string, label: string, withAuth: boolean): AxiosInstance {
  const instance = axios.create({
    baseURL,
    timeout: 8000,
    headers: { 'Content-Type': 'application/json' },
  });

  // Админский access-токен — только campaign_manager: recommendation_api и
  // mobile_api его не принимают (разные секреты, этап B), незачем и светить.
  if (withAuth) {
    instance.interceptors.request.use((config) => {
      const token = getAccessToken();
      if (token && !config.headers.Authorization) {
        config.headers.Authorization = `Bearer ${token}`;
      }
      return config;
    });
  }

  instance.interceptors.response.use(
    (response) => response,
    async (error: AxiosError<{ detail?: unknown }>) => {
      const status = error.response?.status;
      const original = error.config as RetriableConfig | undefined;
      const url = String(original?.url ?? '');
      const detail = error.response?.data?.detail;

      // 401: единожды продлеваем сессию и повторяем запрос.
      if (withAuth && status === 401 && original && !original._retried
          && !CREDENTIAL_CALLS.some(p => url.endsWith(p)) && !isFormRejection(error, url)) {
        original._retried = true;
        const sentWith = original.headers?.Authorization;
        const current = getAccessToken();
        // Пока запрос летел, пару уже обновил параллельный 401 — просто повтор.
        const renewed = (current != null && sentWith !== `Bearer ${current}`)
          || await refreshSession();
        if (renewed && getAccessToken()) {
          original.headers.Authorization = `Bearer ${getAccessToken()}`;
          return instance.request(original);
        }
        return Promise.reject(error); // сессия завершена или не продлена — без тоста
      }

      // 403 «password change required» — не «нет прав», а экран смены пароля.
      if (withAuth && status === 403 && detail === PASSWORD_CHANGE_REQUIRED) {
        requirePasswordChange();
        return Promise.reject(error);
      }

      const silent = original?.silent;
      if (silent === true || (typeof silent === 'function' && silent(error))) {
        return Promise.reject(error);
      }

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
      } else if (status === 401) {
        toast.error(`${label}: требуется вход`, { description: reason });
      } else if (status === 403) {
        // Нет права на действие: сессия жива, просто сообщаем (не logout).
        // Один тост на пачку отказов (страница шлёт несколько запросов сразу).
        toast.warning('Недостаточно прав', { id: 'forbidden', description: `${label}: ${reason}` });
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

export const recommendationClient = makeClient(RECOMMENDATION_BASE, 'Recommendation API', false);
export const campaignClient       = makeClient(CAMPAIGN_BASE,       'Campaign Manager', true);
export const mobileClient         = makeClient(MOBILE_BASE,         'Mobile API', false);

/** HTTP-статус ошибки axios (undefined — сеть/таймаут). */
export function httpStatus(error: unknown): number | undefined {
  return (error as AxiosError | undefined)?.response?.status;
}

/** Convenience type — `T` if `data` is present, otherwise `null`. */
export type ApiResult<T> = T | null;

export const API_BASES = {
  recommendation: RECOMMENDATION_BASE,
  campaign:       CAMPAIGN_BASE,
  mobile:         MOBILE_BASE,
};
