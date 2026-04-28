-- =====================================================================
-- Buffer table for batched ingest into transactions_raw.
-- Producers (transaction_listener, tx_simulator) write here; the
-- Buffer engine flushes to the underlying MergeTree in micro-batches.
--
-- Buffer parameters:
--   num_layers      = 16
--   min/max time    = 10 / 60 seconds
--   min/max rows    = 1_000 / 100_000
--   min/max bytes   = 1 MiB / 10 MiB
-- =====================================================================

CREATE TABLE IF NOT EXISTS transactions_buffer
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
    inserted_at         DateTime DEFAULT now()
)
ENGINE = Buffer(currentDatabase(), transactions_raw,
                16,
                10, 60,
                1000, 100000,
                1048576, 10485760);
