// @ts-nocheck
/* eslint-disable */
import React, { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster, toast } from "sonner";

import { AppShell } from "./components/cashback/Layout";
import Dashboard from "./components/cashback/pages/Dashboard";
import Campaigns from "./components/cashback/pages/Campaigns";
import Analytics from "./components/cashback/pages/Analytics";
import Explanations from "./components/cashback/pages/Explanations";
import MlLimits from "./components/cashback/pages/MlLimits";
import Users from "./components/cashback/pages/Users";

import { USERS, CAMPAIGNS as INITIAL_CAMPAIGNS, PERMISSIONS } from "./data/mockData";
import {
  useApiHealth, useLiveCampaigns, useCampaignOps, statusToAction,
} from "./shared/api/live";

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

// ── Layout ─────────────────────────────────────────────────────────────────────
function AppContent() {
  const [page, setPage] = useState("dashboard");
  const [currentUser, setCurrentUser] = useState<any>(USERS[0]);
  const [localCampaigns, setLocalCampaigns] = useState<any[]>(INITIAL_CAMPAIGNS);

  const health = useApiHealth();
  const liveQ = useLiveCampaigns(health.campaigns);
  const ops = useCampaignOps();

  const isLive = health.campaigns && Array.isArray(liveQ.data);
  const campaigns = isLive ? liveQ.data : localCampaigns;

  // ── Мутации кампаний: live → API + refetch, demo → локальный стейт ───────────
  async function saveCampaign(data: any, isEdit: boolean): Promise<boolean> {
    if (!isLive) {
      if (isEdit) {
        setLocalCampaigns(cs => cs.map(c => (c.id === data.id ? data : c)));
      } else {
        setLocalCampaigns(cs => [...cs, {
          ...data, id: Date.now(), spent: 0, ctr: 0, roi: 0, createdBy: currentUser.id,
        }]);
      }
      return true;
    }
    try {
      if (isEdit) {
        await ops.update.mutateAsync({ id: String(data.id), form: data });
        if (data.status === "active") {
          await ops.setStatus.mutateAsync({ id: String(data.id), action: "activate" });
        }
      } else {
        const created = await ops.create.mutateAsync(data);
        if (data.status === "active") {
          await ops.setStatus.mutateAsync({ id: created.campaign_id, action: "activate" });
        }
      }
      return true;
    } catch {
      return false; // ошибка уже показана интерцептором axios
    }
  }

  async function changeStatus(id: any, newStatus: string): Promise<boolean> {
    if (!isLive) {
      setLocalCampaigns(cs => cs.map(c => (c.id === id ? { ...c, status: newStatus } : c)));
      return true;
    }
    const action = statusToAction(newStatus);
    if (!action) return false;
    try {
      await ops.setStatus.mutateAsync({ id: String(id), action });
      return true;
    } catch {
      return false;
    }
  }

  async function deleteCampaign(id: any): Promise<boolean> {
    if (!isLive) {
      setLocalCampaigns(cs => cs.filter(c => c.id !== id));
      return true;
    }
    toast.warning("Удаление недоступно в live-режиме", {
      description: "Кампании в БД не удаляются ради аудита — переведите её в «Завершена».",
    });
    return false;
  }

  const campaignOps = {
    isLive,
    save: saveCampaign,
    changeStatus,
    remove: deleteCampaign,
  };

  function handleUserSwitch(user: any) {
    setCurrentUser(user);
    const perms = (PERMISSIONS as any)[user.role];
    const pagePerms: Record<string, string> = {
      dashboard: "dashboard",
      campaigns: "campaigns_view",
      analytics: "analytics",
      explanations: "analytics",
      ml_limits: "users",
      users: "users",
    };
    if (!perms[pagePerms[page]]) setPage("dashboard");
  }

  const pageTitle = PAGE_TITLES[page] ?? { title: "CashBack Admin", subtitle: "" };
  const topBarProps = {
    ...pageTitle,
    action: health.checked ? <DataSourceBadge live={isLive} /> : null,
  };

  return (
    <AppShell
      currentPage={page}
      onNavigate={setPage}
      topBarProps={topBarProps}
      currentUser={currentUser}
      onUserSwitch={handleUserSwitch}
      campaigns={campaigns}
    >
      <ErrorBoundary>
        {page === "dashboard"    && <Dashboard onNavigate={setPage} currentUser={currentUser} campaigns={campaigns} isLive={isLive} />}
        {page === "campaigns"    && <Campaigns currentUser={currentUser} wizardVariant="steps" campaigns={campaigns} ops={campaignOps} />}
        {page === "analytics"    && <Analytics currentUser={currentUser} campaigns={campaigns} isLive={isLive} />}
        {page === "explanations" && <Explanations recApiOnline={health.recommendations} />}
        {page === "ml_limits"    && <MlLimits currentUser={currentUser} />}
        {page === "users"        && <Users currentUser={currentUser} />}
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
