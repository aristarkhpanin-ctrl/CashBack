# 0011 — Docker + Kubernetes + Helm for packaging and deployment

* **Status:** Accepted
* **Date:** 2025-12-21

## Context

Eight runtime artefacts (7 services + frontend) need to ship as
**immutable, reproducible** images, must run identically locally and in
production, and be configurable per-environment without code changes.
Production target is Kubernetes (managed EKS / GKE-equivalent).

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| **Docker (multi-stage) + K8s + Helm** | Industry standard; declarative; reusable across cloud providers; Helm = template + value-override pattern. | Steep ops curve for new teams. |
| Docker Compose only | Simple. | No HPA, no rolling updates, no native NetworkPolicy. |
| Raw K8s manifests + kustomize | No template language. | Lots of duplication for 8 components. |
| Nomad + Consul | Lighter than K8s. | Smaller ecosystem; team unfamiliar. |
| Serverless (Lambda / Cloud Run) | Zero ops. | The transaction listener is a long-running consumer; not a fit. |

## Decision

**Docker (multi-stage)** for every service:

* Builder stage installs deps with `pip install --prefix=/install ...`
  (Python services) or `npm ci && npm run build` (frontend).
* Runtime stage copies only `/install` + `/app` (Python) or `dist/` +
  nginx config (frontend) — final images are 200–600 MB.
* Healthcheck baked in (`HEALTHCHECK CMD curl -f /health/live`).
* `EXPOSE` is the only port; `CMD` is `uvicorn` / `nginx` / `python -m`.

**Kubernetes 1.27+** is the production target.

**Helm 3** chart in `helm/cashback/`:

* per-service `Deployment + Service + HPA` templates;
* shared `_helpers.tpl` for `cashback.commonLabels`, image refs;
* `ConfigMap` + dual-mode Secret (inline / `external-secrets-operator`);
* `CronJob` for ML training;
* `Ingress` with regex-rewrite to `/api/recommendations`,
  `/api/campaigns`, `/api/mobile`, frontend at `/`;
* default-deny `NetworkPolicy` baseline + intra-app + scrape allows;
* optional bitnami subcharts for Postgres / Redis / Kafka / ClickHouse
  behind `<chart>.enabled` flags.

Resource requests/limits + HPA targets per **table 27** of chapter 3.3.

## Consequences

* + One `helm install cashback ./helm/cashback -f my-values.yaml`
  ships everything.
* + Same Dockerfiles power local Compose AND the K8s deployment — no
  drift.
* + GitHub Actions builds and pushes to `ghcr.io/<repo>/<service>:{sha,latest}`
  on every push to `main`.
* − Helm template language is rough; we hide complexity in `_helpers.tpl`.
* − Cluster ops cost (control plane + workers + monitoring) is non-trivial
  for a thesis project — the `make up` Compose fallback exists for
  defence demonstrations.

## References

* `services/*/Dockerfile`, `frontend/Dockerfile`
* `helm/cashback/Chart.yaml`, `helm/cashback/values.yaml`
* `helm/cashback/templates/`
* `.github/workflows/ci.yml` — build matrix on push to main.
