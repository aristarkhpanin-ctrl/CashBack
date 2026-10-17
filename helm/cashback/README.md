# cashback Helm chart

Personalised cashback recommendation platform — Kubernetes-native deployment
of all microservices from chapter 3.3 of the dissertation.

```text
helm/cashback/
├── Chart.yaml
├── values.yaml
├── README.md            ← this file
└── templates/
    ├── _helpers.tpl
    ├── configmap.yaml
    ├── secret.yaml
    ├── serviceaccount.yaml
    ├── deployment-{recommendation-api,campaign-manager,mobile-api,
    │                transaction-listener,etl-worker,frontend}.yaml
    ├── service-{...}.yaml
    ├── hpa-{...}.yaml
    ├── cronjob-ml-retrain.yaml
    ├── ingress.yaml
    └── networkpolicy.yaml
```

## Prerequisites

* Kubernetes 1.27+
* `helm` 3.13+
* The subchart archives (Postgres / Redis / Kafka / ClickHouse). Helm needs
  them to render the chart even while the subcharts are disabled. They come
  from Bitnami's OCI registry (`oci://registry-1.docker.io/bitnamicharts`),
  so no `helm repo add` is needed:

  ```bash
  helm dependency update helm/cashback
  ```
* For the `kafka_consumer_lag` HPA on `transaction-listener`: a working
  `prometheus-adapter` (or KEDA) exposing the metric to the
  `external.metrics.k8s.io` API.

## Install

```bash
helm install cashback helm/cashback \
  --namespace cashback --create-namespace \
  -f my-values.yaml
```

Upgrade:

```bash
helm upgrade --install cashback helm/cashback \
  --namespace cashback \
  -f my-values.yaml --atomic --timeout 10m
```

Uninstall:

```bash
helm uninstall cashback --namespace cashback
```

## Key parameters (table 27)

| Path                                          | Default                  | Notes |
|-----------------------------------------------|--------------------------|-------|
| `image.registry`                              | `ghcr.io`                | registry of our images only (subcharts keep `docker.io`); a non-empty `global.imageRegistry` (a mirror) overrides every image, subcharts included |
| `image.repository`                            | `aristarkhpanin-ctrl/cashback` | image = `{registry}/{repository}/<component>:<appVersion>` |
| `recommendationApi.replicas`                  | `2`                      | scale-to-zero is **off** (HPA min = 2) |
| `recommendationApi.resources.requests.cpu`    | `500m`                   | per pod |
| `recommendationApi.resources.limits.cpu`      | `1000m`                  | per pod |
| `recommendationApi.resources.requests.memory` | `512Mi`                  |       |
| `recommendationApi.resources.limits.memory`   | `1Gi`                    |       |
| `recommendationApi.hpa.minReplicas`           | `2`                      |       |
| `recommendationApi.hpa.maxReplicas`           | `10`                     |       |
| `recommendationApi.hpa.targetCpuUtilizationPercentage` | `70`            |       |
| `campaignManager.hpa.{min,max}Replicas`       | `2 / 6`                  |       |
| `mobileApi.hpa.{min,max}Replicas`             | `2 / 8`                  |       |
| `transactionListener.hpa.kafkaConsumerLagThreshold` | `5000`             | scales out when avg lag > 5k records |
| `mlTraining.schedule`                         | `0 3 * * 1` (Mon 03:00)  | `concurrencyPolicy: Forbid` |
| `webFrontend.hpa.{min,max}Replicas`           | `2 / 6`                  |       |
| `ingress.host`                                | `cashback.example.com`   | TLS via cert-manager + `cashback-tls` secret |
| `networkPolicy.enabled`                       | `true`                   | default-deny baseline + scrape allow |

The full list lives in [`values.yaml`](./values.yaml).

## Subcharts

Stateful systems are off by default — production deployments should use a
managed Postgres / Redis / Kafka / ClickHouse and point the chart at them
via the `config.*` block. Set `<chart>.enabled=true` to bundle the Bitnami
chart for a self-contained dev environment.

Bitnami deleted the versioned `docker.io/bitnami/*` images in 2025, so
`values.yaml` points every subchart image at `docker.io/bitnamilegacy/*`:
the same tags, frozen and unsupported (no security fixes). Use the
subcharts for dev/demo clusters only.

```yaml
postgresql: { enabled: true, auth: { username: cashback, database: cashback } }
redis:      { enabled: true }
kafka:      { enabled: true, controller: { replicaCount: 1 } }
clickhouse: { enabled: true, shards: 1, replicaCount: 1 }
```

## Secrets

The chart has no built-in secrets (stage B, `docs/adr/0013-auth-hardening.md`).
`secret.yaml` renders two Secrets:

