# 0012 — Feature-Sliced Design (FSD-lite) for the frontend

* **Status:** Accepted
* **Date:** 2025-12-22

## Context

The admin UI has four heterogeneous areas (Dashboard, Campaigns,
Analytics, Experiments) plus an Explain-recommendation page. Each area
queries different backend services (recommendation API + campaign
manager + mobile BFF) and renders different visualisations (Recharts
line / heatmap / funnel / SHAP bar chart). Shared concerns (HTTP client,
toasts, types, query keys) should not leak into pages.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **Feature-Sliced Design (lite)** | Clear boundaries (`shared` / `features` / `pages`); scales with team size; easy onboarding. | Some boilerplate. |
| Atomic design | Component-centric; great for design systems. | Doesn't help with per-feature backend wiring. |
| Domain folders (e.g. `src/recommendations/`) | Simple. | Tends to mix UI + API + state in one folder; cross-feature reuse becomes ad-hoc. |
| Flat `pages/` only | Fastest to start. | Already painful at four pages × three APIs. |

## Decision

We adopt a **Feature-Sliced Design–lite** layout (no `entities` slice;
the project is small enough for `shared / features / pages`):

```text
frontend/src/
├── shared/
│   ├── api/             # axios client, types, generated/  (1 source of truth)
│   └── hooks/           # useDebounced, ...
├── features/
│   ├── campaigns/       # api/campaignApi.ts (+ ui/ when needed)
│   ├── analytics/       # api/analyticsApi.ts
│   ├── ab-testing/      # api/abApi.ts + ui/{SignificanceBadge, VariantComparisonTable, ZTestCalculator}
│   └── recommendations/ # api/recommendationsApi.ts + ui/ShapExplanationCard
├── store/               # zustand wizardStore (cross-page state)
├── components/          # KpiCard, Sidebar, TopBar, ui/* (shadcn primitives)
├── pages/               # Dashboard / Campaigns / Analytics / Experiments / ExplainRecommendation
├── App.tsx              # router + QueryClientProvider + middleware
└── main.tsx
```

Conventions:

* A `pages/X.tsx` only imports from `features/*`, `components/*`,
  `shared/*` — never from another page or another feature's internals.
* Each `features/X/api/<X>Api.ts` exports a typed client and a
  `<X>Keys` query-key factory consumed by TanStack Query.
* TS types live in `shared/api/types.ts` (hand-written) and
  `shared/api/generated/` (auto via `scripts/generate-api-types.sh`).
* Cross-feature mutable state belongs in `store/` (zustand) — only the
  Campaigns Wizard needs it (5-step form survives navigation).

## Consequences

* + Adding a new page (e.g. `/cohorts`) is a copy-paste of the
  Analytics blueprint — feature folder + page + route in `App.tsx`.
* + The HTTP layer is mockable: ASGI tests in `tests/integration/`
  swap `app.state.recommendation_client` for a fake; same on the
  React side via TanStack-Query test utilities.
* + Lint enforces `import` boundaries (we keep this informal for now;
  adding `eslint-plugin-boundaries` is on the roadmap).
* − Onboarding cost — 30 minutes to read this ADR + pages.
* − Some duplication in tiny apps (worth it for the larger areas).

## References

* `frontend/src/`
* `frontend/src/shared/api/client.ts` — single axios + sonner gateway.
* `frontend/src/store/wizardStore.ts` — zustand store.
* `frontend/src/features/recommendations/ui/ShapExplanationCard.tsx` —
  bidirectional SHAP visualisation per chapter 3.2.
