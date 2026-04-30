import { useState } from 'react';
import { Beaker, ChevronRight, FlaskConical } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { abApi, abKeys } from '@/features/ab-testing/api/abApi';
import SignificanceBadge from '@/features/ab-testing/ui/SignificanceBadge';
import VariantComparisonTable from '@/features/ab-testing/ui/VariantComparisonTable';
import ZTestCalculator from '@/features/ab-testing/ui/ZTestCalculator';
import type { ABStatus } from '@/shared/api/types';

const statusStyle: Record<ABStatus, { bg: string; fg: string; label: string }> = {
  DRAFT:   { bg: '#94a3b820', fg: '#64748b', label: 'Черновик' },
  ACTIVE:  { bg: '#10b98120', fg: '#10b981', label: 'Активен' },
  STOPPED: { bg: '#f59e0b20', fg: '#f59e0b', label: 'Остановлен' },
};

export default function Experiments() {
  const [tab, setTab] = useState<'list' | 'calculator'>('list');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list = useQuery({
    queryKey: abKeys.list,
    queryFn: abApi.list,
  });

  const detail = useQuery({
    enabled: !!selectedId,
    queryKey: selectedId ? abKeys.results(selectedId) : ['ab', 'noop'],
    queryFn: () => abApi.getResults(selectedId as string),
  });

  return (
    <div data-cmp="Experiments" className="p-8 flex flex-col gap-6">
      {/* Tabs */}
      <div className="bg-card rounded-xl border border-border p-1 inline-flex gap-1 self-start">
        {(
          [
            { id: 'list', label: 'Эксперименты', icon: FlaskConical },
            { id: 'calculator', label: 'z-test калькулятор', icon: Beaker },
          ] as const
        ).map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              background: tab === id ? 'var(--primary)' : 'transparent',
              color: tab === id ? '#fff' : 'var(--muted-foreground)',
            }}
          >
            <Icon size={15} />
            {label}
          </button>
        ))}
      </div>

      {tab === 'calculator' && <ZTestCalculator />}

      {tab === 'list' && (
        <>
          {/* List */}
          <div className="bg-card rounded-xl shadow-custom border border-border overflow-hidden">
            <div className="px-6 py-4 border-b border-border flex items-center justify-between">
              <h2 className="text-sm font-semibold text-foreground">Список экспериментов</h2>
              <span className="text-xs text-muted-foreground">
                {list.data?.length ?? 0} активных / черновых
              </span>
            </div>

            {list.isLoading && (
              <div className="p-10 text-center text-sm text-muted-foreground">Загрузка…</div>
            )}

            {!list.isLoading && (list.data?.length ?? 0) === 0 && (
              <div className="p-10 text-center text-sm text-muted-foreground">
                Нет экспериментов. Запустите тест через POST /experiments.
              </div>
            )}

            {(list.data?.length ?? 0) > 0 && (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-muted">
                    <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Название
                    </th>
                    <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Метрика
                    </th>
                    <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Старт
                    </th>
                    <th className="text-left px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Варианты
                    </th>
                    <th className="text-right px-6 py-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Статус
                    </th>
                    <th className="px-6 py-3" />
                  </tr>
                </thead>
                <tbody>
                  {list.data?.map((exp) => {
                    const st = statusStyle[exp.status];
                    return (
                      <tr
                        key={exp.experiment_id}
                        className="border-b border-border last:border-0 hover:bg-muted/50 transition-colors cursor-pointer"
                        onClick={() => setSelectedId(exp.experiment_id)}
                      >
                        <td className="px-6 py-3.5 font-medium text-foreground">{exp.name}</td>
                        <td className="px-6 py-3.5 text-muted-foreground">{exp.target_metric}</td>
                        <td className="px-6 py-3.5 text-muted-foreground">
                          {new Date(exp.start_date).toLocaleDateString()}
                        </td>
                        <td className="px-6 py-3.5 text-muted-foreground">
                          {exp.variants.map((v) => v.name).join(' / ')}
                        </td>
                        <td className="px-6 py-3.5 text-right">
                          <span
                            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold"
                            style={{ background: st.bg, color: st.fg }}
                          >
                            <span className="w-1.5 h-1.5 rounded-full" style={{ background: st.fg }} />
                            {st.label}
                          </span>
                        </td>
                        <td className="px-6 py-3.5 text-right text-muted-foreground">
                          <ChevronRight size={16} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>

          {/* Selected experiment results */}
          {selectedId && (
            <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-foreground">
                  Результаты эксперимента
                </h2>
                {detail.data && (
                  <SignificanceBadge
                    significance={detail.data.significance}
                    pValue={detail.data.p_value}
                  />
                )}
              </div>
              {detail.isLoading && (
                <div className="p-6 text-sm text-muted-foreground">Загрузка результатов…</div>
              )}
              {detail.data && <VariantComparisonTable results={detail.data} />}
            </div>
          )}
        </>
      )}
    </div>
  );
}
