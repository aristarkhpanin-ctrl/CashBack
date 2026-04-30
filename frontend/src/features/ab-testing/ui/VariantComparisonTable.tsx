/**
 * Side-by-side table that compares the control / treatment variants of an
 * experiment. Rows are styled to match the existing dashboard tables.
 */
import type { ABResults } from '@/shared/api/types';

interface Props {
  results: ABResults;
}

function pct(v: number): string {
  return `${(v * 100).toFixed(2)}%`;
}

export default function VariantComparisonTable({ results }: Props) {
  const { control, treatment, diff, confidence_interval: ci, z, p_value } = results;
  const liftSign = diff > 0 ? '+' : '';

  return (
    <div
      data-cmp="VariantComparisonTable"
      className="bg-card rounded-xl shadow-custom border border-border overflow-hidden"
    >
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted">
            <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Метрика
            </th>
            <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Control · {control.name}
            </th>
            <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Treatment · {treatment.name}
            </th>
            <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Lift
            </th>
          </tr>
        </thead>
        <tbody>
          <tr className="border-b border-border">
            <td className="px-6 py-3 text-muted-foreground">Размер выборки (n)</td>
            <td className="px-6 py-3 text-right font-medium text-foreground">{control.n.toLocaleString()}</td>
            <td className="px-6 py-3 text-right font-medium text-foreground">{treatment.n.toLocaleString()}</td>
            <td className="px-6 py-3 text-right text-muted-foreground">—</td>
          </tr>
          <tr className="border-b border-border">
            <td className="px-6 py-3 text-muted-foreground">Конверсии</td>
            <td className="px-6 py-3 text-right font-medium text-foreground">{control.successes.toLocaleString()}</td>
            <td className="px-6 py-3 text-right font-medium text-foreground">{treatment.successes.toLocaleString()}</td>
            <td className="px-6 py-3 text-right text-muted-foreground">—</td>
          </tr>
          <tr className="border-b border-border">
            <td className="px-6 py-3 text-muted-foreground">Conversion rate</td>
            <td className="px-6 py-3 text-right font-bold text-foreground">{pct(control.rate)}</td>
            <td className="px-6 py-3 text-right font-bold text-foreground">{pct(treatment.rate)}</td>
            <td className="px-6 py-3 text-right font-bold" style={{ color: diff >= 0 ? '#10b981' : '#ef4444' }}>
              {liftSign}{pct(diff)}
            </td>
          </tr>
          <tr className="border-b border-border last:border-0">
            <td className="px-6 py-3 text-muted-foreground">95% доверительный интервал (Δ)</td>
            <td colSpan={3} className="px-6 py-3 text-right font-medium text-foreground">
              {ci ? `[${pct(ci[0])} … ${pct(ci[1])}]` : '—'}
            </td>
          </tr>
          <tr>
            <td className="px-6 py-3 text-muted-foreground">z / p-value</td>
            <td colSpan={3} className="px-6 py-3 text-right font-medium text-foreground">
              {z != null ? `z = ${z.toFixed(3)}` : 'z = —'}
              {' · '}
              {p_value != null ? `p = ${p_value.toExponential(2)}` : 'p = —'}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