| Secret              | Keys | Mounted into |
|---------------------|------|--------------|
| `<release>-secrets` | `POSTGRES_*`, `REDIS_PASSWORD`, `FCM_SERVER_KEY` | every workload (`envFrom`) |
| `<release>-auth`    | `JWT_SECRET`, `SERVICE_JWT_SECRET`, `MOBILE_JWT_SECRET`, `ADMIN_BOOTSTRAP_PASSWORD` | key by key (`secretKeyRef`), see below |

Each Deployment gets only the keys it needs: campaign-manager —
`JWT_SECRET` + `SERVICE_JWT_SECRET`; recommendation-api —
`SERVICE_JWT_SECRET`; mobile-api — `SERVICE_JWT_SECRET` +
`MOBILE_JWT_SECRET` (+ `config.mobileJwtIssuer`). A compromised mobile-api
or recommendation-api pod therefore cannot sign admin tokens.

Two modes:

1. **Inline (dev)** — set the values (`--set` or a values file kept out of
   git). `helm install` fails until `secrets.jwtSecret`,
   `secrets.serviceJwtSecret`, `secrets.mobileJwtSecret` (each ≥ 32 bytes,
   all different) and `secrets.adminBootstrapPassword` (≥ 12 characters)
   are set; known placeholders such as `please-change-me` are refused:

   ```bash
   helm install cashback helm/cashback -n cashback --create-namespace \
     --set secrets.jwtSecret=$(openssl rand -hex 32) \
     --set secrets.serviceJwtSecret=$(openssl rand -hex 32) \
     --set secrets.mobileJwtSecret=$(openssl rand -hex 32) \
     --set secrets.adminBootstrapPassword=$(openssl rand -base64 18) \
     --set campaignManager.forwardedAllowIps=<ingress controller pod CIDR>
   ```
2. **External-Secrets (prod)** — `externalSecrets.enabled=true` switches
   the rendering to two `kind: ExternalSecret` resources with the same
   target names. Provide a `ClusterSecretStore` (e.g. AWS Secrets Manager,
   Vault) named via `externalSecrets.secretStoreRef` holding
   `cashback/jwt.secret`, `cashback/service-jwt.secret`,
   `cashback/mobile-jwt.secret` and `cashback/admin-bootstrap.password`
   (plus `cashback/postgres`, `cashback/redis`, `cashback/fcm`). The three
   keys must all differ — the chart checks that only for values it renders
   itself, and no service holds both `JWT_SECRET` and `MOBILE_JWT_SECRET`.

`ADMIN_BOOTSTRAP_PASSWORD` is read by no Deployment: the chart ships no
migration Job, and whoever runs `alembic upgrade head` (db_migrations/)
passes it from `<release>-auth` (e.g. `envFrom`/`secretKeyRef` of a
one-off Job). The migration that creates `admin@bank.ru` uses it; later
changes of the value do not touch an existing account.

Behind the ingress every client reaches campaign-manager from a controller
pod, so the login throttle (5 failures per e-mail and client IP, 30 attempts
per IP, 15 min) would count all clients as one: 5 wrong passwords would lock
an e-mail for everybody. `campaignManager.forwardedAllowIps` — the ingress
controller's pod CIDR, rendered as `FORWARDED_ALLOW_IPS` — is therefore
required while `ingress.enabled=true` (rendering fails without it and on
`*`); uvicorn then takes the client from that controller's
`X-Forwarded-For`. Nothing wider: any pod in a trusted range can forge the
client IP.

`config.corsOrigins` (`CORS_ORIGINS`, comma-separated) is empty by default:
the UI is same-origin through the ingress; `*` is refused by the services.

## Verifying the chart

```bash
make helm-lint        # helm lint with throwaway random secrets
make helm-template
helm template helm/cashback --set externalSecrets.enabled=true \
  --set campaignManager.forwardedAllowIps=10.244.0.0/16 | grep -A4 "kind: ExternalSecret"
```

Package for distribution:

```bash
bash scripts/helm-package.sh                # → cashback-0.1.0.tgz
```

## Auto-scaling

| Service                | Trigger                                     | Range |
|------------------------|---------------------------------------------|-------|
| recommendation-api     | CPU 70 %                                    | 2 → 10 |
| campaign-manager       | CPU 70 %                                    | 2 → 6  |
| mobile-api             | CPU 70 %                                    | 2 → 8  |
| transaction-listener   | CPU 70 % **+** kafka_consumer_lag > 5 000   | 2 → 8  |
| frontend               | CPU 60 %                                    | 2 → 6  |

Scale-up policy: `Pods=2 / 30 s` (aggressive); scale-down:
`Pods=1 / 120-300 s` (conservative).

## Day-2

* `kubectl get hpa -n cashback`
* `kubectl describe networkpolicy -n cashback`
* `kubectl logs deploy/cashback-recommendation-api -n cashback`
* MLflow UI: not deployed by this chart (uses managed MLflow); set
  `config.mlflowTrackingUri` accordingly.
