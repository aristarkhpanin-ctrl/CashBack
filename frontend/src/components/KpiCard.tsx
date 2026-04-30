import { TrendingUp, TrendingDown } from 'lucide-react';

interface KpiCardProps {
  title?: string;
  value?: string;
  subValue?: string;
  delta?: number;
  deltaLabel?: string;
  accent?: string;
}

export default function KpiCard({
  title = `Показатель`,
  value = `0`,
  subValue = ``,
  delta = 0,
  deltaLabel = `vs прошлый период`,
  accent = `var(--primary)`,
}: KpiCardProps) {
  const isPositive = delta >= 0;
  return (
    <div
      data-cmp="KpiCard"
      className="bg-card rounded-xl p-5 flex flex-col gap-3 shadow-custom border border-border"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">{title}</span>
        <span className="w-2 h-2 rounded-full" style={{ background: accent }} />
      </div>
      <div>
        <span className="text-3xl font-bold text-foreground">{value}</span>
        {subValue && <span className="ml-2 text-sm text-muted-foreground">{subValue}</span>}
      </div>
      <div className="flex items-center gap-1.5">
        {isPositive
          ? <TrendingUp size={13} style={{ color: `#10b981` }} />
          : <TrendingDown size={13} style={{ color: `var(--destructive)` }} />
        }
        <span className="text-xs font-medium" style={{ color: isPositive ? `#10b981` : `var(--destructive)` }}>
          {isPositive ? `+` : ``}{delta}%
        </span>
        <span className="text-xs text-muted-foreground">{deltaLabel}</span>
      </div>
    </div>
  );
}
