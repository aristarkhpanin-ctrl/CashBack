/**
 * Pill-shaped badge that surfaces the z-test verdict from the backend.
 *
 *   significant → green, "Статистически значимо"
 *   trending    → amber, "Тенденция"
 *   no_data     → grey,  "Недостаточно данных"
 */

interface Props {
  significance: 'significant' | 'trending' | 'no_data';
  pValue?: number | null;
}

const styles = {
  significant: { bg: '#10b98120', fg: '#10b981', label: 'Статистически значимо' },
  trending:    { bg: '#f59e0b20', fg: '#f59e0b', label: 'Тенденция' },
  no_data:     { bg: 'var(--muted)', fg: 'var(--muted-foreground)', label: 'Недостаточно данных' },
} as const;

export default function SignificanceBadge({ significance, pValue }: Props) {
  const style = styles[significance];
  return (
    <span
      data-cmp="SignificanceBadge"
      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold"
      style={{ background: style.bg, color: style.fg }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: style.fg }} />
      {style.label}
      {pValue != null && significance !== 'no_data' && (
        <span className="opacity-80">· p = {pValue.toExponential(2)}</span>
      )}
    </span>
  );
}
