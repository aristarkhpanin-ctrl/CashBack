# 0001 — Python as the backend language

* **Status:** Accepted
* **Date:** 2025-12-15
* **Driver:** chapter 2.3 of the thesis

## Context

The CashBack platform consists of seven backend services (recommendation API,
campaign manager, mobile BFF, transaction listener, ETL, ML training, TX
simulator). The same team owns ML, data engineering and backend, and the
recommendation system itself is the central differentiator — meaning that
fast iteration on ML logic, easy integration with the data-science
ecosystem (LightGBM, SHAP, MLflow, scipy, pandas) and a low cost of hiring
data engineers are first-order requirements.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **Python 3.11** | Mature data/ML ecosystem (LightGBM, SHAP, MLflow, scipy, pandas, FastAPI, asyncio). Single language end-to-end. Trivial integration with airflow operators. | Slower CPU code than compiled languages; GIL limits CPU-bound concurrency (mitigated by `asyncio` for I/O and external workers for ML). |
| Go 1.22 | Strong concurrency, compact binaries, low memory footprint. | No first-class ML libraries (`gorgonia` ≪ pytorch); team would have to bridge to Python anyway for training. |
| Java 21 / Kotlin | Mature streaming + JVM ML (DL4J), strong typing. | Higher operational overhead, longer feedback loops, fewer ML hires available. |
| Rust 1.78 | Best-in-class throughput; safe concurrency. | Tiny ML ecosystem; hiring pool too small for a master's project. |

Decision criteria (weighted): ML ecosystem 40 %, hiring 25 %, throughput 20 %, ops cost 15 %.

## Decision

We use **Python 3.11** as the single backend language. Performance hot
paths (ranking, retrieval) are kept O(features × candidates), the heavy
lifting (LightGBM, FAISS) is delegated to native libraries, and the API
surface is built with FastAPI on uvicorn (uvloop) — typically benchmarked
at 30–50 k RPS per process which exceeds the SLOs from chapter 3.3.

Type-checked with mypy (`--ignore-missing-imports --no-strict-optional`),
linted with ruff. CI runs the matrix `python:3.11 + 3.12` to catch
upcoming-version issues early.

## Consequences

* + Same language for inference, ETL and training simplifies refactor
  (e.g. moving a feature from the ranker to the retrieval).
* + LightGBM / SHAP / MLflow / scipy.stats land in the same `pyproject.toml`
  as FastAPI — no IPC cost for ML.
* − GIL forces us to scale CPU-bound code horizontally (HPA per pod) rather
  than vertically (more threads per pod). Acceptable: HPA is in place.
* − Cold-start latency (interpreter + library imports) is ~3 s for the
  recommendation API. We pre-warm workers with `--workers 1` per pod and
  rely on K8s readiness probes.

## References

* Chapter 2.3 of the thesis — comparative table.
* `services/*/pyproject.toml` — exact pinned versions.
* `.github/workflows/ci.yml` — Python 3.11/3.12 lint matrix.
