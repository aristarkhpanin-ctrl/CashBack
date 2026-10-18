# 0014 — Money integrity: one eligibility function, single-use offers, ledger that cannot be deleted

* **Status:** Accepted
* **Date:** 2026-10-18
* **Fixes:** B45, B24, B23, B22, B25, B21, B26, B38, B39 (`docs/AUDIT.md`), stage C

## Context

The audit found that the system paid cashback by rules that existed in
several places at once, and none of them was complete:

* **B45** — transaction_listener logged a failed accrual and committed the
  Kafka offset anyway (a Postgres restart lost the cashback for good). An
  accepted offer was "single use" only because the listener deleted its
  Redis key after the commit: a crash between the two, or the duplicate
  branch (which never deleted the key), let the offer pay for every purchase
  until the key's TTL (up to 7 days). The consumer group started at
  `latest`, the transaction time was ignored, one malformed notification
  stopped both consumers.
* **B26 / B21 / B25 / B22** — rec_api offered and the listener paid without
  looking at target segments, consent, RFM range, per-category minimums, the
  campaign's date window or its daily limit. Only the unused
  `/campaigns/applicable` checked targeting, and it took *any* old GRANTED
  consent, ignoring a later revocation. The daily-limit "auto-pause" queried
  a ClickHouse table that does not exist.
* **B24** — deleting a PAUSED / COMPLETED campaign cascaded into
  `cashback_accruals`: the ledger of what the bank owes its customers.
* **B23** — `POST /campaigns/{id}/budget/check` debited `budget_spent` for
  a "reservation" nobody ever consumed or released.
* **B38** — status changes were read-then-write: an `activate` racing the
  scheduler's `complete` revived a COMPLETED campaign.
* **B39** — the ML-limits kill switch failed open: a failed Redis publish
  was swallowed, `PUT /ml-limits` answered 200, and the periodic republish
  was the last step of a shared try that any earlier failure skipped.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| Fix each check where it lives (rec_api SQL, listener SQL, `/applicable`) | Smallest diffs | Three copies of the same rules — exactly how they drifted apart |
| A shared Python library imported by the services | One codebase | Services have separate dependency sets and deploys; the SQL still differs per caller; the database cannot enforce it |
| Event-sourced ledger / separate payments service | Strongest audit trail | Far beyond a thesis demo; another service on the money path |
| **Rules as SQL in Postgres (`eligible_campaigns`, `calculate_cashback`), money only inside one PG transaction with row locks, constraints and a trigger as the last line of defence (chosen)** | One definition every caller uses; the database rejects what code gets wrong; testable directly in SQL | Business rules in PL/pgSQL / SQL; a migration per rule change |

For the budget "reservation" (B23): a reservation table with idempotency
keys, expiry and confirm/release was considered and rejected — nothing
consumes reservations. The accrual transaction is the only place that spends
budget, under the campaign row lock.

## Decision

### One definition of "who gets which campaign" (B26, B21, B25, B22)

Migration 009 adds `eligible_campaigns(p_user_id, p_mcc, p_amount, p_at)` —
a `STABLE` SQL function, the **only** eligibility definition. rec_api (offers,
`p_amount = NULL`), the listener (accrual, the purchase amount and
`transaction_date`) and `GET /campaigns/applicable/{user_id}` all call it; no
caller adds conditions of its own. A campaign is eligible when all hold:

* `status = 'ACTIVE'` and `archived_at IS NULL`;
* `start_date <= p_at < end_date`;
* the MCC is one of the campaign's categories, and the amount (if known) is
  at least `COALESCE(category minimum, campaign minimum, 0)`;
* the user's segment is targeted (an empty target list targets everyone);
* the **latest** `personalised_cashback` consent (by `created_at`, then
  `consent_id`) is GRANTED — no consent row, no campaign;
* with `rfm_min` / `rfm_max` set, `users.rfm_score` is known and in range
  (fail-closed: an unknown score is not eligible);
* budget left > 0, and with a `daily_limit`, `spent_today < daily_limit`,
  where `spent_today` sums the campaign's accruals since UTC midnight of
  `p_at` — or of today, if `p_at` is later: an accrual is booked today
  (`accrued_at = now()`), so a purchase stamped tomorrow still sees today's
  spending.

