/**
 * /recommendations/:id/explain — SHAP breakdown for a single recommendation.
 *
 * Loads recommendations for the user (via the `?user=` query parameter)
 * and renders the matching item's SHAP top-factors via
 * `ShapExplanationCard`.
 */
import { useMemo } from 'react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ChevronLeft } from 'lucide-react';
import {
  recommendationsApi, recommendationKeys,
} from '@/features/recommendations/api/recommendationsApi';
import ShapExplanationCard from '@/features/recommendations/ui/ShapExplanationCard';

export default function ExplainRecommendation() {
  const params = useParams<{ id: string }>();
  const [search] = useSearchParams();
  const userId = search.get('user') ?? '';
  const recId = params.id ?? '';
  const topK = 10;

  const enabled = userId.length > 0;

  const query = useQuery({
    enabled,
    queryKey: recommendationKeys.forUser(userId, topK),
    queryFn: () => recommendationsApi.forUser(userId, topK),
  });

  const target = useMemo(() => {
    if (!query.data) return null;
    // Match by mcc_code as recommendation_id is not returned by the API yet;
    // we store the mcc on the URL as fallback. Keep the simple lookup.
    return query.data.recommendations.find(
      (r) => r.mcc_code === recId || r.campaign_id === recId,
    ) ?? query.data.recommendations[0] ?? null;
  }, [query.data, recId]);

  return (
    <div data-cmp="ExplainRecommendation" className="p-8 flex flex-col gap-6">
      <Link
        to="/"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronLeft size={14} />
        Назад
      </Link>

      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-bold text-foreground">Объяснение рекомендации</h2>
        <p className="text-sm text-muted-foreground">
          {userId
            ? `Пользователь ${userId} · MCC ${target?.mcc_code ?? recId}`
            : 'Передайте user_id через параметр ?user=… в URL'}
        </p>
      </div>

      {!enabled && (
        <div className="bg-card rounded-xl border border-border p-6 text-sm text-muted-foreground">
          В URL не указан <code>?user=&lt;uuid&gt;</code>. Откройте /recommendations/{'<id>'}/explain?user=&lt;uuid&gt;
        </div>
      )}

      {enabled && query.isLoading && (
        <div className="bg-card rounded-xl border border-border p-6 text-sm text-muted-foreground">
          Загрузка рекомендаций…
        </div>
      )}

      {enabled && query.error && (
        <div className="bg-card rounded-xl border border-border p-6 text-sm text-destructive">
          Не удалось получить данные рекомендаций.
        </div>
      )}

      {target && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <ShapExplanationCard
            title="Топ-факторов модели"
            subtitle={`Score = ${target.score.toFixed(4)} · MCC ${target.mcc_code}`}
            factors={target.top_factors ?? {}}
            topN={10}
          />
          <div className="bg-card rounded-xl shadow-custom border border-border p-6">
            <h2 className="text-sm font-semibold text-foreground">Контекст рекомендации</h2>
            <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
              <dt className="text-muted-foreground">User ID</dt>
              <dd className="font-mono text-foreground break-all">{userId}</dd>
              <dt className="text-muted-foreground">MCC</dt>
              <dd className="font-medium text-foreground">{target.mcc_code}</dd>
              <dt className="text-muted-foreground">Campaign ID</dt>
              <dd className="font-mono text-foreground break-all">{target.campaign_id ?? '—'}</dd>
              <dt className="text-muted-foreground">Model version</dt>
              <dd className="font-medium text-foreground">{query.data?.model_version ?? '—'}</dd>
              <dt className="text-muted-foreground">Score</dt>
              <dd className="font-bold" style={{ color: 'var(--primary)' }}>
                {target.score.toFixed(4)}
              </dd>
            </dl>
          </div>
        </div>
      )}
    </div>
  );
}
