# 0015 — Analytics definitions: one meaning per number, settlement hold, honest trends and A/B verdicts

* **Status:** Accepted
* **Date:** 2026-10-24
* **Fixes:** B9, B33, B11, B10, B34, B29, B50, B51 (`docs/AUDIT.md`), stage D

## Context

The audit found that the analytics pages showed numbers nobody could defend:

* **B9** — three endpoints (`/analytics/daily-trend`, `/analytics/channels`,
  `/analytics/cohort-retention`) answered 500 on every call: `:param::type`
  inside SQLAlchemy `text()` is not a bind parameter, and `(:periods || '
  months')::interval` is `int || text` for asyncpg. The nightly smoke did not
  notice: `curl -sf … | head` under `bash -e` without `pipefail` passes on a
  500.
* **B33** — the funnel mixed units (users → recommendation rows → accrual
  rows), so drop-off went negative and "transacted" exceeded "accepted";
  EXPIRED offers counted as "opened"; the "paid" stage was always 0 because
  nothing ever set `PAID`; the channel was written at response time, so the
  open rate per channel was 100 % by construction; `has_data` used `and`;
  cohorts folded month 12+ into 0–11; `conversion_rate` was a copy of `ctr`;
  without running campaigns the KPIs silently used all historical campaigns.
* **B11** — KPI trends compared the window with an almost empty previous one
  (▲1286 %, ▲1940 %), labelled "vs прошлый месяц" whatever the period.
* **B29** — every served offer was stored twice: recommendation_api wrote a
  row with `model_version`, mobile_api wrote another without it and handed
  *its* id to the app. Responses updated only the mobile row, so the online
  CTR per model version (`ml_online_ctr`) and the holdout comparison never had
  data, and every count over `recommendations` was doubled.
* **B34** — the A/B control was the first variant by name (renaming a variant
  flipped the sign of the effect), `successes` counted CONVERSION events
  (a rate above 1 → `math.sqrt` of a negative → 500), the verdict
  "significant" had no direction, `assign` did not check the experiment's
  status and raced into a 500, a duplicate experiment name was a 500.
* **B10 / B50 / B51** — in live mode the UI turned backend errors into "no
  data yet" and filled the gap with demo numbers (`seededRng(campaign.id…)` →
  NaN, a mock heatmap labelled "из API", "Конверсия" = 0.65 × CTR), the A/B
  page said "тест лучше" for a significantly worse treatment, a draft edit
  overwrote fields the user had not touched, SHAP log-odds were drawn as
  probability points.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| Patch each formula where it is wrong | Smallest diff | No shared meaning of "opened" or "paid" — exactly how the endpoints drifted apart |
| Separate analytics store (ClickHouse marts fed from Postgres) | Fast at scale, history | A second copy of the truth with its own lag and bugs; the thesis demo is small |
| **Written definitions (this ADR) implemented once in campaign_manager's SQL over the OLTP tables, one recommendation row per served offer, every SQL tested against real Postgres with hand-computed numbers (chosen)** | Every number has one meaning the UI, the docs and the tests share; the data model makes the definitions possible | Analytics queries run on the OLTP database (two new indexes); definitions change only together with this ADR |

For **"paid"**: renaming the stage to "accrued" was rejected — the mobile
balance already distinguishes pending from paid money, and a real bank
integration is out of scope. The settlement hold is modelled instead (below).
For **the A/B control**: "first by name" and "named `control`" were rejected —
both make the verdict depend on a label; the control is now a stored flag.

## Decision

### Definitions (used by the backend, the UI copy, the tests and this document)

| Term | Meaning |
|------|---------|
| **delivered** | a `recommendations` row exists — one row per served offer (B29) |
| **opened / responded** | `response_status IN ('ACCEPTED','DECLINED','SNOOZE')`; `EXPIRED` and `PENDING` are not opened. `SNOOZE` stays the stored answer: mobile_api no longer resets a snoozed row to `PENDING` after `snooze_days` (that reset only erased the answer — since B29 nothing re-serves an old row); `snooze_until` is the moment the app reminds the client, the offer stays answerable until `expires_at` |
| **accepted** | `response_status = 'ACCEPTED'` |
| **transacted** | the offer has a `cashback_accruals` row (`recommendation_id`), any status |
| **paid** | that accrual is `PAID` (set by the settlement job) |