Rows are ordered by `cashback_rate DESC, start_date DESC, campaign_id`;
rec_api takes the first row per candidate MCC (a candidate without an
eligible campaign is dropped before the BRE). The function is a single SQL
statement, so the planner inlines it into the caller when its arguments are
parameters or columns (rec_api: `LATERAL` over `unnest(:mccs)`); a consent
index `(user_id, consent_type, created_at DESC, consent_id DESC)` makes the
latest-consent lookup one index probe.

`calculate_cashback(campaign, mcc, amount)` replaces the 2-argument version
(dropped, no wrapper: a call without the MCC cannot apply the category
minimum): 0 for an MCC outside the campaign's categories or an amount below
the effective minimum; the `rate_tiers` scale is unchanged.

`users.rfm_score` (SMALLINT 1…5, 5 = best, NULL = unknown) is filled by the
demo seeder now — the quintile of R + F + M quintiles over
`user_rfm_features` — and by the ETL later.

### Single-use offers and the accrual transaction (B45, B22)

The offer payload mobile_api writes on accept is
`{"campaign_id","recommendation_id","accepted_at","activation_id"}`; a key
without `recommendation_id` (written before stage C) is never paid
(`legacy_offer`). `AccrualEngine.accrue(...)` runs one Postgres transaction:

1. `recommendations … FOR UPDATE` — the offer must exist and belong to
   (user, campaign, MCC) (`offer_mismatch`); an accrual of this offer, or of
   this purchase under any offer (or a pre-009 row without one), is a
   `duplicate` — only in the first case is the offer consumed (its key
   released), a newer offer met by a replay keeps its key; otherwise it must
   be ACCEPTED (`offer_not_accepted`) no later than the purchase
   (`accepted_after_purchase`: `responded_at`, truncated to whole seconds —
   the precision producers stamp `transaction_date` with — `<=
   transaction_at`; an unknown `responded_at` is not paid; the key stays for
   a later purchase) and `expires_at > transaction_at` (`offer_expired`);
2. `cashback_campaigns … FOR NO KEY UPDATE` — serialises budget and daily
   limit with other accruals and status changes, without blocking the FK
   check (`FOR KEY SHARE`) of offers rec_api / mobile_api write for the
   campaign;
3. the campaign must be in `eligible_campaigns(user, mcc, amount,
   transaction_at)` (`not_eligible`);
4. `calculate_cashback(...)`; 0 → `zero_cashback`;
5. no partial payouts: `spent_today + cashback > daily_limit` →
   `daily_limit_reached`, `cashback > budget left` → `budget_exhausted`;
6. `INSERT … ON CONFLICT DO NOTHING` with the `recommendation_id`: the
   unique keys `(transaction_id, campaign_id)`, `recommendation_id` and
   `transaction_id` of rows with an offer (`uq_accrual_transaction`;
   migration 009, NULL allowed for legacy rows) all mean `duplicate` and
   leave the budget alone — two consumers replaying one purchase under two
   offers pay it once; otherwise `budget_spent += cashback`. COMMIT.

After the commit (best effort — the ledger is already right): a Lua
compare-and-delete removes the Redis key only while it still holds *this*
`recommendation_id` (mobile_api may have written a newer offer), also after a
duplicate of the same offer, so a lost `DEL` is repaired by the next
purchase; then `cashback.accrued` is published with `recommendation_id` and
`transaction_at`. The daily limit is enforced here and in eligibility; there
is no status flip, so nothing has to "resume" a campaign the next day
(`auto_pause` stays as a column and means "stop paying for the rest of the
UTC day", which is now always the behaviour; the campaign wizard shows that
as a note instead of a switch).

Metric `accrual_engine_total{outcome}`: `success`, `offer_mismatch` (also an
unknown `recommendation_id`), `offer_not_accepted`,
`accepted_after_purchase`, `offer_expired`, `not_eligible`, `zero_cashback`,
`daily_limit_reached`, `budget_exhausted`, `duplicate`; listener outcomes add
`no_offer`, `legacy_offer`, `bad_offer`, `dead_lettered`.

### Kafka delivery: retry, seek, DLQ (B45)

* `transaction_at` is the Avro `transaction_date` (aware UTC; naive = UTC).
  `transaction_id`, `user_id`, `mcc_code`, `amount` (numeric, finite) and
  `transaction_date` are required.
