/**
 * Auth API — обёртки над `/auth/*` campaign_manager (фаза 15, этап B).
 */
/* eslint-disable */
import { campaignClient, refreshSession } from '@/shared/api/client';
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from '@/shared/api/tokenStore';

export type AdminRole = 'ADMIN' | 'MARKETER' | 'ANALYST';

export interface AdminUser {
  user_id: string;
  email: string;
  full_name: string;
  role: AdminRole;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
  // Этап B: пароль выдан при установке/сброшен — до смены работает только
  // экран «Смена пароля».
  must_change_password?: boolean;
  // Фаза 26: живые права роли (для гейтов меню/страниц).
  permissions?: Record<string, boolean>;
}

/** Ответ /auth/login, /auth/refresh и /auth/change-password. */
interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  must_change_password?: boolean;
}

/** Матрица прав всех ролей (uppercase-ключи), фаза 26. */
export type RolePermissionsMatrix = Record<AdminRole, Record<string, boolean>>;

export const authApi = {
  /** Ошибки (401/429/сеть) показывает форма входа — без toast. */
  login: async (email: string, password: string): Promise<{ mustChangePassword: boolean }> => {
    const { data } = await campaignClient.post<TokenPair>(
      '/auth/login', { email, password }, { silent: true },
    );
    setTokens(data.access_token, data.refresh_token);
    return { mustChangePassword: !!data.must_change_password };
  },

  /**
   * Смена пароля (в т.ч. обязательная после bootstrap). Сервер гасит все
   * прежние токены и отдаёт новую пару — дальше работаем с ней.
   */
  changePassword: async (currentPassword: string, newPassword: string): Promise<void> => {
    const { data } = await campaignClient.post<TokenPair>(
      '/auth/change-password',
      { current_password: currentPassword, new_password: newPassword },
      { silent: true },
    );
    setTokens(data.access_token, data.refresh_token);
  },

  /**
   * Отзыв семейства refresh-токенов на сервере, затем локальная очистка.
   * Сеть/5xx не мешают выйти локально.
   */
  logout: async (): Promise<void> => {
    const refresh = getRefreshToken();
    try {
      if (refresh) {
        await campaignClient.post('/auth/logout', { refresh_token: refresh }, {
          silent: true, timeout: 3000,
        });
      }
    } catch { /* выходим локально в любом случае */ }
    finally {
      clearTokens();
    }
  },

  /**
   * Восстановление сессии после перезагрузки: access-токена в памяти нет —
   * сразу обмениваем refresh (без заведомого 401 на /auth/me).
   */
  restoreSession: async (): Promise<AdminUser | null> => {
    if (!getRefreshToken()) return null;
    if (!getAccessToken() && !(await refreshSession())) return null;
    return authApi.me();
  },

  me: async (): Promise<AdminUser> => {
    const { data } = await campaignClient.get<AdminUser>('/auth/me');
    return data;
  },

  listUsers: async (): Promise<AdminUser[]> => {
    const { data } = await campaignClient.get<AdminUser[]>('/auth/users');
    return data;
  },

  createUser: async (payload: {
    email: string; password: string; full_name: string; role: AdminRole;
  }): Promise<AdminUser> => {
    const { data } = await campaignClient.post<AdminUser>('/auth/users', payload);
    return data;
  },

  updateUser: async (
    userId: string,
    payload: Partial<{ full_name: string; role: AdminRole; is_active: boolean; password: string }>,
  ): Promise<AdminUser> => {
    const { data } = await campaignClient.patch<AdminUser>(`/auth/users/${userId}`, payload);
    return data;
  },
};

/** Матрица прав ролей (фаза 26) — GET/PATCH /roles/*. */
export const rolesApi = {
  getPermissions: async (): Promise<RolePermissionsMatrix> => {
    const { data } = await campaignClient.get<RolePermissionsMatrix>('/roles/permissions');
    return data;
  },
  /** Частичное обновление прав роли (ADMIN — 403). */
  updatePermissions: async (
    role: AdminRole, permissions: Record<string, boolean>,
  ): Promise<Record<string, boolean>> => {
    const { data } = await campaignClient.patch<Record<string, boolean>>(
      `/roles/${role}/permissions`, { permissions },
    );
    return data;
  },
};

/** ADMIN → admin и т.п. — роли UI написаны в нижнем регистре (mockData). */
export function toUiRole(role: AdminRole): 'admin' | 'marketer' | 'analyst' {
  return role.toLowerCase() as any;
}

export function initialsOf(fullName: string): string {
  return fullName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(w => w[0]!.toUpperCase())
    .join('');
}
