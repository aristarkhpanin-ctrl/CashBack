/**
 * Auth API — обёртки над `/auth/*` campaign_manager (фаза 15).
 */
/* eslint-disable */
import { campaignClient } from '@/shared/api/client';
import { clearTokens, setTokens } from '@/shared/api/tokenStore';

export type AdminRole = 'ADMIN' | 'MARKETER' | 'ANALYST';

export interface AdminUser {
  user_id: string;
  email: string;
  full_name: string;
  role: AdminRole;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
  // Фаза 26: живые права роли (для гейтов меню/страниц).
  permissions?: Record<string, boolean>;
}

/** Матрица прав всех ролей (uppercase-ключи), фаза 26. */
export type RolePermissionsMatrix = Record<AdminRole, Record<string, boolean>>;

export const authApi = {
  login: async (email: string, password: string): Promise<void> => {
    const { data } = await campaignClient.post('/auth/login', { email, password });
    setTokens(data.access_token, data.refresh_token);
  },

  logout: (): void => {
    clearTokens();
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