* **Permanent** errors — `PermanentMessageError` (undecodable Avro by the
  same rules as the ETL consumer, a non-record, missing / invalid fields),
  Postgres SQLSTATE classes 22 (data) and 23 (integrity), Redis `WRONGTYPE`
  — publish the raw key / value / headers to `transactions.accrual.dlq`
  (created on demand, `DLQ_REPLICATION_FACTOR`; the listener's own `acks=all`
  producer) with headers `dlq.error`, `dlq.source.topic`,
  `dlq.source.partition`, `dlq.source.offset`, `dlq.consumer.group`, and
  the offset is committed past the message.
* **Everything else is retryable** (connection, timeout, deadlock,
  serialisation, an undefined function during a deploy, a failed DLQ
  publish, anything unanticipated): the failed partition is sought back to
  the failed offset, every later partition of the batch to its first record,
  the done prefix is committed, and the batch is re-read after a backoff of
  1 s doubling to 30 s (`RETRY_BACKOFF_INITIAL_S` / `_MAX_S`). A replay is
  safe: a purchase that was paid — under any offer — ends in `duplicate`,
  and an offer accepted after the purchase does not pay it.
* `auto_offset_reset='earliest'` for the accrual group; a failed commit
  (rebalance) is logged and the loop goes on. The notification consumer
  skips malformed messages; both loops run under a supervisor that restarts
  a crashed loop with a 1 → 30 s backoff.

### The ledger is never deleted; archive instead (B24)

* Foreign keys from `cashback_accruals` and `recommendations` to campaigns
  are `ON DELETE RESTRICT`; `campaign_categories` stays `CASCADE`.
* `DELETE /campaigns/{id}` → 204 only for a DRAFT without recommendations
  and accruals; anything else → 409 `campaign has history — archive it
  instead` (the foreign-key violation maps to the same 409).
* `POST /campaigns/{id}/archive` (permission `campaigns_delete`) sets
  `archived_at` for PAUSED or COMPLETED campaigns (ACTIVE → 409 "pause or
  complete the campaign first", DRAFT → delete it). Lists hide archived
  campaigns unless `include_archived=true`, so do the dashboard KPIs
  (`/analytics/kpis` without `campaign_id`); an archived campaign is
  eligible for nobody and its status no longer changes — neither through the
  API nor by the scheduler's `complete-expired-campaigns`.

### Status FSM in the database (B38, B25)

* Every status change is compare-and-set (`UPDATE … WHERE status =
  :expected RETURNING …`, 0 rows → 409), in the API and in the scheduler.
  The DRAFT-only edit `PATCH /campaigns/{id}` reads the row `FOR UPDATE`
  before its DRAFT check: a racing `activate` either waits for the edit or
  has committed — then the edit is a 409, not a change of a live campaign.
* Trigger `trg_campaign_status_guard` (BEFORE UPDATE OF status) allows only
  the transitions of `services/campaign_manager/app/fsm.py` — DRAFT→ACTIVE,
  ACTIVE↔PAUSED, ACTIVE/PAUSED→COMPLETED; COMPLETED is terminal. Anything
  else raises SQLSTATE 23514 with `constraint_name =
  trg_campaign_status_guard`, which the API maps to 409. `db_migrations/tests`
  derive the allowed set from `fsm.py`: changing the FSM needs a migration.
* `activate` → 409 when `now() >= end_date`; activating before `start_date`
  is allowed (`starts_in_future: true` in the response, the UI shows
  "запустится {start_date}") — eligibility starts at `start_date`.

### Budget is spent only by accruals (B23)

`POST /campaigns/{id}/budget/check` and its Redis holds are gone. Read-only
`GET /campaigns/{id}/budget` (permission `campaigns_view`) returns
`budget_total, budget_spent, remaining, daily_limit, spent_today,
remaining_today` (today = since UTC midnight, as in `eligible_campaigns`).
rec_api's R6 judges the `eligible_campaigns` row it already has (budget left,
`spent_today < daily_limit`) without a query or row lock of its own — an
offer is not money; the accrual decides.

### Upgrading a running system to 009

* **Deploy order.** 009 drops `calculate_cashback(campaign, amount)`, and the
  pre-stage-C listener logs a failed accrual and commits the offset anyway —
  every purchase it processes against 009 is lost. So: (1) roll out the new
  transaction_listener and wait until the old pods are gone — on the 008
  schema it does not accrue but retries (SQLSTATE class 42 is retryable), the
  offsets stay; (2) `alembic upgrade head`; (3) roll out campaign_manager and
  recommendation_api, which need 009. No compatibility 2-argument function
  is kept: it would let the old listener pay by the old rules (no category,
  window, consent or daily-limit checks) and write accruals without an offer.
