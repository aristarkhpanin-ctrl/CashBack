/**
 * Хранилище JWT-токенов (фаза 15).
 *
 * access — только в памяти (исчезает при перезагрузке, XSS-поверхность
 * минимальна); refresh — в localStorage: демо-стенд без httpOnly-cookie
 * сессий, компромисс зафиксирован в README. Refresh-токен одноразовый
 * (ротация, этап B): после каждого /auth/refresh здесь лежит новый.
 */
/* eslint-disable */

const REFRESH_KEY = "cashback.refresh_token";

let accessToken: string | null = null;
const listeners = new Set<() => void>();
const passwordChangeListeners = new Set<() => void>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function getRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

export function setTokens(access: string, refresh?: string | null): void {
  accessToken = access;
  if (refresh) {
    try { localStorage.setItem(REFRESH_KEY, refresh); } catch { /* private mode */ }
  }
  listeners.forEach(fn => fn());
}

export function clearTokens(): void {
  accessToken = null;
  try { localStorage.removeItem(REFRESH_KEY); } catch { /* ignore */ }
  listeners.forEach(fn => fn());
}

export function isAuthenticated(): boolean {
  return accessToken != null;
}

/** Подписка App на «сессия завершена» (logout / refresh не удался). */
export function onAuthChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * Сервер ответил 403 «password change required»: токен годится только для
 * /auth/me и /auth/change-password — App показывает экран смены пароля.
 */
export function requirePasswordChange(): void {
  passwordChangeListeners.forEach(fn => fn());
}

export function onPasswordChangeRequired(fn: () => void): () => void {
  passwordChangeListeners.add(fn);
  return () => passwordChangeListeners.delete(fn);
}
