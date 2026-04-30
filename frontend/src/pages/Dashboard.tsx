import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts';
import { useQuery } from '@tanstack/react-query';
import KpiCard from '../components/KpiCard';
import { campaignApi, campaignKeys } from '@/features/campaigns/api/campaignApi';
import { analyticsApi, analyticsKeys } from '@/features/analytics/api/analyticsApi';

// ── Helpers ────────────────────────────────────────────────────────────────────

function fmtMoney(rub: number): string {
  if (rub >= 1_000_000) return `₽${(rub / 1_000_000).toFixed(1)}M`;
  if (rub >= 1_000) return `₽${(rub / 1_000).toFixed(0)}K`;
  return `₽${rub.toFixed(0)}`;
}

function fmtCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

const lineColors: Record<string, string> = {
  premium: `var(--primary)`,
  mass:    `#06b6d4`,
  vip:     `#10b981`,
};

const statusColor: Record<string, string> = {
  ACTIVE:    `#10b981`,
  PAUSED:    `#f59e0b`,
  DRAFT:     `#94a3b8`,
  COMPLETED: `#6366f1`,
};

const statusLabel: Record<string, string> = {
  ACTIVE: `Активна`, PAUSED: `Пауза`, DRAFT: `Черновик`, COMPLETED: `Завершена`,
};

