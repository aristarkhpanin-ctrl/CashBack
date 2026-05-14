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

// ── Layout ─────────────────────────────────────────────────────────────────────
function AppContent() {
  const [page, setPage] = useState("dashboard");
  const [currentUser, setCurrentUser] = useState<any>(USERS[0]);
  const [campaigns, setCampaigns] = useState<any[]>(INITIAL_CAMPAIGNS);

  function handleCampaignsChange(updated: any[]) {
    setCampaigns(updated);
  }

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

  const topBarProps = PAGE_TITLES[page] ?? { title: "CashBack Admin", subtitle: "" };

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
        {page === "dashboard"    && <Dashboard onNavigate={setPage} currentUser={currentUser} campaigns={campaigns} />}
        {page === "campaigns"    && <Campaigns currentUser={currentUser} wizardVariant="steps" campaigns={campaigns} onCampaignsChange={handleCampaignsChange} />}
        {page === "analytics"    && <Analytics currentUser={currentUser} campaigns={campaigns} />}
        {page === "explanations" && <Explanations />}
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
