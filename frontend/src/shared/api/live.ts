/**
 * React-query хуки live-режима.
 *
 * Паттерн: тихий health-пинг (без toast-интерцептора) решает, включать ли
 * запросы к бэкенду. Пока API офлайн, страницы работают на mock-данных —
 * стенд остаётся демонстрируемым без docker-стека.
 */
/* eslint-disable */
import axios, { type AxiosError } from 'axios';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { campaignApi, campaignKeys } from '@/features/campaigns/api/campaignApi';
import { analyticsApi } from '@/features/analytics/api/analyticsApi';
import { authApi, initialsOf, rolesApi, toUiRole, type AdminUser } from '@/features/auth/api/authApi';
import { abApi, abKeys } from '@/features/ab-testing/api/abApi';
import { recommendationsApi } from '@/features/recommendations/api/recommendationsApi';
import { campaignClient, httpStatus } from '@/shared/api/client';
import type {
  Campaign,
  CampaignStats,
  FunnelResponse,
  KpiResponse,
  MccCategoryRef,
  MlCustomer,
  SegmentMatrixCell,
  SegmentRef,
} from '@/shared/api/types';
import {
  campaignFromApi,
  campaignToPayload,
  channelsFromApi,
  explanationFromApi,
  funnelFromApi,
  kpiFromApi,
  matrixFromApi,
  mccFromApi,
  mlCustomersFromApi,
  segmentsFromApi,
  trendFromApi,
  type UiCampaign,
  type UiExplanation,
  type UiFunnelStage,
  type UiKpis,
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
  checked: boolean;
}

// Админка работает только с campaign_manager (ML-объяснения — через его
// /ml/explain), поэтому и live-режим решает его health.
export function useApiHealth(): ApiHealth {
  const q = useQuery({
    queryKey: ['api-health'],
    queryFn: async () => ({
      campaigns: await ping('/api/campaigns/health/live'),
    }),
    refetchInterval: 30_000,
    retry: false,
    staleTime: 25_000,
  });
  return {
    campaigns: q.data?.campaigns ?? false,
    checked: q.isFetched,
  };
}

// ── Кампании: список + статистика ───────────────────────────────────────────
async function fetchCampaignsWithStats(includeArchived: boolean): Promise<UiCampaign[]> {
  const list = await campaignApi.listAll(undefined, 200, includeArchived);
  const stats = await Promise.allSettled(
    list.map((c: Campaign) => campaignApi.getStats(c.campaign_id)),
  );
  return list.map((c: Campaign, i: number) => {
    const s = stats[i];
    return campaignFromApi(c, s.status === 'fulfilled' ? (s.value as CampaignStats) : null);
  });
}

/** Список кампаний; архивные (B24) — только с `includeArchived` («Показать архив»). */
export function useLiveCampaigns(enabled: boolean, includeArchived = false) {
  return useQuery({
    queryKey: campaignKeys.list(includeArchived),
    queryFn: () => fetchCampaignsWithStats(includeArchived),
    enabled,
    retry: 1,
    refetchInterval: 60_000,
    // Тумблер архива меняет ключ: без прошлых данных на время запроса список
    // пропал бы, а UI мигнул бы в демо-режим.
    placeholderData: keepPreviousData,
  });
}

// ── Справочники (фаза 21): сегменты и MCC из единого источника ───────────────
export function useReference(enabled: boolean) {
  const segmentsQ = useQuery({
    queryKey: ['reference', 'segments'],
    queryFn: async () => {
      const { data } = await campaignClient.get<SegmentRef[]>('/reference/segments');
      return segmentsFromApi(data);
    },
    enabled,
    retry: 1,
    staleTime: 60_000,
  });
  const mccQ = useQuery({
    queryKey: ['reference', 'mcc-categories'],
    queryFn: async () => {
      const { data } = await campaignClient.get<MccCategoryRef[]>('/reference/mcc-categories');
      return mccFromApi(data);
    },
    enabled,
    retry: 1,
    staleTime: 3_600_000, // справочник MCC статичен — час
  });
  return {
    segments: segmentsQ.data ?? null,
    mccCategories: mccQ.data ?? null,
  };
}