* **Locks.** 009 rewrites hot tables in one transaction. It takes all its
  locks first, in one `LOCK TABLE cashback_accruals, recommendations,
  user_consents, cashback_campaigns, users IN ACCESS EXCLUSIVE MODE` —
  children before parents, the order the services' INSERTs lock them (row,
  then the FK parents) — under `SET LOCAL lock_timeout = '5s'`: it no longer
  holds `users` while waiting for `recommendations` (that deadlocked with an
  offer INSERT), and if it cannot get a lock it rolls back untouched instead
  of queueing everybody behind it. It still holds the locks until COMMIT
  (FK validation, indexes, the backfill — seconds per million rows): run it
  in a quiet window.
* **Legacy accruals.** Accruals written before 009 have no
  `recommendation_id`; 009 links each to the offer it paid — the latest
  ACCEPTED offer of the same user, campaign and MCC accepted no later than
  the accrual — keeping only the earliest accrual per offer and per purchase
  (the UNIQUE keys). Without the link the new engine would pay an offer
  again whose key survived the old listener's `DEL`.

### ML-limits kill switch fails closed (B39)

`PUT /ml-limits` commits to Postgres, then publishes the snapshot (built
from the table rows, honouring each bucket's `enabled`); a failed publish →
503 `saved but not published to the serving path; it will be retried`, and
the change stays in the database. The scheduler runs `republish-ml-limits`
every 60 s as its own job; every job (`complete-expired-campaigns`,
`export-budget-metrics`, `export-online-ctr`, `republish-ml-limits`) runs in
its own try and counts failures in `scheduler_job_failures_total{job}`.

### Demo data obeys the same rules

`scripts/seed_demo_data.py` writes `rfm_score` for demo users (step 4b),
seeds recommendations only for (user, campaign) pairs `eligible_campaigns`
returns now, and creates an accrual only for an ACCEPTED recommendation (its
`recommendation_id`, at most one per offer), with the purchase between
acceptance and expiry, and only if the accrual engine would pay it:
`eligible_campaigns` at the purchase time, `calculate_cashback`, the daily
limit and the budget without partial payouts; `budget_spent` grows by
exactly the accrued sum.

## Consequences

* + Offers, accruals and `/applicable` cannot disagree about who is
  eligible: they run the same SQL. A rule change is one migration with its
  SQL tests (`db_migrations/tests`, in CI).
* + An accepted offer pays at most once, whatever happens to Redis or to
  Kafka redelivery; a Postgres outage delays accruals instead of losing them;
  a message that can never be processed is visible in the DLQ instead of
  silently dropped.
* + The accrual ledger survives campaign deletion attempts; a COMPLETED
  campaign cannot be revived by any code path.
* + The daily limit holds under concurrent purchases (campaign row lock).
* − Every accrual takes two row locks; all purchases of one campaign
  serialise on its row (throughput per campaign is bounded by Postgres).
* − Business rules live in SQL / PL/pgSQL; the FSM exists twice (`fsm.py`
  and the trigger), kept in sync by a test.
* − A message that keeps failing with a retryable error stalls its partition
  (visible in `transaction_listener_retries_total`) until it is fixed.
* − Downgrading 009 drops `recommendation_id`: the single-use guarantee is
  gone again on the old schema.

## Residual risks

* **No outbox for `cashback.accrued`.** The event is published after the
  commit; a crash in between loses the event (the accrual itself is safe).
  Consumers of the topic must not be the source of truth for money.
* **Unknown errors are retried forever.** Fail-safe for money, but a bug
  that raises on one message blocks its partition; there is no retry cap or
  parking topic, only the metric and logs.
* **The producer's clock is trusted.** `transaction_date` decides the date
  window, the offer's expiry and "accepted before the purchase" (to the
  second); nothing bounds it from above. A purchase stamped in the future
  (clock skew, a producer bug) can pass the last check; the daily limit is
  not affected (it counts the day the accrual is booked).
* **Accrual time vs transaction time.** Eligibility and the date window use
  the transaction time; `spent_today` counts accruals by `accrued_at`
  (processing time) since UTC midnight of the transaction day or today,
  whichever is earlier, with no upper bound — a late transaction from
  yesterday is checked against today's spending too (conservative), and the
  day boundary is UTC, not the bank's time zone.