Every **funnel** stage counts **distinct users** (`COUNT(DISTINCT user_id)`)
in the selected window / segment / campaign, so each stage after "received"
is a subset of the previous one; drop-off is clamped to [0, 100] (the target
audience is today's segment membership, which an offer of the window may
predate).

### One row per served offer, delivery channel (B29)

* recommendation_api is the **only** writer of served-offer rows: one row per
  returned item, in one transaction, with `model_version` and `expires_at =
  LEAST(campaign end_date, now() + OFFER_TTL_DAYS)` (default 7). Each
  `RecommendationItem` carries the row's `recommendation_id` and `expires_at`;
  the `recommendations.created` event carries `recommendation_id` and the real
  `campaign_name`. If the rows cannot be stored the serve fails (503): an offer
  without a row could be neither accepted nor counted. The BRE marks R3
  (anti-fatigue key) and R4 (24 h counter) set during evaluation are taken
  back for every offer that was not stored (503, or its campaign row is gone),
  so an offer the client never got does not hide the campaign for
  `ANTI_FATIGUE_TTL_DAYS`. `/explain` writes nothing (both fields null).
* mobile_api writes no rows: it maps the upstream items 1:1, returns their
  `recommendation_id` and the offer's own `expires_at`, and marks the served
  rows `channel = 'IN_APP'` where `channel IS NULL` (one statement).
* `channel` is the **delivery** channel: the transaction_listener notification
  pipeline writes it after a successful dispatch (`… SET channel = :ch WHERE
  recommendation_id = :rid AND channel IS NULL`; adapter push/in_app/email/sms
  → enum PUSH/IN_APP/EMAIL/SMS, metric `notification_channel_recorded_total`).
  The first recorded channel wins. The respond call no longer writes
  `channel` — the channel the client names goes to the log only.
* `ml_online_ctr{model_version}` (campaign_manager job `export-online-ctr`) =
  accepted / responded offers generated in the last 7 days, per
  `model_version`. Rows without a version (demo data, pre-B29 rows) are not a
  model's serve and are left out.

### Endpoints (`GET /analytics/*`, permission `analytics`)

* **B9 rule:** no `:param::type` in `text()` — `CAST(:p AS uuid)`, `CAST(:deciles
  AS int[])`, `make_interval(months => :periods)`.
* **funnel** — `target_audience` (users of the campaign's target segments;
  empty list = all users, as `eligible_campaigns`; unknown campaign = 0; no
  campaign = all users; narrowed by the segment filter) → `received` →
  `opened` → `accepted` → `transacted` → `cashback_paid`, all distinct users
  over the offers generated in the window; `transacted` / `cashback_paid` are
  accruals linked by `recommendation_id` to the *accepted offers of the
  window* (a purchase on some other offer does not count). `pending` keeps
  its meaning: delivered, but no response yet.
* **kpis** — campaigns = the given one, or ACTIVE + PAUSED non-archived ones;
  with none and no filter: zeros and `has_data = false` (no fallback to
  history). `reach` = distinct users delivered in the window, `avg_ctr` =
  accepted / delivered offers, `spent` = Σ `budget_spent` of those campaigns
  (all time — shown next to their budget). `trends.spent`, however, compares
  the accruals of the window with the previous window, so the UI labels it
  "начисления за N дн. vs предыдущие N дней", not as growth of `spent`.
  `has_data = spent > 0 OR avg_ctr > 0 OR delivered > 0`. Its scope is the
  running campaigns: the analytics page takes "has data" for its summary from
  the funnel it is built from (a window whose offers belong to a campaign that
  has since completed has data in the funnel, `has_data = false` here).
* **channels** — per delivery channel (NULL → `UNKNOWN`): `sent` = delivered
  rows, `opened`, `accepted`, `converted` (accepted offers with an accrual).
  The open rate is below 100 % unless every offer was answered.
