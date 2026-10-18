// @ts-nocheck
/* eslint-disable */
import React, { useState, useEffect, useMemo } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { Toaster, toast } from "sonner";

import { AppShell } from "./components/cashback/Layout";
import Dashboard from "./components/cashback/pages/Dashboard";
import Campaigns from "./components/cashback/pages/Campaigns";
import Analytics from "./components/cashback/pages/Analytics";
import Experiments from "./components/cashback/pages/Experiments";
import Explanations from "./components/cashback/pages/Explanations";
import MlLimits from "./components/cashback/pages/MlLimits";
import Users from "./components/cashback/pages/Users";
import Login from "./components/cashback/pages/Login";
import ChangePassword from "./components/cashback/pages/ChangePassword";

import {
  USERS, CAMPAIGNS as INITIAL_CAMPAIGNS, PERMISSIONS,
  SEGMENTS as MOCK_SEGMENTS, MCC_CATEGORIES as MOCK_MCC,
} from "./data/mockData";
import {
  useApiHealth, useLiveCampaigns, useCampaignOps, statusToAction, useAdminUsers,
  useMlLimits, useEventStream, useReference, useRolePermissions,
} from "./shared/api/live";
import { authApi, toUiRole, initialsOf } from "./features/auth/api/authApi";
import {
  isAuthenticated, onAuthChange, onPasswordChangeRequired,
} from "./shared/api/tokenStore";

// ── Error Boundary ─────────────────────────────────────────────────────────────
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false };
  static getDerivedStateFromError() { return { hasError: true }; }
  componentDidCatch(error: Error) {
    console.log(`ErrorBoundary caught:`, error);
    toast.error(`Что-то пошло не так, обновите страницу`);
  }
  render() {
    return this.state.hasError
      ? <div style={{ padding: 32, textAlign: "center", color: "#94a3b8" }}>Произошла ошибка</div>
      : this.props.children;
  }
}

// ── Page titles ────────────────────────────────────────────────────────────────
const PAGE_TITLES: Record<string, { title: string; subtitle: string }> = {
  dashboard:    { title: "Дашборд",              subtitle: "Обзор состояния платформы" },
  campaigns:    { title: "Управление кампаниями", subtitle: "Создание и мониторинг кэшбэк-программ" },
  analytics:    { title: "Аналитика",            subtitle: "Интерактивные отчёты и воронки" },
  experiments:  { title: "A/B-эксперименты",     subtitle: "Двухпропорционный z-тест: control vs treatment" },
  explanations: { title: "ML-объяснения",        subtitle: "SHAP-разбор рекомендаций для каждого клиента" },
  ml_limits:    { title: "ML-лимиты",            subtitle: "Максимальные ставки кэшбэка для ML-рекомендаций по сегментам" },
  users:        { title: "Пользователи",         subtitle: "Управление доступом и ролями" },
};

// ── Live/demo indicator (TopBar action slot) ───────────────────────────────────
function DataSourceBadge({ live }: { live: boolean }) {
  return (
    <div title={live
      ? "Данные загружаются из backend-сервисов (docker-стек запущен)"
      : "Backend недоступен — интерфейс работает на демо-данных"}
      style={{
        display: "flex", alignItems: "center", gap: 6,
        padding: "4px 10px", borderRadius: 20, fontSize: 11, fontWeight: 700,
        background: live ? "oklch(0.95 0.05 160)" : "#fef9c3",
        color: live ? "oklch(0.40 0.15 160)" : "#92400e",
        border: `1px solid ${live ? "oklch(0.85 0.08 160)" : "#fde68a"}`,
        whiteSpace: "nowrap", userSelect: "none",
      }}>
      <span style={{
        width: 7, height: 7, borderRadius: "50%",
        background: live ? "oklch(0.65 0.18 160)" : "#eab308",
      }} />
      {live ? "LIVE API" : "ДЕМО-ДАННЫЕ"}
    </div>
  );
}

