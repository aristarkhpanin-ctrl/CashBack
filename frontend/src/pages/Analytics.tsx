import { useMemo, useState } from 'react';
import { Filter } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { campaignApi, campaignKeys } from '@/features/campaigns/api/campaignApi';
import { analyticsApi, analyticsKeys } from '@/features/analytics/api/analyticsApi';
import type { FunnelStep, SegmentMatrixCell } from '@/shared/api/types';

// ── Constants ─────────────────────────────────────────────────────────────────

const periods: { label: string; days: number }[] = [
  { label: `7 дней`, days: 7 },
  { label: `14 дней`, days: 14 },
  { label: `30 дней`, days: 30 },
  { label: `90 дней`, days: 90 },
];
const channels = [`Все каналы`, `Push`, `Email`, `SMS`];

const STEP_LABEL_RU: Record<string, string> = {
  target_audience: `Целевая аудитория`,
  received:        `Получили предложение`,
  opened:          `Открыли`,
  accepted:        `Приняли`,
  transacted:      `Совершили транзакцию`,
  cashback_paid:   `Получили кэшбэк`,
};

function matrixHeatLevel(ctr: number): string {
  if (ctr >= 0.10) return `heat-cell-5`;
  if (ctr >= 0.07) return `heat-cell-4`;
  if (ctr >= 0.05) return `heat-cell-3`;
  if (ctr >= 0.03) return `heat-cell-2`;
  if (ctr >= 0.01) return `heat-cell-1`;
  return `heat-cell-0`;
}

function fmtK(n: number): string {
  if (n >= 1000000) return `${(n / 1000000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(0)}K`;
  return String(n);
}

// ── Funnel Component ──────────────────────────────────────────────────────────

