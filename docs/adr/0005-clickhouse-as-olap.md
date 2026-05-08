# 0005 — ClickHouse as the OLAP store

* **Status:** Accepted
* **Date:** 2025-12-18
* **Driver:** chapter 2.3, table 12

## Context

We need to store and query potentially billions of transaction rows
(simulator drops 1 000+ tx/sec for hours), recompute 108-feature RFM
vectors per user nightly, and answer analytic queries (segment-matrix,
funnel, cohort retention) in seconds. The schema is wide
(`user_rfm_features` has 108 columns) and heavily aggregated.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **ClickHouse 24.3** | Column-store, vectorised queries, partitioning + TTL, native Parquet ingest, mature replication. | Single-writer per shard for `INSERT`; learning curve for distributed setups. |
| Apache Druid | Real-time + historical, sub-second analytics. | Heavier ops; smaller community; awkward updates. |
| Snowflake / BigQuery | Managed, infinite scale. | Vendor lock-in; cost; latency from outside the cloud. |
| Postgres + columnar ext (Hydra/Citus) | Single store. | Worse compression / slower for columnar workloads. |
| TimescaleDB | Time-series strong. | Not optimal for wide aggregates (108 columns). |

## Decision

**ClickHouse 24.3-alpine** with three tables:

1. `transactions_raw` (chapter 3.1, listing 3.5):
   * `ENGINE = MergeTree`
   * `PARTITION BY toYYYYMM(transaction_date)`
   * `ORDER BY (user_id, transaction_date)`
   * `TTL transaction_date + INTERVAL 36 MONTH`
   * 12 columns including `is_weekend`, `hour_of_day` (`MATERIALIZED`).

2. `user_rfm_features` (chapter 3.1, listing 3.6 schema):
   * `ENGINE = ReplacingMergeTree(computed_at)`
   * 108 features:
     * 6 base RFM (`recency_days`, `frequency_total`, `monetary_total`,
       `avg_ticket`, `weekend_ratio`, `evening_ratio`)
     * `distinct_mcc_count`
     * 50 × `freq_<mcc>` (UInt32)
     * 50 × `amt_<mcc>`  (Decimal(18, 2))
     * `tenure_months`

3. `transactions_buffer` — `Buffer(currentDatabase(), transactions_raw,
   16, 10, 60, 1000, 100000, 1048576, 10485760)` for batch ingest from
   the ETL.

Async client: `clickhouse-connect` (HTTP/8123). Async-friendly via
`asyncio.to_thread` wrappers in feature-store + accrual paths.

## Consequences

* + The `RFM_QUERY` from listing 3.6 is one window-less aggregate that
  can scan 100 M rows in 1–2 s.
* + Native `Buffer` engine smoothes spiky ingest from `tx_simulator`.
* + Compression cuts storage by ~10× vs Postgres for wide RFM rows.
* − Not designed for point updates — the `ReplacingMergeTree` has
  eventual dedup. The mobile API never reads from CH directly; it
  reads `features:{user_id}` from Redis (warmed by the ETL pipeline).
* − A single CH writer; we mitigate by funnelling all writes through the
  Buffer table.

## References

* `infrastructure/clickhouse/migrations/001_create_transactions_raw.sql`
* `infrastructure/clickhouse/migrations/002_create_user_rfm_features.sql`
* `infrastructure/clickhouse/migrations/003_create_buffer_table.sql`
* `services/etl/app/feature_eng.py` — `RFM_QUERY` constant.
* `services/etl/app/ch_loader.py` — `insert_arrow` to Buffer.
