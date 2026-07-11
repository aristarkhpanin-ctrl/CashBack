/**
 * React-query хуки live-режима.
 *
 * Паттерн: тихий health-пинг (без toast-интерцептора) решает, включать ли
 * запросы к бэкенду. Пока API офлайн, страницы работают на mock-данных —
 * стенд остаётся демонстрируемым без docker-стека.
 */
/* eslint-disable */
import axios from 'axios';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { campaignApi, campaignKeys } from '@/features/campaigns/api/campaignApi';
import { analyticsApi } from '@/features/analytics/api/analyticsApi';
import { authApi, initialsOf, toUiRole, type AdminUser } from '@/features/auth/api/authApi';
import { recommendationClient } from '@/shared/api/client';
import type {
  Campaign,
  CampaignStats,
  FunnelResponse,
  RecommendationResponse,
  SegmentMatrixCell,
} from '@/shared/api/types';
import {
  campaignFromApi,
  campaignToPayload,
  explanationFromApi,
  funnelFromApi,
  matrixFromApi,
  type UiCampaign,
  type UiExplanation,
  type UiFunnelStage,
  type UiMatrix,
} from '@/shared/api/adapters';

// ── Health (без интерцепторов — не спамит тостами в демо-режиме) ────────────
const quiet = axios.create({ timeout: 3000 });

async function ping(url: string): Promise<boolean> {
  try {
    await quiet.get(url);
    return true;
  } catch {
    return false;
  }
}

export interface ApiHealth {
  campaigns: boolean;
  recommendations: boolean;
  checked: boolean;
}

export function useApiHealth(): ApiHealth {
  const q = useQuery({
    queryKey: ['api-health'],
    queryFn: async () => ({
      campaigns: await ping('/api/campaigns/health/live'),
      recommendations: await ping('/api/recommendations/health/live'),
    }),
    refetchInterval: 30_000,
    retry: false,
    staleTime: 25_000,
  });
  return {
    campaigns: q.data?.campaigns ?? false,
    recommendations: q.data?.recommendations ?? false,
    checked: q.isFetched,
  };
}

// ── Кампании: список + статистика ───────────────────────────────────────────
async function fetchCampaignsWithStats(): Promise<UiCampaign[]> {
  const list = await campaignApi.listAll();
  const stats = await Promise.allSettled(
    list.map((c: Campaign) => campaignApi.getStats(c.campaign_id)),
  );
  return list.map((c: Campaign, i: number) => {
    const s = stats[i];
    return campaignFromApi(c, s.status === 'fulfilled' ? (s.value as CampaignStats) : null);
  });
}

export function useLiveCampaigns(enabled: boolean) {
  return useQuery({
    queryKey: campaignKeys.all,
    queryFn: fetchCampaignsWithStats,
    enabled,
    retry: 1,
    refetchInterval: 60_000,
  });
}

/** Мутации кампаний: create / update(draft) / смена статуса через FSM. */
export function useCampaignOps() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: campaignKeys.all });

  const create = useMutation({
    mutationFn: (form: any) => campaignApi.create(campaignToPayload(form)),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: ({ id, form }: { id: string; form: any }) =>
      campaignApi.update(id, campaignToPayload(form)),
    onSuccess: invalidate,
  });
  const setStatus = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'activate' | 'pause' | 'complete' }) =>
      campaignApi.updateStatus(id, action),
    onSuccess: invalidate,
  });
  return { create, update, setStatus, invalidate };
}

/** UI-статус → FSM-действие бэкенда. */
export function statusToAction(newStatus: string): 'activate' | 'pause' | 'complete' | null {
  if (newStatus === 'active') return 'activate';
  if (newStatus === 'paused') return 'pause';
  if (newStatus === 'completed') return 'complete';
  return null;
}

// ── Аналитика ───────────────────────────────────────────────────────────────
export function useLiveFunnel(
  campaignId: string | null,
  periodDays: number,
  enabled: boolean,
) {
  return useQuery<UiFunnelStage[] | null>({
    queryKey: ['analytics', 'funnel', campaignId ?? 'all', periodDays],
    queryFn: async () => {
      const resp: FunnelResponse = await analyticsApi.getFunnel(campaignId, periodDays);
      return funnelFromApi(resp);
    },
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

export function useLiveMatrix(periodDays: number, enabled: boolean) {
  return useQuery<UiMatrix | null>({
    queryKey: ['analytics', 'segment-matrix', periodDays],
    queryFn: async () => {
      const cells: SegmentMatrixCell[] = await analyticsApi.getSegmentMatrix(periodDays);
      return matrixFromApi(cells);
    },
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

// ── Пользователи админ-панели (фаза 15) ─────────────────────────────────────
export interface UiAdminUser {
  id: string;
  name: string;
  email: string;
  role: 'admin' | 'marketer' | 'analyst';
  avatar: string;
  lastLogin: string;
  isActive: boolean;
}

function adminUserToUi(u: AdminUser): UiAdminUser {
  return {
    id: u.user_id,
    name: u.full_name,
    email: u.email,
    role: toUiRole(u.role),
    avatar: initialsOf(u.full_name),
    lastLogin: u.last_login_at
      ? new Date(u.last_login_at).toLocaleString('ru', {
          day: '2-digit', month: '2-digit', year: 'numeric',
          hour: '2-digit', minute: '2-digit',
        })
      : 'Ещё не входил',
    isActive: u.is_active,
  };
}

export function useAdminUsers(enabled: boolean) {
  const qc = useQueryClient();
  const query = useQuery<UiAdminUser[]>({
    queryKey: ['admin-users'],
    queryFn: async () => (await authApi.listUsers()).map(adminUserToUi),
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
  const invalidate = () => qc.invalidateQueries({ queryKey: ['admin-users'] });

  const create = useMutation({
    mutationFn: (p: { email: string; password: string; full_name: string; role: string }) =>
      authApi.createUser(p as any),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Record<string, unknown> }) =>
      authApi.updateUser(id, patch as any),
    onSuccess: invalidate,
  });
  return { query, create, update };
}

// ── SHAP-объяснение по клиенту ──────────────────────────────────────────────
export function useLiveExplanation(userId: string | null, enabled: boolean) {
  return useQuery<{ explanation: UiExplanation | null; userId: string }>({
    queryKey: ['explanation', userId],
    queryFn: async () => {
      const { data } = await recommendationClient.get<RecommendationResponse>(
        `/recommendations/${userId}`,
        { params: { top_k: 5 } },
      );
      return { explanation: explanationFromApi(data), userId: userId as string };
    },
    enabled: enabled && !!userId,
    retry: 0,
    staleTime: 60_000,
  });
}