/** Мутации кампаний: create / update(draft) / смена статуса через FSM /
 *  удаление черновика / архив. */
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
  const remove = useMutation({
    mutationFn: (id: string) => campaignApi.remove(id),
    onSuccess: invalidate,
  });
  const archive = useMutation({
    mutationFn: (id: string) => campaignApi.archive(id),
    onSuccess: invalidate,
  });
  return { create, update, setStatus, remove, archive, invalidate };
}

/** UI-статус → FSM-действие бэкенда. */
export function statusToAction(newStatus: string): 'activate' | 'pause' | 'complete' | null {
  if (newStatus === 'active') return 'activate';
  if (newStatus === 'paused') return 'pause';
  if (newStatus === 'completed') return 'complete';
  return null;
}

// ── Аналитика ───────────────────────────────────────────────────────────────
// segmentId — витринная корзина (premium/…) или null/'all' без фильтра (фаза 24).
export function useLiveKpis(
  campaignId: string | null,
  periodDays: number,
  segmentId: string | null,
  enabled: boolean,
) {
  return useQuery<UiKpis | null>({
    queryKey: ['analytics', 'kpis', campaignId ?? 'all', periodDays, segmentId ?? 'all'],
    queryFn: async () => {
      const resp: KpiResponse = await analyticsApi.getKpis(campaignId, periodDays, segmentId);
      return kpiFromApi(resp);
    },
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

export function useLiveFunnel(
  campaignId: string | null,
  periodDays: number,
  enabled: boolean,
  segmentId: string | null = null,
) {
  return useQuery<UiFunnelStage[] | null>({
    queryKey: ['analytics', 'funnel', campaignId ?? 'all', periodDays, segmentId ?? 'all'],
    queryFn: async () => {
      const resp: FunnelResponse = await analyticsApi.getFunnel(campaignId, periodDays, segmentId);
      return funnelFromApi(resp);
    },
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

export function useLiveMatrix(
  periodDays: number, enabled: boolean, segmentId: string | null = null,
) {
  return useQuery<UiMatrix | null>({
    queryKey: ['analytics', 'segment-matrix', periodDays, segmentId ?? 'all'],
    queryFn: async () => {
      const cells: SegmentMatrixCell[] = await analyticsApi.getSegmentMatrix(periodDays, segmentId);
      return matrixFromApi(cells);
    },
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

// ── Динамика принятых + каналы (фаза 16) ────────────────────────────────────
export function useLiveTrend(
  campaignId: string | null,
  periodDays: number,
  enabled: boolean,
  segmentId: string | null = null,
) {
  return useQuery({
    queryKey: ['analytics', 'daily-trend', campaignId ?? 'all', periodDays, segmentId ?? 'all'],
    queryFn: async () =>
      trendFromApi(await analyticsApi.getDailyTrend(campaignId, periodDays, segmentId)),
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

export function useLiveChannels(
  campaignId: string | null,
  periodDays: number,
  enabled: boolean,
  segmentId: string | null = null,
) {
  return useQuery({
    queryKey: ['analytics', 'channels', campaignId ?? 'all', periodDays, segmentId ?? 'all'],
    queryFn: async () =>
      channelsFromApi(await analyticsApi.getChannels(campaignId, periodDays, segmentId)),
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

// ── A/B-эксперименты (фаза 16) ──────────────────────────────────────────────

export function useExperiments(enabled: boolean) {
  return useQuery({
    queryKey: abKeys.list,
    queryFn: abApi.list,
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
}

export function useExperimentResults(experimentId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: abKeys.results(experimentId ?? ''),
    queryFn: () => abApi.getResults(experimentId as string),
    enabled: enabled && !!experimentId,
    retry: 0, // 409 «need ≥2 variants» и 404 не ретраим
    staleTime: 30_000,
  });
}

export function useExperimentOps() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: abKeys.list });
  const create = useMutation({ mutationFn: abApi.create, onSuccess: invalidate });
  const setStatus = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'start' | 'stop' }) =>
      abApi.updateStatus(id, action),
    onSuccess: invalidate,
  });
  return { create, setStatus };
}

// ── SSE realtime (фаза 19, аутентификация — этап B) ─────────────────────────
import { useEffect, useRef, useState } from 'react';

const SSE_URL = '/api/campaigns/events/stream';
const SSE_RETRY_BASE_MS = 1000;
const SSE_RETRY_MAX_MS = 30_000;

/** Короткоживущий (60 с) sse-токен: EventSource не умеет слать Authorization. */
async function fetchStreamToken(): Promise<string> {
  // silent: realtime опционален (fallback — refetchInterval), фоновые
  // переподключения не должны сыпать тостами.
  const { data } = await campaignClient.post<{ token: string; expires_in: number }>(
    '/events/token', null, { silent: true },
  );
  return data.token;
}

/**
 * Подписка на серверный поток начислений. На событие `stats` дебаунсит
 * инвалидацию списка кампаний — KPI дашборда обновляются без перезагрузки.
 * Возвращает момент последнего события (для индикатора «обновлено N с»).
 *
 * Поток требует `?token=` (POST /events/token). Встроенный реконнект
 * EventSource повторил бы тот же URL с протухшим токеном (401 → CLOSED),
 * поэтому при ошибке соединение закрывается и открывается заново со свежим
 * токеном — с экспоненциальной паузой 1 → 30 с.
 */
export function useEventStream(enabled: boolean) {
  const qc = useQueryClient();
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!enabled || typeof EventSource === 'undefined') return;

    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let stopped = false;

    const scheduleReconnect = () => {
      if (stopped) return;
      const delay = Math.min(SSE_RETRY_MAX_MS, SSE_RETRY_BASE_MS * 2 ** attempt);
      attempt += 1;
      retryTimer = setTimeout(connect, delay);
    };

    async function connect() {
      retryTimer = null;
      let token: string;
      try {
        token = await fetchStreamToken();
      } catch (err) {
        // 401 — сессия завершена (интерцептор уже увёл на логин), 403 — нет
        // права dashboard: повтор не поможет. Остальное (сеть, 5xx) — повтор.
        const status = httpStatus(err);
        if (status !== 401 && status !== 403) scheduleReconnect();
        return;
      }
      if (stopped) return;
      es = new EventSource(`${SSE_URL}?token=${encodeURIComponent(token)}`);
      es.onopen = () => { attempt = 0; };
      es.onmessage = (evt) => {
        try {
          const data = JSON.parse(evt.data);
          if (data.type !== 'stats') return;
        } catch {
          return;
        }
        setLastEventAt(Date.now());
        if (debounceRef.current) clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(() => {
          qc.invalidateQueries({ queryKey: campaignKeys.all });
        }, 800);
      };
      // Ошибку соединения не логируем в console: realtime опционален.
      es.onerror = () => {
        es?.close();
        es = null;
        scheduleReconnect();
      };
    }

    connect();

    return () => {
      stopped = true;
      es?.close();
      if (retryTimer) clearTimeout(retryTimer);
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [enabled, qc]);

  return { lastEventAt };
}

// ── ML-лимиты (фаза 18) ─────────────────────────────────────────────────────
const numify = (v: string | number) => (typeof v === 'number' ? v : parseFloat(v));

/** API → форма стейта страницы MlLimits ({bucket: {maxCashback, ...}}). */
function mlLimitsToUi(resp: any) {
  const limits: Record<string, any> = {};
  for (const item of resp.limits || []) {
    limits[item.segment_bucket] = {
      maxCashback: numify(item.max_rate),
      minCashback: numify(item.min_rate),
      dailyBudget: numify(item.daily_budget),
      autoApprove: !!item.auto_approve,
      riskLevel: item.risk_level,
    };
  }
  return { limits, globalEnabled: !!resp.global_enabled };
}

/**
 * detail 503 у PUT /ml-limits (B39): лимиты записаны в БД, но снапшот для
 * serving-пути (правило R7) не опубликован — его переиздаст задача
 * планировщика. Другой 503 (шлюз, сервис лежит) — изменения НЕ записаны.
 */
export const ML_LIMITS_NOT_PUBLISHED =
  'saved but not published to the serving path; it will be retried';

function savedButNotPublished(err: unknown): boolean {
  const resp = (err as AxiosError<{ detail?: unknown }> | undefined)?.response;
  return resp?.status === 503 && resp.data?.detail === ML_LIMITS_NOT_PUBLISHED;
}

export function useMlLimits(enabled: boolean) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ['ml-limits'],
    queryFn: async () => {
      const { data } = await campaignClient.get('/ml-limits');
      return mlLimitsToUi(data);
    },
    enabled,
    retry: 1,
    staleTime: 30_000,
  });

  const save = useMutation({
    mutationFn: async ({ limits, globalEnabled }: {
      limits: Record<string, any>; globalEnabled: boolean;
    }) => {
      const payload = {
        global_enabled: globalEnabled,
        limits: Object.entries(limits).map(([bucket, l]: [string, any]) => ({
          segment_bucket: bucket,
          min_rate: String(l.minCashback),
          max_rate: String(l.maxCashback),
          daily_budget: String(l.dailyBudget),
          auto_approve: !!l.autoApprove,
          risk_level: l.riskLevel,
        })),
      };
      // published=false — сохранено, но ещё не действует (тост — на странице).
      try {
        await campaignClient.put('/ml-limits', payload, { silent: savedButNotPublished });
        return { published: true };
      } catch (err) {
        if (savedButNotPublished(err)) return { published: false };
        throw err;
      }
    },
    // В обоих исходах изменения уже в БД.
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ml-limits'] }),
  });

  return { query, save };
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

// ── Матрица прав ролей (фаза 26) ────────────────────────────────────────────
export function useRolePermissions(enabled: boolean) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ['role-permissions'],
    queryFn: rolesApi.getPermissions,
    enabled,
    retry: 1,
    staleTime: 30_000,
  });
  const update = useMutation({
    mutationFn: ({ role, permissions }: { role: string; permissions: Record<string, boolean> }) =>
      rolesApi.updatePermissions(role as any, permissions),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['role-permissions'] }),
  });
  return { query, update };
}

// ── SHAP-объяснение по клиенту ──────────────────────────────────────────────
// Через campaign_manager /ml/explain — без побочных эффектов serving-пути (B27).
export function useLiveExplanation(userId: string | null, enabled: boolean) {
  return useQuery<{ explanation: UiExplanation | null; userId: string }>({
    queryKey: ['explanation', userId],
    queryFn: async () => {
      const data = await recommendationsApi.explain(userId as string, 5);
      return { explanation: explanationFromApi(data), userId: userId as string };
    },
    enabled: enabled && !!userId,
    retry: 0,
    staleTime: 60_000,
  });
}

// ── Ростер клиентов с prediction для левой панели (фаза 25) ──────────────────
export function useMlCustomers(enabled: boolean) {
  return useQuery({
    queryKey: ['ml', 'customers'],
    queryFn: async () => {
      const { data } = await campaignClient.get<MlCustomer[]>('/ml/customers', {
        params: { limit: 20 },
      });
      return mlCustomersFromApi(data);
    },
    enabled,
    retry: 1,
    staleTime: 60_000,
  });
}
