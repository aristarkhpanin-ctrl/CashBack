/**
 * ShapExplanationCard
 * -------------------
 * Bidirectional bar chart of SHAP top-N contributions.
 *   * positive contributions → primary blue
 *   * negative contributions → red (#ef4444)
 *
 * The chart sorts features by absolute impact and centres the X axis at 0
 * so positive and negative bars extend from a shared zero baseline.
 */
import {
  Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts';

interface FactorRow {
  feature: string;
  value: number;
}

interface Props {
  title?: string;
  subtitle?: string;
  factors: Record<string, number>;
  /** Cap the number of bars; default 10. */
  topN?: number;
  height?: number;
}

const POS_COLOR = 'var(--primary)';
const NEG_COLOR = '#ef4444';

export default function ShapExplanationCard({
  title = 'SHAP-факторы',
  subtitle = 'Положительные значения — рост вероятности принятия, отрицательные — снижение',
  factors,
  topN = 10,
  height = 320,
}: Props) {
  const rows: FactorRow[] = Object.entries(factors)
    .map(([feature, value]) => ({ feature, value: Number(value) }))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
    .slice(0, topN);

  const maxAbs = rows.length
    ? Math.max(...rows.map((r) => Math.abs(r.value)))
    : 1;
  const domain: [number, number] = [-maxAbs * 1.1, maxAbs * 1.1];

  if (rows.length === 0) {
    return (
      <div
        className="bg-card rounded-xl shadow-custom border border-border p-6"
        data-cmp="ShapExplanationCard"
      >
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        <p className="text-xs text-muted-foreground mt-1">Нет данных</p>
      </div>
    );
  }

  return (
    <div
      className="bg-card rounded-xl shadow-custom border border-border p-6"
      data-cmp="ShapExplanationCard"
    >
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        <p className="text-xs text-muted-foreground mt-0.5">{subtitle}</p>
      </div>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart
          data={rows}
          layout="vertical"
          margin={{ top: 8, right: 32, left: 12, bottom: 8 }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" horizontal={false} />
          <XAxis
            type="number"
            domain={domain}
            tick={{ fontSize: 11, fill: 'var(--muted-foreground)' }}
            tickLine={false}
            axisLine={{ stroke: 'var(--border)' }}
          />
          <YAxis
            type="category"
            dataKey="feature"
            width={150}
            tick={{ fontSize: 11, fill: 'var(--foreground)' }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            cursor={{ fill: 'var(--accent)' }}
            contentStyle={{
              background: 'var(--card)',
              border: '1px solid var(--border)',
              borderRadius: 8,
              fontSize: 12,
            }}
            formatter={(v: number) => v.toFixed(4)}
          />
          <ReferenceLine x={0} stroke="var(--border)" strokeWidth={1} />
          <Bar dataKey="value" radius={[3, 3, 3, 3]}>
            {rows.map((row, idx) => (
              <Cell
                key={idx}
                fill={row.value >= 0 ? POS_COLOR : NEG_COLOR}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      <div className="flex items-center gap-4 mt-3 pt-3 border-t border-border text-xs">
        <div className="flex items-center gap-1.5">
          <span
            className="inline-block w-3 h-3 rounded"
            style={{ background: POS_COLOR }}
          />
          <span className="text-muted-foreground">Повышает вероятность</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-3 rounded" style={{ background: NEG_COLOR }} />
          <span className="text-muted-foreground">Снижает вероятность</span>
        </div>
      </div>
    </div>
  );
}
