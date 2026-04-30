import React, { useState } from 'react';
import { BrowserRouter, Routes, Route, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster, toast } from 'sonner';
import Sidebar from './components/Sidebar';
import TopBar from './components/TopBar';
import Dashboard from './pages/Dashboard';
import Campaigns from './pages/Campaigns';
import Analytics from './pages/Analytics';
import Experiments from './pages/Experiments';
import ExplainRecommendation from './pages/ExplainRecommendation';
import NotFound from './pages/NotFound';

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
      ? <div className="p-8 text-center text-muted-foreground">Произошла ошибка</div>
      : this.props.children;
  }
}

// ── Page titles ────────────────────────────────────────────────────────────────

const pageTitles: Record<string, string> = {
  '/': `Дашборд`,
  '/campaigns': `Управление кампаниями`,
  '/analytics': `Аналитика`,
  '/experiments': `A/B эксперименты`,
};

function titleFor(path: string): string {
  if (pageTitles[path]) return pageTitles[path];
  if (path.startsWith(`/recommendations/`) && path.endsWith(`/explain`)) {
    return `Объяснение рекомендации`;
  }
  return `CashbackAdmin`;
}

// ── Layout ─────────────────────────────────────────────────────────────────────

function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();
  const title = titleFor(location.pathname);

  return (
    <div
      className="flex min-h-screen bg-background"
      style={{ minWidth: 1440 }}
    >
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <div className="flex flex-col flex-1 min-w-0">
        <TopBar title={title} />
        <main className="flex-1 overflow-auto">
          <ErrorBoundary>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/campaigns" element={<Campaigns />} />
              <Route path="/analytics" element={<Analytics />} />
              <Route path="/experiments" element={<Experiments />} />
              <Route path="/recommendations/:id/explain" element={<ExplainRecommendation />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}

// ── App ────────────────────────────────────────────────────────────────────────

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
    <BrowserRouter>
      <AppLayout />
      <Toaster position="top-right" richColors />
    </BrowserRouter>
  </QueryClientProvider>
);

export default App;
