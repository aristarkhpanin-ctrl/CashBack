# Security policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a vulnerability

**Don't open a public issue.** Please email `security@cashback.example.com`
with:

* the affected component (path in the repo, version / commit SHA);
* a minimal reproducer (or detailed description);
* the impact you observed and your assessed severity (CVSS v4 if possible);
* whether you've already disclosed the issue elsewhere.

We acknowledge incoming reports within **3 business days** and aim to have
a remediation plan within **14 days** for high / critical findings. We're
happy to credit reporters in the release notes; tell us if you'd prefer to
stay anonymous.

## In-scope

* All Python services under `services/`
* The frontend bundle under `frontend/`
* The Helm chart `helm/cashback/`
* The CI configuration `.github/workflows/`

## Out-of-scope

* Vulnerabilities in third-party dependencies — please report upstream.
  We track and bump our deps weekly via Dependabot
  (`.github/dependabot.yml`).
* Infrastructure issues in customer-managed deployments
  (Postgres / Redis / Kafka / ClickHouse). The chart wires these up but
  doesn't ship hardening for them — that's the operator's responsibility.

## Hardening defaults

Out-of-the-box this repo follows the following minimum security baseline:

* All container images are pinned by tag (no `:latest` in production
  manifests; only the canonical Helm `latest` alias).
* All Python services run as non-root (`runAsNonRoot: true`,
  `runAsUser: 1000`) and drop all capabilities.
* `NetworkPolicy` defaults to deny; only `cashback`-labelled pods,
  the cluster ingress controller, and the configured Prometheus
  namespace can reach pods (see `helm/cashback/templates/networkpolicy.yaml`).
* Secrets are templated through `external-secrets-operator` when
  `externalSecrets.enabled=true`; the inline-Secret mode is **dev-only**.
* Pre-commit hook `detect-private-key` blocks accidental key commits.
* `check-added-large-files` blocks files > 512 kB by default — flag
  binary blobs in the PR description.

## Disclosure

We follow a coordinated disclosure model. After patching:

1. We push the fix to `main` (release notes labelled `security`).
2. We wait 7 days for users to upgrade.
3. We then publicly credit the reporter (with consent) and document
   the issue in `SECURITY.md`'s "Past advisories" section.
