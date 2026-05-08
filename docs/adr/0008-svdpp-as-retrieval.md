# 0008 — SVD++ (implicit ALS) as the retrieval layer

* **Status:** Accepted
* **Date:** 2025-12-19
* **Driver:** chapter 2.1 — two-tier hybrid recommender

## Context

A pure ranker over 50–100 candidate MCC is workable but inefficient:
the LightGBM ranker still has to score every candidate, even those a
specific user historically dislikes. We add a *retrieval* layer that
quickly narrows the candidate set per user before ranking, so the
pipeline is **retrieve → rank → BRE-filter** (chapter 2.1 / listing 3.9).

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **implicit ALS (SVD++ for implicit feedback)** | Battle-tested for retail; no negative-sampling needed; GPU-free; small embedding sizes; FAISS-friendly. | Cold-start users; matrix re-fit on schema change. |
| Two-tower neural net | State-of-the-art on YouTube-scale data. | Heavy training; requires GPU; over-engineered for 10 k MCC items. |
| Item-CF (cosine) | Trivial. | Doesn't leverage user side info; weaker than ALS at the same scale. |
| Word2Vec on transactions ("MCC2vec") | Captures sequence. | Sequence training cost; we already have RFM features for the ranker. |
| FAISS-only on RFM features | Simple. | 108-dim features aren't a *similarity* embedding; you'd recover popular MCC, not personalised. |

## Decision

We use the **`implicit` library's `AlternatingLeastSquares`** as a stand-in
for SVD++ (the implicit-feedback variant). Implementation:
`services/ml_training/app/svd_trainer.py`.

* Construct a `csr_matrix` of shape `(n_users, n_top50_mcc)` whose
  values are `freq_<mcc> * log1p(amt_<mcc>)` (an implicit-confidence
  proxy from the RFM features).
* Fit `AlternatingLeastSquares(factors=64, iterations=15,
  regularization=0.05)`.
* Persist `user_embeddings.npy`, `item_embeddings.npy`, `user_ids.txt`,
  `item_codes.txt` as MLflow artefacts (group `embeddings/`).
* Build an in-memory FAISS `IndexFlatIP` over the L2-normalised item
  embeddings; persist as `index/items.index`.

At inference time (`services/recommendation_api/app/candidate_gen.py`):

* lookup the user's embedding by id (cold start → top-20 popular MCC);
* `faiss.normalize_L2(user_vec)` + `index.search(user_vec, k=20)`;
* return the top-k `mcc_code`s for the LightGBM ranker.

## Consequences

* + Reduces ranker workload from 50+ candidates to top-20.
* + FAISS lookup is sub-millisecond — adds noise-floor latency.
* + Embedding files are tiny (10 k users × 64 floats = 2.5 MB).
* − Cold-start users bypass retrieval entirely (popular fallback).
  Acceptable in v1; future-work: blend popularity + content features.
* − When the top-50 MCC list changes, we must retrain — this is
  hardcoded by chapter 3.1 listing 3.6 and matches the
  `infrastructure/clickhouse/migrations/002_create_user_rfm_features.sql`
  schema.

## References

* `services/ml_training/app/svd_trainer.py`
* `services/recommendation_api/app/candidate_gen.py`
* `services/ml_training/tests/unit/test_trainer.py` — `build_matrix`
  shape + nnz assertions.