function FunnelChart({ data }: { data: FunnelStep[] }) {
  const max = data[0]?.count || 1;
  const colors = [
    `var(--primary)`,
    `#6366f1`,
    `#818cf8`,
    `#06b6d4`,
    `#10b981`,
    `#34d399`,
  ];

  return (
    <div className="flex flex-col gap-3">
      {data.map((step, i) => {
        const pct = Math.round((step.count / max) * 100);
        const conv = i > 0 ? Math.round((step.count / Math.max(1, data[i - 1].count)) * 100) : null;
        const color = colors[i] || colors[colors.length - 1];
        return (
          <div key={`${step.name}-${i}`} className="flex flex-col gap-1">
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium text-foreground">{STEP_LABEL_RU[step.name] ?? step.name}</span>
              <div className="flex items-center gap-3">
                {conv !== null && (
                  <span
                    className="px-2 py-0.5 rounded-full text-xs font-semibold"
                    style={{ background: `${color}20`, color }}
                  >
                    ↓ {conv}%
                  </span>
                )}
                <span className="text-muted-foreground w-16 text-right">{fmtK(step.count)}</span>
              </div>
            </div>
            <div className="relative h-8 bg-muted rounded-r-md overflow-hidden">
              <div
                className="h-full rounded-r-md transition-all duration-700"
                style={{ width: `${pct}%`, background: color }}
              />
              <span
                className="absolute left-3 top-1/2 -translate-y-1/2 text-xs font-bold"
                style={{ color: pct > 20 ? `#fff` : color }}
              >
                {pct}%
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────────

export default function Analytics() {
  const [campaignId, setCampaignId] = useState<string>(``);    // empty = all
  const [periodDays, setPeriodDays] = useState<number>(30);
  const [channel, setChannel] = useState<string>(`Все каналы`);
  const [segment, setSegment] = useState<string>(`Все сегменты`);

  // List of campaigns for the filter dropdown.
  const campaignsQ = useQuery({
    queryKey: campaignKeys.active,
    queryFn: campaignApi.list,
  });

  const funnelQ = useQuery({
    queryKey: analyticsKeys.funnel(campaignId || null, periodDays),
    queryFn: () => analyticsApi.getFunnel(campaignId || null, periodDays),
  });

  const matrixQ = useQuery({
    queryKey: analyticsKeys.matrix(periodDays),
    queryFn: () => analyticsApi.getSegmentMatrix(periodDays),
  });

  // Derived data ----------------------------------------------------
  const funnelData = funnelQ.data?.steps ?? [];

  const matrix = useMemo(() => {
    const cells: SegmentMatrixCell[] = matrixQ.data ?? [];
    const segmentIds = Array.from(new Set(cells.map((c) => c.segment_id))).sort((a, b) => a - b);
    const mccs = Array.from(new Set(cells.map((c) => c.mcc_code))).slice(0, 8);
    const grid: { ctr: number; impressions: number }[][] = segmentIds.map((seg) =>
      mccs.map((mcc) => {
        const cell = cells.find((c) => c.segment_id === seg && c.mcc_code === mcc);
        return { ctr: cell?.ctr ?? 0, impressions: cell?.impressions ?? 0 };
      }),
    );
    const topCombos = [...cells]
      .filter((c) => c.impressions > 0)
      .sort((a, b) => b.ctr - a.ctr)
      .slice(0, 3);
    return { segmentIds, mccs, grid, topCombos };
  }, [matrixQ.data]);

  const accepted = funnelData.find((s) => s.name === `accepted`)?.count ?? 0;
  const cashbackPaid = funnelData.find((s) => s.name === `cashback_paid`)?.count ?? 0;
  const target = funnelData.find((s) => s.name === `target_audience`)?.count ?? 1;
  const received = funnelData.find((s) => s.name === `received`)?.count ?? 1;

  const segmentFilters = [`Все сегменты`, ...matrix.segmentIds.map((s) => `Сегмент #${s}`)];

  return (
    <div data-cmp="Analytics" className="p-8 flex flex-col gap-6">

      {/* Filters */}
      <div className="bg-card rounded-xl border border-border p-4 flex items-center gap-4 flex-wrap">
        <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
          <Filter size={15} />
          Фильтры:
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Кампания:</span>
          <select
            value={campaignId}
            onChange={(e) => setCampaignId(e.target.value)}
            className="px-3 py-1.5 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value={``}>Все кампании</option>
            {(campaignsQ.data ?? []).map((c) => (
              <option key={c.campaign_id} value={c.campaign_id}>{c.name}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Период:</span>
          <select
            value={periodDays}
            onChange={(e) => setPeriodDays(Number(e.target.value))}
            className="px-3 py-1.5 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {periods.map((p) => (
              <option key={p.days} value={p.days}>{p.label}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Канал:</span>
          <select
            value={channel}
            onChange={(e) => setChannel(e.target.value)}
            className="px-3 py-1.5 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {channels.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Сегмент:</span>
          <select
            value={segment}
            onChange={(e) => setSegment(e.target.value)}
            className="px-3 py-1.5 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {segmentFilters.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </div>
      </div>

      {/* Main content */}
      <div className="flex gap-5">

        {/* Funnel */}
        <div className="flex-1 bg-card rounded-xl border border-border p-6 shadow-custom">
          <div className="mb-5">
            <h2 className="text-sm font-semibold text-foreground">Воронка кэшбэк-предложения</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              Убывание аудитории по этапам · {periods.find((p) => p.days === periodDays)?.label}
              {campaignId ? ` · ${(campaignsQ.data ?? []).find((c) => c.campaign_id === campaignId)?.name ?? campaignId}` : ` · Все кампании`}
            </p>
          </div>

          {funnelQ.isLoading && (
            <div className="text-xs text-muted-foreground py-8 text-center">Загрузка воронки…</div>
          )}
          {!funnelQ.isLoading && funnelData.length === 0 && (
            <div className="text-xs text-muted-foreground py-8 text-center">Нет данных по воронке.</div>
          )}
          {funnelData.length > 0 && <FunnelChart data={funnelData} />}

          {/* Summary row */}
          {funnelData.length > 0 && (
            <div className="flex gap-4 mt-6 pt-5 border-t border-border">
              <div className="flex-1 text-center">
                <p className="text-xs text-muted-foreground mb-1">Общая конверсия</p>
                <p className="text-2xl font-extrabold" style={{ color: `var(--primary)` }}>
                  {target > 0 ? ((cashbackPaid / target) * 100).toFixed(2) : `0.00`}%
                </p>
              </div>
              <div className="flex-1 text-center">
                <p className="text-xs text-muted-foreground mb-1">Получили кэшбэк</p>
                <p className="text-2xl font-extrabold text-foreground">{fmtK(cashbackPaid)}</p>
              </div>
              <div className="flex-1 text-center">
                <p className="text-xs text-muted-foreground mb-1">Отклик (принятие)</p>
                <p className="text-2xl font-extrabold" style={{ color: `#10b981` }}>
                  {received > 0 ? ((accepted / received) * 100).toFixed(2) : `0.00`}%
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Segment × Category matrix */}
        <div className="bg-card rounded-xl border border-border p-6 shadow-custom" style={{ minWidth: 520 }}>
          <div className="mb-5">
            <h2 className="text-sm font-semibold text-foreground">Матрица: Сегмент × Категория MCC</h2>
            <p className="text-xs text-muted-foreground mt-0.5">CTR (accepted/impressions) за период</p>
          </div>
          <div className="overflow-x-auto">
            {matrixQ.isLoading && (
              <div className="text-xs text-muted-foreground py-8 text-center">Загрузка матрицы…</div>
            )}
            {!matrixQ.isLoading && matrix.mccs.length === 0 && (
              <div className="text-xs text-muted-foreground py-8 text-center">Нет данных по сегмент × MCC.</div>
            )}
            {matrix.mccs.length > 0 && (
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr>
                    <th className="text-left pb-3 pr-3 text-muted-foreground font-normal" style={{ width: 80 }}>Сегмент</th>
                    {matrix.mccs.map((cat) => (
                      <th
                        key={cat}
                        className="pb-3 px-1 text-center font-normal text-muted-foreground"
                        style={{ writingMode: `vertical-rl`, transform: `rotate(180deg)`, height: 72, fontSize: 11 }}
                      >
                        MCC {cat}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {matrix.segmentIds.map((seg, si) => (
                    <tr key={seg}>
                      <td className="py-1 pr-3 font-semibold text-foreground text-xs">#{seg}</td>
                      {matrix.mccs.map((_, ci) => {
                        const cell = matrix.grid[si][ci];
                        const pct = (cell.ctr * 100).toFixed(1);
                        return (
                          <td key={ci} className="py-1 px-1">
                            <div
                              className={`${matrixHeatLevel(cell.ctr)} rounded`}
                              style={{
                                width: 44,
                                height: 28,
                                display: `flex`,
                                alignItems: `center`,
                                justifyContent: `center`,
                                fontSize: 11,
                                fontWeight: 700,
                                margin: `0 auto`,
                              }}
                              title={`Impressions: ${cell.impressions}`}
                            >
                              {pct}
                            </div>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Matrix legend */}
          <div className="flex items-center gap-2 mt-4 pt-3 border-t border-border">
            <span className="text-xs text-muted-foreground">Низкий CTR</span>
            {[0, 1, 2, 3, 4, 5].map((v) => (
              <div key={v} className={`heat-cell-${v} rounded`} style={{ width: 22, height: 22 }} />
            ))}
            <span className="text-xs text-muted-foreground">Высокий CTR</span>
          </div>

          {/* Top combos */}
          {matrix.topCombos.length > 0 && (
            <div className="mt-4 pt-3 border-t border-border">
              <p className="text-xs font-semibold text-foreground mb-2">Лучшие комбинации</p>
              <div className="flex flex-col gap-1.5">
                {matrix.topCombos.map((row) => (
                  <div key={`${row.segment_id}-${row.mcc_code}`} className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">
                      Сегмент #{row.segment_id} × MCC {row.mcc_code}
                    </span>
                    <span
                      className="px-2 py-0.5 rounded-full text-xs font-bold"
                      style={{ background: `#4f46e520`, color: `#4f46e5` }}
                    >
                      {(row.ctr * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
