-- =====================================================================
-- Materialised RFM feature store (108 features per user).
-- Populated by the ETL job from `transactions_raw`.
--   6  base RFM features
--   1  distinct_mcc_count
-- 100  per-MCC pairs (freq_<mcc>, amt_<mcc>) — top-50 MCC codes
--   1  tenure_months
-- =====================================================================

CREATE TABLE IF NOT EXISTS user_rfm_features
(
    user_id              UUID,
    computed_at          DateTime DEFAULT now(),

    -- Base RFM block
    recency_days         UInt32,
    frequency_total      UInt32,
    monetary_total       Decimal(18, 2),
    avg_ticket           Decimal(15, 2),
    weekend_ratio        Float32,
    evening_ratio        Float32,
    distinct_mcc_count   UInt16,

    -- Per-MCC frequency (top-50)
    freq_5411 UInt32, freq_5812 UInt32, freq_5814 UInt32, freq_5541 UInt32, freq_5499 UInt32,
    freq_5912 UInt32, freq_5311 UInt32, freq_5651 UInt32, freq_5732 UInt32, freq_5942 UInt32,
    freq_5921 UInt32, freq_5462 UInt32, freq_4111 UInt32, freq_4121 UInt32, freq_4131 UInt32,
    freq_4814 UInt32, freq_4829 UInt32, freq_4900 UInt32, freq_7832 UInt32, freq_7011 UInt32,
    freq_7299 UInt32, freq_7211 UInt32, freq_7230 UInt32, freq_7538 UInt32, freq_7995 UInt32,
    freq_8011 UInt32, freq_8021 UInt32, freq_8062 UInt32, freq_8099 UInt32, freq_8211 UInt32,
    freq_8398 UInt32, freq_8999 UInt32, freq_5993 UInt32, freq_5995 UInt32, freq_5970 UInt32,
    freq_5945 UInt32, freq_5947 UInt32, freq_5946 UInt32, freq_5641 UInt32, freq_5611 UInt32,
    freq_5621 UInt32, freq_5712 UInt32, freq_5722 UInt32, freq_5933 UInt32, freq_5200 UInt32,
    freq_5599 UInt32, freq_5331 UInt32, freq_5300 UInt32, freq_4511 UInt32, freq_5691 UInt32,

    -- Per-MCC monetary (top-50)
    amt_5411 Decimal(18, 2), amt_5812 Decimal(18, 2), amt_5814 Decimal(18, 2),
    amt_5541 Decimal(18, 2), amt_5499 Decimal(18, 2), amt_5912 Decimal(18, 2),
    amt_5311 Decimal(18, 2), amt_5651 Decimal(18, 2), amt_5732 Decimal(18, 2),
    amt_5942 Decimal(18, 2), amt_5921 Decimal(18, 2), amt_5462 Decimal(18, 2),
    amt_4111 Decimal(18, 2), amt_4121 Decimal(18, 2), amt_4131 Decimal(18, 2),
    amt_4814 Decimal(18, 2), amt_4829 Decimal(18, 2), amt_4900 Decimal(18, 2),
    amt_7832 Decimal(18, 2), amt_7011 Decimal(18, 2), amt_7299 Decimal(18, 2),
    amt_7211 Decimal(18, 2), amt_7230 Decimal(18, 2), amt_7538 Decimal(18, 2),
    amt_7995 Decimal(18, 2), amt_8011 Decimal(18, 2), amt_8021 Decimal(18, 2),
    amt_8062 Decimal(18, 2), amt_8099 Decimal(18, 2), amt_8211 Decimal(18, 2),
    amt_8398 Decimal(18, 2), amt_8999 Decimal(18, 2), amt_5993 Decimal(18, 2),
    amt_5995 Decimal(18, 2), amt_5970 Decimal(18, 2), amt_5945 Decimal(18, 2),
    amt_5947 Decimal(18, 2), amt_5946 Decimal(18, 2), amt_5641 Decimal(18, 2),
    amt_5611 Decimal(18, 2), amt_5621 Decimal(18, 2), amt_5712 Decimal(18, 2),
    amt_5722 Decimal(18, 2), amt_5933 Decimal(18, 2), amt_5200 Decimal(18, 2),
    amt_5599 Decimal(18, 2), amt_5331 Decimal(18, 2), amt_5300 Decimal(18, 2),
    amt_4511 Decimal(18, 2), amt_5691 Decimal(18, 2),

    tenure_months        UInt16
)
ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY toYYYYMM(computed_at)
ORDER BY user_id
SETTINGS index_granularity = 8192;