// ── Realtime indicator (фаза 19) ────────────────────────────────────────────────
function RealtimeIndicator({ lastEventAt }: { lastEventAt: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => tick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const secs = Math.max(0, Math.round((Date.now() - lastEventAt) / 1000));
  return (
    <div title="Realtime-поток начислений (SSE)" style={{
      display: "flex", alignItems: "center", gap: 6,
      fontSize: 11, color: "#64748b", whiteSpace: "nowrap",
    }}>
      <span style={{
        width: 7, height: 7, borderRadius: "50%",
        background: "oklch(0.65 0.18 160)",
        boxShadow: secs < 3 ? "0 0 0 3px oklch(0.90 0.10 160)" : "none",
        transition: "box-shadow 0.3s",
      }} />
      обновлено {secs} с назад
    </div>
  );
}

// Демо-режим: тот же признак, что starts_in_future бэкенда — старт (полночь
// UTC дня startDate) ещё не наступил.
function startsInFuture(c: { startDate?: string }): boolean {
  return !!c.startDate && c.startDate > new Date().toISOString().slice(0, 10);
}

// ── Layout ─────────────────────────────────────────────────────────────────────
function AppContent() {
  const [page, setPage] = useState("dashboard");
  const [demoUser, setDemoUser] = useState<any>(USERS[0]);
  const [localCampaigns, setLocalCampaigns] = useState<any[]>(INITIAL_CAMPAIGNS);
  // «Показать архив» на странице кампаний (B24): в live — include_archived=true.
  const [showArchived, setShowArchived] = useState(false);

  // ── Auth (фаза 15, этап B): live-режим требует JWT-сессию ─────────────────
  const [authUser, setAuthUser] = useState<any>(null);
  const [authChecked, setAuthChecked] = useState(false);
  // 403 «password change required» с любого эндпоинта (этап B).
  const [pwdChangeForced, setPwdChangeForced] = useState(false);
  const qc = useQueryClient();

  const health = useApiHealth();

  // Восстановление сессии по refresh-токену при первом онлайне API.
  useEffect(() => {
    if (!health.campaigns || authChecked) return;
    (async () => {
      try {
        setAuthUser(await authApi.restoreSession());
      } catch { /* refresh протух — остаёмся на странице логина */ }
      setAuthChecked(true);
    })();
  }, [health.campaigns, authChecked]);

  // Сессия завершена (logout / refresh отклонён) → мгновенный logout в UI;
  // кэш прошлой сессии не должен достаться следующему пользователю
  // (health-пинг оставляем — иначе UI мигнёт в демо-режим).
  useEffect(() => onAuthChange(() => {
    if (isAuthenticated()) return;
    setAuthUser(null);
    setPwdChangeForced(false);
    qc.removeQueries({ predicate: q => q.queryKey[0] !== "api-health" });
  }), [qc]);

  useEffect(() => onPasswordChangeRequired(() => setPwdChangeForced(true)), []);

  async function handleLogin(email: string, password: string) {
    const login = await authApi.login(email, password);
    const me = await authApi.me();      // me доступен и до смены пароля
    setPwdChangeForced(login.mustChangePassword);
    setAuthUser(me);
  }

  async function handlePasswordChanged() {
    setAuthUser(await authApi.me());
    setPwdChangeForced(false);
    // Ответы, полученные с 403 «password change required» (часть хуков
    // гасит ошибку в пустой список), не должны дожить до staleTime.
    qc.invalidateQueries({ predicate: q => q.queryKey[0] !== "api-health" });
  }

  async function handleLogout() {
    await authApi.logout();             // POST /auth/logout, затем clearTokens
    setPage("dashboard");
  }

  const authed = authUser != null;
  const mustChangePassword = authed && (!!authUser.must_change_password || pwdChangeForced);
  // Запросы к API — только с полноценной сессией: токен до смены пароля
  // сервер принимает лишь на /auth/me и /auth/change-password.
  const session = health.campaigns && authed && !mustChangePassword;
  const livePerms = authUser?.permissions ?? {};
  const canManageUsers = session && !!livePerms.users;
  const liveQ = useLiveCampaigns(session, showArchived);
  const ops = useCampaignOps();
  const adminUsers = useAdminUsers(canManageUsers);
  const mlLimits = useMlLimits(session && !!livePerms.analytics);
  const rolePerms = useRolePermissions(canManageUsers);

  const isLive = session && Array.isArray(liveQ.data);
  const listedCampaigns = isLive ? liveQ.data : localCampaigns;
  // Архивные кампании видны только на странице кампаний под «Показать архив»;
  // дашборд, аналитика и счётчик в сайдбаре их не учитывают. useMemo — та же
  // ссылка между рендерами (страницы мемоизируют расчёты по списку).
  const campaigns = useMemo(
    () => listedCampaigns.filter((c: any) => !c.archivedAt), [listedCampaigns]);

  // Справочники (фаза 21): в live — из API, иначе mock. Единый источник для
  // визарда кампаний, фильтров аналитики и карточек ML-лимитов.
  const reference = useReference(isLive);
  const segments = reference.segments ?? MOCK_SEGMENTS;
  const mccCategories = reference.mccCategories ?? MOCK_MCC;

  // Realtime-поток начислений (фаза 19): при работающем стеке KPI обновляются
  // без перезагрузки, как только транзакция прошла через listener.
  const { lastEventAt } = useEventStream(isLive && !!livePerms.dashboard);

  // В live-режиме текущий пользователь — из /auth/me, роль управляет UI.
  const currentUser = health.campaigns && authed
    ? {
        id: authUser.user_id,
        name: authUser.full_name,
        email: authUser.email,
        role: toUiRole(authUser.role),
        avatar: initialsOf(authUser.full_name),
        lastLogin: authUser.last_login_at
          ? new Date(authUser.last_login_at).toLocaleString("ru")
          : "—",
      }
    : demoUser;

  // Фаза 26: в live гейтим меню/страницы живыми правами роли из /auth/me
  // (мутируем страничный namespace PERMISSIONS — тот же паттерн, что справочники).
  if (health.campaigns && authed && authUser?.permissions) {
    (PERMISSIONS as any)[currentUser.role] = authUser.permissions;
  }

  // ── Мутации кампаний: live → API + refetch, demo → локальный стейт ───────────
  // saved=false — ничего не записано (ошибка уже показана интерцептором axios).
  // activationFailed — кампания записана, но запуск (PATCH /status) отклонён:
  // она осталась черновиком, и UI не должен рапортовать об успешном запуске.
  // startsInFuture — активна, но платить начнёт со start_date (B25).
  async function saveCampaign(
    data: any, isEdit: boolean,
  ): Promise<{ saved: boolean; activationFailed?: boolean; startsInFuture?: boolean }> {
    if (!isLive) {
      if (isEdit) {
        setLocalCampaigns(cs => cs.map(c => (c.id === data.id ? data : c)));
      } else {
        setLocalCampaigns(cs => [...cs, {
          ...data, id: Date.now(), spent: 0, ctr: 0, roi: 0, createdBy: currentUser.id,
        }]);
      }
      return { saved: true, startsInFuture: data.status === "active" && startsInFuture(data) };
    }
    let id: string;
    try {
      if (isEdit) {
        await ops.update.mutateAsync({ id: String(data.id), form: data });
        id = String(data.id);
      } else {
        id = (await ops.create.mutateAsync(data)).campaign_id;
      }
    } catch {
      return { saved: false };
    }
    if (data.status === "active") {
      try {
        const res = await ops.setStatus.mutateAsync({ id, action: "activate" });
        return { saved: true, startsInFuture: !!res.starts_in_future };
      } catch {
        return { saved: true, activationFailed: true };
      }
    }
    return { saved: true };
  }

  async function changeStatus(
    id: any, newStatus: string,
  ): Promise<{ changed: boolean; startsInFuture?: boolean }> {
    if (!isLive) {
      const c = localCampaigns.find(x => x.id === id);
      setLocalCampaigns(cs => cs.map(x => (x.id === id ? { ...x, status: newStatus } : x)));
      return { changed: true, startsInFuture: newStatus === "active" && !!c && startsInFuture(c) };
    }
    const action = statusToAction(newStatus);
    if (!action) return { changed: false };
    try {
      const res = await ops.setStatus.mutateAsync({ id: String(id), action });
      return { changed: true, startsInFuture: !!res.starts_in_future };
    } catch {
      return { changed: false };
    }
  }

  async function deleteCampaign(id: any): Promise<boolean> {
    if (!isLive) {
      setLocalCampaigns(cs => cs.filter(c => c.id !== id));
      return true;
    }
    // Удаление в live — только черновик без истории (B24; иначе 409).
    try {
      await ops.remove.mutateAsync(String(id));
      return true;
    } catch {
      return false; // 403/409 уже показаны axios-интерцептором
    }
  }

  async function archiveCampaign(id: any): Promise<boolean> {
    if (!isLive) {
      setLocalCampaigns(cs => cs.map(c => (
        c.id === id ? { ...c, archivedAt: c.archivedAt ?? new Date().toISOString() } : c)));
      return true;
    }
    try {
      await ops.archive.mutateAsync(String(id));
      return true;
    } catch {
      return false; // 403/409 уже показаны axios-интерцептором
    }
  }

  const campaignOps = {
    isLive,
    save: saveCampaign,
    changeStatus,
    remove: deleteCampaign,
    archive: archiveCampaign,
    showArchived,
    setShowArchived,
  };

  function handleUserSwitch(user: any) {
    setDemoUser(user);
    const perms = (PERMISSIONS as any)[user.role];
    const pagePerms: Record<string, string> = {
      dashboard: "dashboard",
      campaigns: "campaigns_view",
      analytics: "analytics",
      experiments: "analytics",
      explanations: "analytics",
      ml_limits: "users",
      users: "users",
    };
    if (!perms[pagePerms[page]]) setPage("dashboard");
  }

  // Ops страницы «Пользователи»: в live — /auth/users (право users; правила
  // для ADMIN-аккаунтов — на странице и на сервере), иначе локально.
  const usersOps = isLive && canManageUsers
    ? {
        enabled: true,
        users: adminUsers.query.data ?? [],
        loading: adminUsers.query.isLoading,
        save: async (data: any, isEdit: boolean) => {
          try {
            if (isEdit) {
              await adminUsers.update.mutateAsync({
                id: data.id,
                patch: { full_name: data.name, role: data.role.toUpperCase() },
              });
            } else {
              await adminUsers.create.mutateAsync({
                email: data.email,
                password: data.password,
                full_name: data.name,
                role: data.role.toUpperCase(),
              });
            }
            return true;
          } catch { return false; }
        },
        changeRole: async (id: any, uiRole: string) => {
          try {
            await adminUsers.update.mutateAsync({
              id: String(id), patch: { role: uiRole.toUpperCase() },
            });
            return true;
          } catch { return false; }
        },
        deactivate: async (id: any) => {
          try {
            await adminUsers.update.mutateAsync({
              id: String(id), patch: { is_active: false },
            });
            return true;
          } catch { return false; }
        },
        // Фаза 26: матрица прав из /roles/permissions (uppercase → lowercase).
        perms: {
          matrix: rolePerms.query.data ? {
            admin: (rolePerms.query.data as any).ADMIN,
            marketer: (rolePerms.query.data as any).MARKETER,
            analyst: (rolePerms.query.data as any).ANALYST,
          } : null,
          toggle: async (uiRole: string, key: string, next: boolean) => {
            try {
              await rolePerms.update.mutateAsync({
                role: uiRole.toUpperCase(), permissions: { [key]: next },
              });
              return true;
            } catch { return false; }
          },
        },
      }
    : null;

  // ── Login-гейт live-режима ─────────────────────────────────────────────────
  if (health.campaigns && !authed) {
    if (!authChecked) {
      return (
        <div style={{
          minHeight: "100vh", display: "flex", alignItems: "center",
          justifyContent: "center", color: "#94a3b8",
          fontFamily: "'Inter', sans-serif", background: "#f1f5f9",
        }}>Проверка сессии…</div>
      );
    }
    return <Login onLogin={handleLogin} />;
  }
  if (health.campaigns && mustChangePassword) {
    return (
      <ChangePassword
        email={authUser.email}
        onChanged={handlePasswordChanged}
        onLogout={handleLogout}
      />
    );
  }

  const pageTitle = PAGE_TITLES[page] ?? { title: "CashBack Admin", subtitle: "" };
  const topBarProps = {
    ...pageTitle,
    action: health.checked ? (
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {isLive && lastEventAt != null && <RealtimeIndicator lastEventAt={lastEventAt} />}
        <DataSourceBadge live={isLive} />
      </div>
    ) : null,
  };

  return (
    <AppShell
      currentPage={page}
      onNavigate={setPage}
      topBarProps={topBarProps}
      currentUser={currentUser}
      onUserSwitch={handleUserSwitch}
      onLogout={health.campaigns && authed ? handleLogout : null}
      campaigns={campaigns}
    >
      <ErrorBoundary>
        {page === "dashboard"    && <Dashboard onNavigate={setPage} currentUser={currentUser} campaigns={campaigns} isLive={isLive} />}
        {page === "campaigns"    && <Campaigns currentUser={currentUser} wizardVariant="steps" campaigns={showArchived ? listedCampaigns : campaigns} ops={campaignOps} segments={segments} mccCategories={mccCategories} />}
        {page === "analytics"    && <Analytics currentUser={currentUser} campaigns={campaigns} isLive={isLive} segments={segments} mccCategories={mccCategories} />}
        {page === "experiments"  && <Experiments currentUser={currentUser} isLive={isLive} />}
        {page === "explanations" && <Explanations campaignsOnline={isLive} />}
        {page === "ml_limits"    && (
          <MlLimits
            currentUser={currentUser}
            segments={segments}
            liveLimits={isLive ? {
              enabled: true,
              initial: mlLimits.query.data ?? null,
              // published=false — записано в БД, но ещё не действует (503, B39).
              save: async (limits: any, globalEnabled: boolean) => {
                try {
                  const { published } = await mlLimits.save.mutateAsync({ limits, globalEnabled });
                  return { saved: true, published };
                } catch { return { saved: false }; }
              },
            } : null}
          />
        )}
        {page === "users"        && <Users currentUser={currentUser} liveUsers={usersOps} />}
      </ErrorBoundary>
    </AppShell>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
});

const App = () => (
  <QueryClientProvider client={queryClient}>
    <AppContent />
    <Toaster position="top-right" richColors />
  </QueryClientProvider>
);

export default App;
