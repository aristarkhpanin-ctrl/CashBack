# 0002 — FastAPI as the HTTP framework

* **Status:** Accepted
* **Date:** 2025-12-16

## Context

Four of seven services (recommendation_api, campaign_manager, mobile_api,
the etl webserver) expose HTTP/JSON. We need first-class async support
(p99 ≤ 50 ms target from chapter 3.3, Postgres + Redis + httpx fan-out),
declarative request/response validation, OpenAPI 3.1 generation for the
mobile + frontend clients, and dependency injection for testability.

## Alternatives

| Option              | Pros | Cons |
|---------------------|------|------|
| **FastAPI 0.115**   | Native async; pydantic v2 validation; OpenAPI 3.1; ASGI; rich middleware ecosystem; great DX | Some startup overhead (lifespan + reflection); pydantic v2 migration cost |
| Flask 3 + Marshmallow | Simple; familiar.   | Sync-by-default; OpenAPI is bolted-on; slow for async fan-out. |
| Django REST Framework | Batteries-included; admin UI. | Heavyweight; sync ORM; bad fit for streaming integrations. |
| Starlette (raw)     | Minimal; max throughput. | We'd reimplement validation + OpenAPI ourselves. |

Decision criteria: async (40), validation/OpenAPI (35), ecosystem (15),
throughput (10).

## Decision

**FastAPI 0.115** with pydantic v2 (`pydantic==2.*`) on **uvicorn[standard]**
with `--proxy-headers --no-access-log`. Each service uses the same skeleton
(see e.g. `services/recommendation_api/app/main.py`):

* lifespan-based DI for clients (Redis, Postgres, MLflow, Kafka)
* CORS / GZip / RequestID middlewares (custom)
* Prometheus instrumentation via `prometheus-fastapi-instrumentator`
* OpenTelemetry instrumentation when `OTEL_ENDPOINT` is set
* health probes — `/health/live` (liveness) + `/health/ready`
  (composite Postgres / Redis / ClickHouse / MLflow / model loaded)

OpenAPI is auto-published at `/openapi.json` and consumed by the
frontend's `scripts/generate-api-types.sh` (see ADR 0012).

## Consequences

* + Pydantic v2 validation kills an entire class of bugs at the service
  boundary; the BRE / Recommendation API responses are strongly typed.
* + The frontend types regenerate on each backend change — no schema
  drift across services.
* + Dependency injection makes unit + integration tests trivial: see
  `tests/integration/test_recommendation_flow.py` patching state via
  `app.state.feature_store = AsyncMock(...)`.
* − Pydantic v2 forced a major migration step (validators / config),
  done once before phase 6.
* − Some FastAPI middleware are `BaseHTTPMiddleware`-based and add ~1 ms
  latency. Acceptable: still well below the 50 ms target.

## References

* `services/recommendation_api/app/main.py` — canonical layout.
* `services/recommendation_api/app/api/recommendations.py` — listing 3.9.
* `frontend/src/shared/api/client.ts` — interceptor mirroring FastAPI errors.