* **The daily limit is now a hard cap for every campaign.** Before stage C
  it was never enforced (B22), and campaigns created with `auto_pause =
  false` were exempt from the intended pause. After 009 every `daily_limit`
  caps payouts, including the ones migration 006 derived from budget and
  duration for older campaigns — review those after the upgrade
  (`daily_limit = NULL` lifts the cap). The wizard no longer shows an
  "auto-pause" switch.
* **Legacy links are inferred.** The 009 backfill matches a pre-009 accrual
  to an offer by user, campaign, MCC and time. The old code's double payouts
  stay without an offer; if one of them paid a second offer whose key is
  still alive, that offer looks unpaid and pays once more (needs an old
  double payout and a surviving key).
* **Consent and RFM are "now", not "at the purchase".** The latest consent
  row decides, even if it was written after the purchase; `rfm_score` is
  whatever was computed last. Outside the demo nobody has an `rfm_score`
  until the ETL fills it (follow-up), so RFM-filtered campaigns pay nobody.
* **A purchase consumed after the offer key's TTL is not paid.** The key lives
  until the offer expires (the campaign's `end_date` at most); a purchase made
  before that but processed later (consumer lag, a Postgres outage that runs
  past `end_date`) finds no key, ends as `no_offer` and is committed — the
  cashback is lost (reproduced by the integration run, scenario S3b). Follow-up:
  a listener fallback to the unpaid ACCEPTED offer in Postgres, or a TTL grace
  period in mobile_api, plus the reconciliation job below.
* **The Redis key is still the trigger.** A lost key (Redis flush) means the
  listener never looks at an ACCEPTED offer: the customer is not paid, and
  there is no reconciliation job yet (AUDIT §4 — nightly `SUM(accruals)` ↔
  `budget_spent`).
* **DLQ durability** depends on `DLQ_REPLICATION_FACTOR` (1 in the demo);
  there is no tool to replay the DLQ.
* **Deploy order is a runbook, not enforced.** Migrating to 009 while a
  pre-stage-C listener still consumes loses those purchases (see "Upgrading
  a running system"); 009 holds its table locks until COMMIT and needs a
  quiet window.
* **Archive is one-way** (no unarchive endpoint); the demo seeder has no
  advisory lock against two concurrent runs.

## Verification

* `db_migrations/tests` — every rule of 009 with positive and negative cases,
  upgrade → downgrade → upgrade on empty and populated databases, the
  legacy-accrual backfill, the up-front locks under `lock_timeout` (CI:
  service-integration `db_migrations`).
* `services/transaction_listener/tests/integration` — single-use offers and
  purchases (a replay under another campaign's offer, concurrent replays),
  purchases before the accept, a purchase stamped tomorrow against today's
  limit, the campaign lock vs offer INSERTs, replay of the whole topic,
  Postgres outage mid-batch, DLQ, 20 concurrent purchases against a daily
  limit (CI: service-integration `transaction_listener`).
* `services/campaign_manager/tests/integration/test_money_integrity.py` —
  delete vs archive, activate vs scheduler, DRAFT edit vs activate, budget
  endpoint, `/applicable`, archived campaigns in KPIs and the scheduler, ML
  limits with Redis down.
* `tests/integration/test_listener_avro_accrual.py` — the e2e producer's Avro
  records: one accrual per offer after a lost `DEL`, the category minimum,
  JSON → DLQ; `tests/integration/test_seed_demo_data.py` — demo money by the
  same rules.
* `tests/e2e/test_full_cashback_cycle.py` (nightly) — the live money path:
  below-minimum purchase not paid, second purchase on the same offer not
  paid.

## References

* `docs/AUDIT.md` — B21–B26, B38, B39, B45; stage C contract.
* `db_migrations/migrations/versions/009_money_integrity.py`, `db_migrations/tests/`.
* `services/transaction_listener/app/accrual/engine.py`, `app/listener.py`, `app/main.py`.
* `services/campaign_manager/app/api/campaigns.py`, `app/scheduling.py`,
  `app/api/ml_limits.py`, `app/fsm.py`.
* `services/recommendation_api/app/api/recommendations.py`,
  `app/bre/rules/budget_reservation.py`.
* `scripts/seed_demo_data.py`, `.github/workflows/ci.yml`.
