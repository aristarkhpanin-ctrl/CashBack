# 0003 — Apache Kafka (KRaft) as the message broker

* **Status:** Accepted
* **Date:** 2025-12-17
* **Driver:** chapter 2.3, table 11

## Context

The platform has multiple async event flows:

* `transactions.raw` — synthetic / production payment events (Avro).
* `recommendations.created` — fan-out to the notification pipeline.
* `cashback.accrued` — emitted by the accrual engine after a successful
  PG transaction.
* `offer.accepted` — published by mobile-api after a user accepts an
  offer (so other services can subscribe to "user-now-active" signals).

Hard requirements: Avro schema enforcement (Schema Registry), strict
per-key ordering for accrual (one user/MCC at a time), at-least-once
semantics with explicit consumer commits, retention 7 days, sustained
1 000+ msgs/sec.

## Alternatives

| Option            | Pros | Cons |
|-------------------|------|------|
| **Kafka 3.7 (KRaft)** | Battle-tested, partition-keyed ordering, Schema Registry support, mature Python clients (`aiokafka`, `confluent-kafka`). KRaft removes ZooKeeper. | Operational complexity (controllers, replicas) — mitigated by Bitnami Helm chart. |
| RabbitMQ 3.13     | Easier ops, flexible routing. | No partitioned ordering; replay is awkward; no first-class Avro story. |
| AWS SQS / GCP Pub/Sub | Managed, zero ops. | Vendor lock-in; lacks ordering guarantees we need; costs. |
| Apache Pulsar     | Geo-replication, tiered storage. | Smaller ecosystem; team unfamiliar; operationally heavier. |

Decision criteria: ordering 30, ecosystem 25, ops 20, replay 15, schema 10.

## Decision

We use **Apache Kafka 3.7 in KRaft mode** with **Confluent Schema Registry
7.6**. Locally a single-broker setup ships in `docker-compose.yml`
(`bitnami/kafka:3.7`); in Kubernetes the Bitnami subchart runs as a
StatefulSet (`helm/cashback/values.yaml: kafka.controller.replicaCount`).

Topic configuration (chapter 3.1, table 14) is enforced at producer
startup via `AdminClient.create_topics` / `alter_configs`:

```yaml
retention.ms:        604800000   # 7 days
compression.type:    zstd
max.message.bytes:   1048576
replication.factor:  1           # 3 in production
```

Avro records carry the schema-registry magic byte; deserialisation goes
through `confluent_kafka.schema_registry.avro.AvroDeserializer`.

## Consequences

* + `key=user_id` partitioning gives us single-flight per-user ordering
  for free (no global lock in the listener).
* + `enable_auto_commit=False` + manual `consumer.commit()` after a
  successful Parquet stage gives us at-least-once + exactly-once-effect
  via the idempotent `INSERT … ON CONFLICT (transaction_id, campaign_id)`
  in `cashback_accruals`.
* + Replay is trivial — set the consumer group offset back and re-run.
* − KRaft is younger than ZK; we accept the risk for a master's project.
* − One more stateful system to operate. We document `bitnami/kafka` as
  the canonical install path.

## References

* `services/transaction_listener/app/listener.py` — `cashback-accrual-group`.
* `services/etl/app/kafka_consumer.py` — `cashback-etl-group`, listing 3.2.
* `services/tx_simulator/app/simulator.py` — topic auto-create with
  table-14 settings.
* `infrastructure/kafka/schemas/transaction_event.avsc` — listing 1.
