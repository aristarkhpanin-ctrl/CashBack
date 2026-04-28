-- =====================================================================
-- Chapter 3.1, listing 3.5 — raw transactions fact table.
-- Local-development variant: plain MergeTree (no replication).
-- Partitioned monthly, 36-month retention.
-- =====================================================================

CREATE TABLE IF NOT EXISTS transactions_raw
(
    transaction_id      String,
    user_id             UUID,
    transaction_date    DateTime,
    amount              Decimal(15, 2),
    currency            LowCardinality(String),
    mcc_code            FixedString(4),
    merchant_id         String,
    merchant_name       String,
    channel             LowCardinality(String),
    city                LowCardinality(String),
    country             LowCardinality(String),
    is_weekend          UInt8 MATERIALIZED toDayOfWeek(transaction_date) >= 6,
    hour_of_day         UInt8 MATERIALIZED toHour(transaction_date),
    inserted_at         DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(transaction_date)
ORDER BY (user_id, transaction_date)
TTL transaction_date + INTERVAL 36 MONTH
SETTINGS index_granularity = 8192;
