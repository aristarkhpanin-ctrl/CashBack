# 0006 — Redis as the cache + feature store + queue

* **Status:** Accepted
* **Date:** 2025-12-18

## Context

Hot-path operations the system must execute in single-digit milliseconds:

* feature lookup (`features:{user_id}`) for ranking;
* offer-state lookup (`accepted_offers:{user_id}:{mcc_code}`) on every
  inbound transaction;
* anti-fatigue dedup (`recent_offer:{user_id}:{campaign_id}`, TTL 14 d);
* frequency gate (`rec_count:{user_id}`, TTL 24 h);
* in-app inbox (`in_app_queue:{user_id}` — Redis list);
* OST (`ost:{user_id}` — JSON histogram, TTL 30 d);
* budget reservation hold (`budget_reservation:{cid}:{rid}`, ephemeral).

We also need atomic counters (`SETEX`, `INCR`) and simple data
structures (lists, hashes) — not full relational storage.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **Redis 7.2** | Sub-millisecond latency; native TTL; rich data structures; AOF persistence; mature Python clients (`redis-py 5`); pipelines. | In-memory cost; persistence weaker than a real DB. |
| Memcached | Simpler. | No structures (just key/value), no TTL semantics for our patterns. |
| Postgres `UNLOGGED` table | Same store. | 10–50× slower; doesn't fit hot-path budget. |
| In-process LRU | Zero deps. | Doesn't survive restarts; not shared across replicas. |

## Decision

**Redis 7.2-alpine** — single instance locally; managed-Redis or sentinel
in production. All services use the **async** client
(`redis.asyncio.Redis.from_url`) to avoid blocking the event loop.

Key naming convention (single source of truth):

| Pattern | TTL | Owner | Listing |
|---------|-----|-------|---------|
| `features:{user_id}`                 | 1 h  | ETL writes / reco-api reads | 3.6 |
| `accepted_offers:{user_id}:{mcc}`    | =expires_at-now | mobile-api writes / listener reads-and-deletes | 3.11 |
| `recent_offer:{user_id}:{cid}`       | 14 d | BRE writes/reads | 3.12, R3 |
| `rec_count:{user_id}`                | 24 h | BRE writes/reads | R4 |
| `in_app_queue:{user_id}`             | 7 d  | NotificationPipeline lpush | 3.12 |
| `ost:{user_id}`                      | 30 d | OST DAG writes / DeliveryScheduler reads | — |
| `budget_reservation:{cid}:{rid}`     | <=300s | campaign_manager / observability | — |

Pipelines used in `RFMComputer.run` (writes 100 k+ keys per ETL run).

## Consequences

* + Hot-path lookups are O(1) — `FeatureStoreClient.get` resolves under
  1 ms when warm.
* + TTL handles cleanup automatically — no garbage-collection cron.
* + `WATCH/MULTI/EXEC` available if we need optimistic concurrency
  later (we currently rely on Postgres `FOR UPDATE` for budgets).
* − Single instance is a SPOF; we run with AOF + plan to add Sentinel.
* − Memory cap: 100 M users × 1 KB feature blob = ~100 GB. Acceptable
  with TTL + eviction (`maxmemory-policy allkeys-lru`).

## References

* `services/recommendation_api/app/feature_store.py`
* `services/transaction_listener/app/accrual/engine.py` (delete on success).
* `services/recommendation_api/app/bre/rules/{anti_fatigue,frequency_gate}.py`
* `services/transaction_listener/app/notification/pipeline.py` — `InAppAdapter`.