* **cohort-retention** — cohort = users whose `created_at` falls in
  `cohort_month`; period = `years * 12 + months` of the age between the
  accrual's month and the cohort month; `periods` up to 24; `active_users` =
  distinct users with an accrual in that month; `cohort_month` must be a real
  month (`YYYY-MM`, 01–12).
* **top-campaigns** (all time, distinct users) — `ctr = accepted / delivered`,
  `conversion_rate = transacted / accepted`, `cashback_paid` = Σ PAID
  accruals (the funnel's last stage), `roi` = (revenue − cashback) / cashback.
* **campaign stats** (`GET /campaigns/{id}/stats`, all time — the campaign
  cards and the dashboard's campaign table): `impressions` = delivered offers,
  `reach` = distinct users (the UI's "Охват … клиентов"), `clicks` = opened,
  `accepted` = accepted offers, `ctr` = accepted / delivered offers (as KPI
  `avg_ctr`); `conversion_rate`, `cashback_paid` (PAID only) and `roi` are the
  campaign's values in top-campaigns. Before: "click" = not PENDING (EXPIRED
  counted), `ctr` was the open rate, `conversion_rate` = accepted /
  impressions, `cashback_paid` included PENDING, and the UI showed offers as
  clients.
* **daily-trend** (accepted offers by response day and segment bucket) and
  **segment-matrix** (offers and acceptances per segment × MCC; optional
  `campaign_id` — the analytics page labels the matrix with the selected
  campaign and requests that campaign's offers) keep their meaning; they count
  offers, not users — they are not funnels.

### KPI trends (B11)

* `trends.reach`, `trends.ctr`, `trends.spent` compare the window with the
  previous window of the same length; `trends.reach` is the delta of the same
  metric as `reach` (distinct users).
* A trend is `null` when its base in the previous window is below
  `KPI_TREND_MIN_BASE` (default 20 events) or zero, or the previous value is
  0. The base is the previous window's delivered offers for reach and CTR and
  its accruals for spent. Otherwise the trend is capped to ±999.9 %.
* The response carries `trend_window_days` (= `period`); the UI labels the
  trend "vs предыдущие N дней", shows `null` as "—" and a 0 trend as neutral.

### Settlement: PENDING → PAID models the bank's settlement hold (B33)

The listener books an accrual as `PENDING`. campaign_manager's scheduler job
`settle-accruals` (its own job and failure series
`scheduler_job_failures_total{job="settle-accruals"}`, first run at start,
then every `SCHEDULING_INTERVAL_MINUTES`, default 15) runs

```sql
UPDATE cashback_accruals SET status = 'PAID', paid_at = now()
 WHERE status = 'PENDING'
   AND accrued_at < now() - make_interval(hours => :SETTLEMENT_DELAY_HOURS)
```

with `SETTLEMENT_DELAY_HOURS` from the environment (default 24, ≥ 0). This
stands in for the hold between authorisation and clearing with the bank; there
is no real payout integration. Only the status changes: amounts and
`budget_spent` are untouched, so the stage C invariants (ADR-0014) hold. The
mobile balance (`pending_amount` / `paid_amount` = `available_amount`) follows
the status by itself. Migration 010 adds `paid_at` (backfilled with
`accrued_at` for rows already PAID).

### A/B tests (B34)

* **Control** = the variant with `ab_variants.is_control` (at most one per
  experiment, `uq_ab_variants_control`). On `POST /experiments` the first
  variant is the control unless `is_control` is given explicitly; exactly one
  control is required (422 otherwise), variant names must be unique within the
  request (422), a duplicate experiment name is 409. Migration 010 backfills
  existing experiments: `ab_variants` has no `created_at`, so "created first"
  is the variant's position in `ab_experiments.variants` (the creation body,
  in request order), then the variant named `control`, then the smallest
  `variant_id`.
* **Results** (`GET /experiments/{id}/results`): `n` = assignments per
  variant, `successes` = `COUNT(DISTINCT assignment_id)` with a CONVERSION
  event — one SQL query, no rows loaded; a conversion counts once per
  assignment (`uq_ab_events_conversion`; 010 removes old duplicates, keeping
  the earliest). The treatment is the first non-control variant by name (the
  endpoint compares one pair). Two-proportion z-test, guarded: no test for an
  empty group, a rate outside [0, 1] or zero pooled variance; p = 2·sf(|z|)
  ∈ [0, 1] by construction. An experiment without a control variant → 409.
* **Verdict:** `significance` ∈ {`no_data` (no test, fewer than
  `AB_MIN_OBSERVATIONS` = 200 assignments, or p ≥ `AB_TRENDING_ALPHA` = 0.20),
  `trending` (p < 0.20), `significant` (p < `AB_SIGNIFICANCE_ALPHA` = 0.05)}
  plus `winner` ∈ {`treatment`, `control`, `null`} — the variant with the
  higher rate, `null` without a verdict or without a difference.
  `significant` + `winner = control` means **the test variant is
  significantly worse** ("тест хуже — не катить"); `significant` + `treatment`
  — "тест лучше"; otherwise "разница не значима".
* `POST /experiments/{id}/assign/{user_id}` (permission `campaigns_edit`):
  409 unless the experiment is ACTIVE; idempotent (`INSERT … ON CONFLICT
  (user_id, experiment_id) DO NOTHING`, then read) — a repeat or a race
  returns the one assignment, never a 500.

### The UI shows the backend's numbers or says why it cannot (B10, B50, B51)

Live mode never substitutes mock or demo numbers: each live widget is
`loading`, `empty` or `error` (the HTTP status and a "Повторить" button);
demo data exists only in mock mode. That includes the per-campaign stats (a
failed `/stats` shows "—" and an error with "Повторить" instead of a reach of
0, and a draft whose history is unknown is not offered for deletion), the
segment reference (segment sizes only from `/reference/segments` — while it
loads or fails, the wizard's audience step, the segment filter and the
ML-limit cards say so) and the sidebar's count of active campaigns (shown
only for a loaded list). Trends and the A/B verdict follow the two
sections above. A draft edit sends only the fields the user changed; SHAP
contributions are shown in log-odds units and labelled so. The E2E stub
(`frontend/e2e/stub_backend.py`) mirrors every shape above.

### Demo data and the nightly follow the same definitions

`scripts/seed_demo_data.py` writes what the live path would: one row per
served offer with `expires_at = LEAST(campaign end, generated_at +
OFFER_TTL_DAYS)`, a delivery `channel` on every row and no `model_version`
(no MLflow model served it); a response only between serving and expiry and
not in the future, otherwise `EXPIRED` after the term and `PENDING` before
it; an offer no earlier than the customer's registration and latest consent;
`PAID` (with `paid_at` = end of the hold) only for accruals older than
`SETTLEMENT_DELAY_HOURS`, the rest `PENDING`; customers registered over the
last 18 months (cohorts older than a year); every experiment with an
explicit control (the first variant), its assignments no earlier than the
customer's registration and no event in the future. `OFFER_TTL_DAYS` /
`SETTLEMENT_DELAY_HOURS` (or `--offer-ttl-days` / `--settlement-delay-hours`)
default to the services' values. `nightly-smoke.yml` requires 200 from every
analytics endpoint (funnel, kpis, daily-trend, channels, cohort-retention of
the current month with `periods=18`, top-campaigns by `conversion_rate`,
segment-matrix) and from the seeded experiment's results on the seeded stack.

### Upgrading to 010

010 takes its locks up front under `lock_timeout` 5 s (ACCESS EXCLUSIVE on
`cashback_accruals`, `ab_events`, `ab_variants`; SHARE on `recommendations`
for the index — reads are not blocked) and needs a quiet window like 009.
Only campaign_manager reads the new columns (`is_control`, `paid_at`): run
`alembic upgrade head`, then roll it out. Roll out recommendation_api before
mobile_api: the new mobile_api serves only items that carry a
`recommendation_id`, so behind an old recommendation_api it would serve
nothing; until mobile_api follows, the old one keeps writing its own second
row per offer (B29) — the duplicates of that window stay in the table.

## Consequences

* + Every number on the analytics pages has one written meaning; the funnel
  is monotone by construction, the open rate cannot be 100 % by construction,
  and the "paid" stage moves.
* + `ml_online_ctr` and the holdout comparison finally have data; counts over
  `recommendations` are no longer doubled.
* + A/B verdicts cannot flip with a variant's name, cannot 500 on duplicate
  events or races, and say which way the effect goes.
* + Each analytics SQL is pinned by a real-Postgres test with hand-computed
  numbers; the nightly fails on any analytics 500.
* − Analytics runs on the OLTP database (two extra indexes, window scans on
  `recommendations`); at bank scale it belongs in a separate store.
* − Two units on purpose: funnel and top-campaigns count users, KPI CTR,
  channels, daily-trend and segment-matrix count offers. The UI copy has to
  say which.
* − The definitions are now a contract: changing one means changing this ADR,
  the SQL and its tests together.

## Residual risks

* **Settlement is a model.** `PAID` is "the hold has passed", not money that
  reached the customer; there are no reversals or chargebacks, and `paid_at`
  is the job's run time (up to one interval after the hold ends).
* **The trend threshold is a count, not a test.** 20 events keep ▲1940 % away
  but do not make a trend statistically meaningful.
* **A/B results compare one pair at a fixed horizon.** Multi-arm experiments
  are reduced to control vs the first other variant by name; looking at the
  p-value repeatedly while data arrives (peeking) inflates false positives —
  there is no sequential correction.
* **First delivery channel wins.** If mobile_api's best-effort IN_APP mark
  fails and no notification is sent, the row stays `UNKNOWN`; a re-delivery
  in another channel is not recorded.
* **Target audience is "now".** The funnel's first stage uses today's segments
  and does not require consent, so it is an upper bound of who could have
  been reached.
* **Cohort activity = an accrual.** A customer who only browsed or bought
  outside campaigns is "inactive" in the cohort grid.
* **Demo history is approximate.** The seeder's ClickHouse transactions span
  the last 90 days regardless of the customer's registration date (RFM
  features only); offers, consents and accruals respect it.

