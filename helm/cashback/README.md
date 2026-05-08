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
* For the optional bundled subcharts (Postgres / Redis / Kafka / ClickHouse):

  ```bash
  helm repo add bitnami https://charts.bitnami.com/bitnami
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

```yaml
postgresql: { enabled: true, auth: { username: cashback, database: cashback } }
redis:      { enabled: true }
kafka:      { enabled: true, controller: { replicaCount: 1 } }
clickhouse: { enabled: true, shards: 1, replicaCount: 1 }
```

## Secrets

Two modes:

1. **Inline (dev)** — populate `secrets.*` in `values.yaml` (`secret.yaml`
   renders an `Opaque` Secret). Safe for local clusters only.
2. **External-Secrets (prod)** — `externalSecrets.enabled=true` switches
   the rendering to `kind: ExternalSecret`. Provide a
   `ClusterSecretStore` (e.g. AWS Secrets Manager, Vault) named via
   `externalSecrets.secretStoreRef`.

## Verifying the chart

```bash
helm lint helm/cashback
helm template helm/cashback --debug | head -100
helm template helm/cashback --set externalSecrets.enabled=true | grep -A4 "kind: ExternalSecret"
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