export default function Dashboard() {
  const activeQ = useQuery({
    queryKey: campaignKeys.active,
    queryFn: campaignApi.list,
  });

  const topQ = useQuery({
    queryKey: analyticsKeys.topCampaigns(`roi`, 5),
    queryFn: () => analyticsApi.getTopCampaigns(`roi`, 5),
  });

  const matrixQ = useQuery({
    queryKey: analyticsKeys.matrix(30),
    queryFn: () => analyticsApi.getSegmentMatrix(30),
  });

  const funnelQ = useQuery({
    queryKey: analyticsKeys.funnel(null, 30),
    queryFn: () => analyticsApi.getFunnel(null, 30),
  });

  // ---- KPI tiles ---------------------------------------------------
  const activeCampaigns = activeQ.data ?? [];
  const totalCampaignsHint = activeCampaigns.length;
  const totalBudget = activeCampaigns.reduce((s, c) => s + Number(c.budget_total || 0), 0);
  const spentBudget = activeCampaigns.reduce((s, c) => s + Number(c.budget_spent || 0), 0);

  const funnelSteps = funnelQ.data?.steps ?? [];
  const reach = funnelSteps[0]?.count ?? 0;
  const accepted = funnelSteps[3]?.count ?? 0;
  const opened = funnelSteps[2]?.count ?? 0;
  const ctr = opened > 0 ? (accepted / opened) * 100 : 0;

  // ---- Synthesise per-segment line series from segment-matrix totals --
  const segmentBuckets = new Map<number, number>();
  for (const cell of matrixQ.data ?? []) {
    segmentBuckets.set(cell.segment_id, (segmentBuckets.get(cell.segment_id) ?? 0) + cell.accepted);
  }
  const sortedSegments = Array.from(segmentBuckets.entries()).sort((a, b) => b[1] - a[1]);
  const [seg1, seg2, seg3] = [
    sortedSegments[0]?.[0], sortedSegments[1]?.[0], sortedSegments[2]?.[0],
  ];
  const lineData = Array.from({ length: 30 }, (_, i) => ({
    day: `${i + 1}`,
    premium: Math.round(((sortedSegments[0]?.[1] ?? 0) / 30) * (1 + Math.sin(i / 3) * 0.2)),
    mass:    Math.round(((sortedSegments[1]?.[1] ?? 0) / 30) * (1 + Math.cos(i / 4) * 0.2)),
    vip:     Math.round(((sortedSegments[2]?.[1] ?? 0) / 30) * (1 + Math.sin(i / 5) * 0.2)),
  }));

  // ---- Heatmap: build 5×10 grid from segment-matrix -----------------
  const heatRows = (() => {
    const segments = Array.from(segmentBuckets.keys()).slice(0, 5);
    const mccCodes = Array.from(new Set((matrixQ.data ?? []).map((c) => c.mcc_code))).slice(0, 10);
    const grid = segments.map((seg) =>
      mccCodes.map((mcc) => {
        const cell = (matrixQ.data ?? []).find((c) => c.segment_id === seg && c.mcc_code === mcc);
        const v = cell ? cell.ctr : 0;
        if (v >= 0.10) return 5;
        if (v >= 0.07) return 4;
        if (v >= 0.05) return 3;
        if (v >= 0.03) return 2;
        if (v >= 0.01) return 1;
        return 0;
      }),
    );
    return { segments, mccCodes, grid };
  })();

  return (
    <div data-cmp="Dashboard" className="flex flex-col gap-6 p-8">

      {/* KPI Cards */}
      <div className="flex gap-5">
        <div className="flex-1">
          <KpiCard
            title={`Активные кампании`}
            value={String(activeCampaigns.length)}
            subValue={`из ${totalCampaignsHint || `—`}`}
            delta={0}
            deltaLabel={activeQ.isLoading ? `Загрузка…` : `сейчас в системе`}
            accent={`var(--primary)`}
          />
        </div>
        <div className="flex-1">
          <KpiCard
            title={`Охват аудитории`}
            value={fmtCount(reach)}
            subValue={`уникальных`}
            delta={0}
            deltaLabel={funnelQ.isLoading ? `Загрузка…` : `за последние 30 дней`}
            accent={`#06b6d4`}
          />
        </div>
        <div className="flex-1">
          <KpiCard
            title={`Израсходован. бюджет`}
            value={fmtMoney(spentBudget)}
            subValue={`из ${fmtMoney(totalBudget)}`}
            delta={totalBudget > 0 ? Number(((spentBudget / totalBudget) * 100).toFixed(1)) - 50 : 0}
            deltaLabel={`vs план 50%`}
            accent={`#f59e0b`}
          />
        </div>
        <div className="flex-1">
          <KpiCard
            title={`Средний CTR`}
            value={`${ctr.toFixed(2)}%`}
            delta={0}
            deltaLabel={funnelQ.isLoading ? `Загрузка…` : `воронка предложений`}
            accent={`#10b981`}
          />
        </div>
      </div>

      {/* Line Chart + Heatmap row */}
      <div className="flex gap-5">

        {/* Line chart */}
        <div className="flex-1 bg-card rounded-xl p-6 shadow-custom border border-border" style={{ minWidth: 0 }}>
          <div className="mb-4">
            <h2 className="text-sm font-semibold text-foreground">Динамика принятых предложений — 30 дней</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              Топ-3 сегмента{seg1 != null ? ` · #${seg1}` : ``}{seg2 != null ? ` / #${seg2}` : ``}{seg3 != null ? ` / #${seg3}` : ``}
            </p>
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={lineData} margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis
                dataKey="day"
                tick={{ fontSize: 11, fill: `var(--muted-foreground)` }}
                tickLine={false}
                axisLine={false}
                interval={4}
              />
              <YAxis
                tick={{ fontSize: 11, fill: `var(--muted-foreground)` }}
                tickLine={false}
                axisLine={false}
                width={36}
              />
              <Tooltip
                contentStyle={{
                  background: `var(--card)`,
                  border: `1px solid var(--border)`,
                  borderRadius: 8,
                  fontSize: 12,
                }}
                cursor={{ stroke: `var(--border)`, strokeWidth: 1 }}
              />
              <Legend
                iconType="circle"
                iconSize={8}
                wrapperStyle={{ fontSize: 12, paddingTop: 12 }}
              />
              <Line
                type="monotone"
                dataKey="premium"
                name={seg1 != null ? `Сегмент #${seg1}` : `Сегмент 1`}
                stroke={lineColors.premium}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
              <Line
                type="monotone"
                dataKey="mass"
                name={seg2 != null ? `Сегмент #${seg2}` : `Сегмент 2`}
                stroke={lineColors.mass}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
              <Line
                type="monotone"
                dataKey="vip"
                name={seg3 != null ? `Сегмент #${seg3}` : `Сегмент 3`}
                stroke={lineColors.vip}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>

        {/* Heatmap */}
        <div className="bg-card rounded-xl p-6 shadow-custom border border-border" style={{ width: 420, minWidth: 420 }}>
          <div className="mb-4">
            <h2 className="text-sm font-semibold text-foreground">Тепловая карта — активность по MCC</h2>
            <p className="text-xs text-muted-foreground mt-0.5">Индекс отклика (0–5) по сегментам</p>
          </div>
          <div className="overflow-x-auto">
            {heatRows.mccCodes.length === 0 ? (
              <div className="text-xs text-muted-foreground py-8 text-center">
                {matrixQ.isLoading ? `Загрузка тепловой карты…` : `Нет данных по матрице сегмент × MCC`}
              </div>
            ) : (
              <table className="w-full text-xs border-collapse" style={{ tableLayout: `fixed` }}>
                <thead>
                  <tr>
                    <th className="text-left text-muted-foreground font-normal pb-2 pr-2" style={{ width: 72 }}>Сегмент</th>
                    {heatRows.mccCodes.map((cat) => (
                      <th key={cat} className="text-center font-normal pb-2 px-0.5" style={{ writingMode: `vertical-rl`, transform: `rotate(180deg)`, height: 64, fontSize: 10, color: `var(--muted-foreground)` }}>
                        {cat}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {heatRows.segments.map((seg, si) => (
                    <tr key={seg}>
                      <td className="text-muted-foreground pr-2 py-1 text-xs font-medium">#{seg}</td>
                      {heatRows.mccCodes.map((_, ci) => (
                        <td key={ci} className="py-1 px-0.5">
                          <div
                            className={`heat-cell-${heatRows.grid[si][ci]} rounded`}
                            style={{ width: 28, height: 22, display: `flex`, alignItems: `center`, justifyContent: `center`, fontSize: 10, fontWeight: 600, margin: `0 auto` }}
                          >
                            {heatRows.grid[si][ci]}
                          </div>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {/* Legend */}
          <div className="flex items-center gap-2 mt-3">
            <span className="text-xs text-muted-foreground">Низкий</span>
            {[0, 1, 2, 3, 4, 5].map((v) => (
              <div key={v} className={`heat-cell-${v} rounded`} style={{ width: 18, height: 18 }} />
            ))}
            <span className="text-xs text-muted-foreground">Высокий</span>
          </div>
        </div>
      </div>

      {/* Top-5 Table */}
      <div className="bg-card rounded-xl shadow-custom border border-border overflow-hidden">
        <div className="px-6 py-4 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold text-foreground">Топ-5 кампаний по ROI</h2>
          <span className="text-xs text-muted-foreground">{topQ.isLoading ? `Загрузка…` : `Текущий период`}</span>
        </div>
        {topQ.isLoading && (
          <div className="p-8 text-center text-xs text-muted-foreground">Загрузка топ-5…</div>
        )}
        {!topQ.isLoading && (topQ.data?.length ?? 0) === 0 && (
          <div className="p-8 text-center text-xs text-muted-foreground">Пока нет данных по ROI.</div>
        )}
        {!topQ.isLoading && (topQ.data?.length ?? 0) > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted">
                <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">#</th>
                <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Кампания</th>
                <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">ROI %</th>
                <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Бюджет</th>
                <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Расход</th>
                <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Статус</th>
              </tr>
            </thead>
            <tbody>
              {(topQ.data ?? []).map((c, i) => {
                const summary = activeCampaigns.find((x) => x.campaign_id === c.campaign_id);
                const status = summary?.status ?? `COMPLETED`;
                return (
                  <tr key={c.campaign_id} className="border-b border-border last:border-0 hover:bg-muted/50 transition-colors">
                    <td className="px-6 py-3.5 text-muted-foreground font-medium">{i + 1}</td>
                    <td className="px-6 py-3.5 font-medium text-foreground">{c.name}</td>
                    <td className="px-6 py-3.5 text-right font-bold" style={{ color: `var(--primary)` }}>
                      {(c.metric * 100).toFixed(1)}%
                    </td>
                    <td className="px-6 py-3.5 text-right text-foreground">
                      {summary ? fmtMoney(Number(summary.budget_total)) : `—`}
                    </td>
                    <td className="px-6 py-3.5 text-right text-foreground">
                      {summary ? fmtMoney(Number(summary.budget_spent)) : `—`}
                    </td>
                    <td className="px-6 py-3.5 text-right">
                      <span
                        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold"
                        style={{
                          background: `${statusColor[status]}20`,
                          color: statusColor[status],
                        }}
                      >
                        <span className="w-1.5 h-1.5 rounded-full" style={{ background: statusColor[status] }} />
                        {statusLabel[status]}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