## Verification

* `services/campaign_manager/tests/integration/test_analytics_ab.py` — every
  analytics SQL, the campaign stats and the A/B results on a hand-built
  fixture with exact expected numbers: users, offers in all statuses, accruals
  PENDING / PAID, channels incl. unknown, cohorts spanning more than 12
  months, the segment matrix of one campaign, experiments with duplicate
  conversions, a deliberately worse treatment, concurrent assigns, settlement
  after the delay.
* `db_migrations/tests/test_010_analytics_integrity.py` — 010's constraints,
  backfills (controls, duplicate conversions, `paid_at`), upgrade / downgrade
  cycle, locks under `lock_timeout`.
* recommendation_api / mobile_api / transaction_listener unit and integration
  suites — one row per served offer, `recommendation_id` / `expires_at`
  passed through, R3/R4 marks taken back for offers not stored, IN_APP mark,
  listener delivery channel, respond keeps the channel, a snoozed answer stays
  `SNOOZE` after the snooze period.
* `services/campaign_manager/tests/integration/test_money_integrity.py` — the
  campaign list carries the per-category minimums the draft editor starts
  from (as `GET /campaigns/{id}`).
* `tests/integration/test_seed_demo_data.py` — demo offers, statuses, channels,
  settlement, cohorts and experiment controls as the live path would write
  them; `tests/e2e/test_full_cashback_cycle.py` (nightly) — one row per
  served offer with its model version and channel, the respond keeps the
  channel; `nightly-smoke.yml` — every analytics endpoint 200 on the seeded
  stack.
* `frontend/e2e/` — live widgets against a stub returning 500 / empty data.

## References

* `docs/AUDIT.md` — B9, B10, B11, B29, B33, B34, B50, B51; stage D contract.
* `db_migrations/migrations/versions/010_analytics_integrity.py`.
* `services/campaign_manager/app/api/analytics.py`, `app/api/ab_testing.py`,
  `app/scheduling.py`, `app/config.py`.
* `services/recommendation_api/app/api/recommendations.py`,
  `services/mobile_api/app/api/mobile.py`,
  `services/transaction_listener/app/notification/delivery.py`.
* `scripts/seed_demo_data.py`, `.github/workflows/nightly-smoke.yml`.
* ADR-0014 (money integrity: accruals, budget, `eligible_campaigns`).
