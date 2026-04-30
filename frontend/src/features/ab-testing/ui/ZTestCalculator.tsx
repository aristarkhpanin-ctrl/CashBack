/**
 * Stand-alone two-proportion z-test calculator (client-side).
 *
 * Lets the user plug in expected sample sizes / conversions before
 * launching an experiment, and previews z, p-value and 95% CI.
 */
import { useState } from 'react';
import { clientZTest } from '@/features/ab-testing/api/abApi';
import SignificanceBadge from './SignificanceBadge';

function pct(v: number): string {
  return `${(v * 100).toFixed(2)}%`;
}

export default function ZTestCalculator() {
  const [nA,  setNA]  = useState('1000');
  const [sA,  setSA]  = useState('80');
  const [nB,  setNB]  = useState('1000');
  const [sB,  setSB]  = useState('110');

  const result = clientZTest(
    Number(sA), Number(nA), Number(sB), Number(nB), 0.05,
  );

  function badge() {
    if (!result) return 'no_data' as const;
    if (result.pValue < 0.05) return 'significant' as const;
    if (result.pValue < 0.20) return 'trending' as const;
    return 'no_data' as const;
  }

  return (
    <div
      data-cmp="ZTestCalculator"
      className="bg-card rounded-xl shadow-custom border border-border p-6"
    >
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-foreground">Калькулятор z-теста</h2>
        <p className="text-xs text-muted-foreground mt-0.5">
          Two-proportion z-test для сравнения групп A и B (α = 0.05).
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div className="flex flex-col gap-2">
          <span className="text-xs text-muted-foreground uppercase tracking-wider">Group A (control)</span>
          <label className="text-xs text-muted-foreground">Размер выборки nₐ</label>
          <input
            type="number"
            value={nA}
            onChange={(e) => setNA(e.target.value)}
            className="px-3 py-2 text-sm border border-border rounded-lg bg-background"
            min={0}
          />
          <label className="text-xs text-muted-foreground">Конверсий sₐ</label>
          <input
            type="number"
            value={sA}
            onChange={(e) => setSA(e.target.value)}
            className="px-3 py-2 text-sm border border-border rounded-lg bg-background"
            min={0}
          />
        </div>
        <div className="flex flex-col gap-2">
          <span className="text-xs text-muted-foreground uppercase tracking-wider">Group B (treatment)</span>
          <label className="text-xs text-muted-foreground">Размер выборки n_b</label>
          <input
            type="number"
            value={nB}
            onChange={(e) => setNB(e.target.value)}
            className="px-3 py-2 text-sm border border-border rounded-lg bg-background"
            min={0}
          />
          <label className="text-xs text-muted-foreground">Конверсий s_b</label>
          <input
            type="number"
            value={sB}
            onChange={(e) => setSB(e.target.value)}
            className="px-3 py-2 text-sm border border-border rounded-lg bg-background"
            min={0}
          />
        </div>
      </div>

      <div className="mt-5 pt-4 border-t border-border">
        {result ? (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">CR(A) / CR(B)</span>
              <span className="font-medium text-foreground">
                {pct(result.pA)} → {pct(result.pB)}
              </span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">Lift Δ</span>
              <span
                className="font-bold"
                style={{ color: result.diff >= 0 ? '#10b981' : '#ef4444' }}
              >
                {result.diff >= 0 ? '+' : ''}{pct(result.diff)}
              </span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">95% CI</span>
              <span className="font-medium text-foreground">
                [{pct(result.ci[0])} … {pct(result.ci[1])}]
              </span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">z / p-value</span>
              <span className="font-medium text-foreground">
                z = {result.z.toFixed(3)} · p = {result.pValue.toExponential(2)}
              </span>
            </div>
            <div className="mt-2">
              <SignificanceBadge significance={badge()} pValue={result.pValue} />
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Введите ненулевые размеры выборок.</p>
        )}
      </div>
    </div>
  );
}
