# 0013 — Auth hardening: secrets, admin sessions, RBAC, SSE, customer and service tokens

* **Status:** Accepted
* **Date:** 2026-10-17
* **Fixes:** B19, B20, B35, B36, B12, B27 (`docs/AUDIT.md`), stage B

## Context

The audit found that the money-moving parts of the system were protected
only on paper:

* **B19** — one HS256 `JWT_SECRET` was shared by campaign_manager,
  recommendation_api and mobile_api, with a public default
  (`dev-secret-change-me`) in compose and an empty value accepted at start-up:
  anyone could mint an ADMIN token. A breach of the client-facing mobile_api
  handed out the admin signing key as well.
* **B20** — migration 002 created `admin@bank.ru / admin`; the seeder added
  demo logins whose passwords were printed in the README.
* **B35** — RBAC lived in the UI. The role was read from the token, so a
  demoted or deactivated admin kept access until the token expired; refresh
  tokens were reusable forever; bcrypt ran on the event loop and answered
  faster for unknown e-mails; the role-permission cache was per process.
* **B36** — `/events/stream` (SSE) needed no token, CORS allowed `*` with
  credentials, and every replica shared one Kafka consumer group.
* **B12** — mobile_api had no authentication: `/v1/mobile/*/{user_id}`
  served and accepted offers for any user id (IDOR).
* **B27** — the admin "ML explanations" page called the *serving* endpoint,
  which records recommendations, publishes Kafka events and updates the
  anti-fatigue / frequency counters.

### Threat model

| Asset | Why it matters |
|-------|----------------|
| Campaign configuration and budgets (campaign_manager) | Directly moves money: cashback rates, budgets, activation |
| ML kill switch / limits (`PUT /ml-limits`) | Turns the model off or loosens the BRE for every customer |
| Customers' offers and cashback history (mobile_api) | Personal data; accepting an offer arms a paid accrual |
| Recommendation log, Kafka events, BRE counters | Training labels and contact policy; polluting them skews the model and spams customers |
| Signing keys and the bootstrap admin password | Whoever holds them is any user |

| Actor / entry point | What they could do before |
|---------------------|---------------------------|
| Anonymous Internet client (nginx :3000, published ports :8001–8003) | Forge admin tokens with the public/empty key; log in as `admin/admin`; read SSE business metrics; read and accept any customer's offers |
| Authenticated customer (mobile app) | Read / answer other customers' offers by changing `{user_id}` / `{recommendation_id}` |
| Lower-privileged staff (MARKETER, ANALYST) | Call any API the UI merely hides; keep a role after demotion |
| Compromised mobile_api pod | Read `JWT_SECRET` and mint ADMIN tokens |
| Leaked or reused refresh token | Unlimited new access tokens, no detection |
| Admin opening "ML explanations" | Side effects on real customers (B27) |

Out of scope for this stage: network-level authentication of Postgres /
Redis / Kafka / ClickHouse (default credentials inside the compose network),
TLS termination, and the Airflow / Grafana / MLflow UIs.

## Alternatives

| Option | Pros | Cons |
|--------|------|------|
| Keep one shared HS256 key, just require a strong value | Smallest change | A breach of any service still forges every token type, including ADMIN |
| Asymmetric keys per issuer (RS256 / EdDSA + JWKS) | Verifiers hold no signing material; per-issuer keys; standard rotation (`kid`) | Key distribution and rotation tooling for a thesis demo; three services to migrate at once |
| Opaque server-side admin sessions (cookie + Redis) | Instant revocation, no XSS-readable token | Rewrites the SPA auth flow and CSRF handling; SSE / multi-tab work differently |
| **Three HS256 keys with typed claims + DB-backed checks per request + Redis refresh rotation (chosen)** | Each key has one purpose and one set of holders; revocation via `token_version` without a session store on the hot path; small, testable change | Symmetric keys: every verifier can also sign its own token type; no `kid` rotation |

Customer authentication: building a login for bank customers is not the job
of this system — the bank's IdP issues customer tokens. The demo replaces the
IdP with a script that signs tokens with `MOBILE_JWT_SECRET`; no runtime
endpoint mints customer tokens.

## Decision

### Keys and tokens (B19)

| Env var | Signs | Verifies | Purpose |
|---------|-------|----------|---------|
| `JWT_SECRET` | campaign_manager | campaign_manager | admin `access` / `refresh` / `sse` tokens |
| `SERVICE_JWT_SECRET` | mobile_api, campaign_manager | recommendation_api | `service` tokens into recommendation_api |
| `MOBILE_JWT_SECRET` | bank IdP (demo: `scripts/mint_customer_token.py`) | mobile_api | `customer` tokens |

* mobile_api and recommendation_api never receive `JWT_SECRET`; campaign_manager
  never receives `MOBILE_JWT_SECRET`. campaign_manager and mobile_api refuse
  equal values for their two keys.
* Every service validates its keys at start-up (pydantic Settings): required,
  no default in code, ≥ 32 bytes UTF-8, not a known placeholder
  (`dev-secret-change-me`, `please-change-me`, `change-me`, `changeme`,
  `secret`, `test-secret`). There is no bypass flag; unit tests set test keys
  in their conftest; recommendation_api's `AUTH_ENABLED` switch is gone.
* All tokens are HS256 with `iat`, `exp`, `iss`, `aud`, `type`; decoding checks
  all of them, so a token of another type or audience is 401:

| type | iss | aud | sub | TTL |
|------|-----|-----|-----|-----|
| `access` | `cashback-campaign-manager` | `cashback-admin` | admin id | 15 min (+ `email`, `role`, `ver`, `jti`, `pwd_change`) |
| `refresh` | `cashback-campaign-manager` | `cashback-admin` | admin id | 7 days (+ `ver`, `jti`, `fam`) |
| `sse` | `cashback-campaign-manager` | `cashback-sse` | admin id | 60 s (+ `ver`, `jti`) |
| `service` | `mobile-api` / `campaign-manager` | `recommendation-api` | = iss | 5 min |
| `customer` | `MOBILE_JWT_ISSUER` (demo `cashback-demo-idp`) | `cashback-mobile` | customer UUID | set by the IdP (demo 1 h) |

### Admin sessions (B20, B35)

* No built-in password. Migration 002 creates `admin@bank.ru` with
  `ADMIN_BOOTSTRAP_PASSWORD` (≥ 12 chars, not a known default) or, if unset,
  a random password printed once (`bootstrap admin password: …`). Migration
  008 adds `must_change_password`, `token_version`, `password_changed_at`;
  on an existing database it replaces the legacy `admin` hash with the
  bootstrap password and the old seeder's public demo passwords
  (`marketer` / `analyst`, recognised by their hashes) with
  `DEMO_USERS_PASSWORD` — each random and printed once when its variable is
  unset — and both must be changed at first login; a password 002 generated
  must be changed too. Hashing runs in Postgres (pgcrypto
  `crypt(…, gen_salt('bf', 12))`).
* `POST /auth/login`: one bcrypt check per request in a thread pool (a dummy
  hash for unknown e-mails — same timing and body for unknown / wrong /
  inactive); throttling in Redis — 5 failures per (email, client IP) and 30
  attempts per IP per 15 min → 429 + `Retry-After`; fail-open with an error
  log when Redis is down (the admin panel stays usable). The window is set
  by `EXPIRE` on the first attempt (no `EXPIRE … NX`, which Redis < 7.0
  rejects — that turned every attempt into a silent fail-open). The client
  IP is uvicorn's: `X-Forwarded-For` is honoured only from
  `FORWARDED_ALLOW_IPS` — compose pins the frontend nginx to `172.28.0.10`
  and trusts exactly that address, Helm requires
  `campaignManager.forwardedAllowIps` (the ingress controller's pod CIDR)
  while the ingress is on.
* A generated password must be changed first: such an access token carries
  `pwd_change` and gets 403 `password change required` everywhere except
  `GET /auth/me` and `POST /auth/change-password`.
* Refresh rotation: every refresh `jti` is single-use (`GETDEL` of
  `auth:refresh:{jti}`); presenting a used one revokes the whole family;
  `POST /auth/logout` revokes it explicitly; Redis down → 503 (fail closed).
* Every request loads the admin row by `sub`: missing, inactive or a stale
  `ver` (≠ `token_version`) → 401. The role used for authorization is the
  database role. Changing a role, deactivating or changing / resetting a
  password bumps `token_version`, which revokes access, refresh and SSE
  tokens at once. `POST /auth/change-password` and `PATCH /auth/users/{id}`
  take the row with `SELECT … FOR UPDATE`, and change-password checks the
  token's `ver` again under the lock: concurrent writes cannot collapse two
  bumps into one or let the account holder's password overwrite an admin's
  reset.

### Authorization (B35)

* `require_permission(key)` reads the role's `role_permissions` row on every
  request (no process cache — replicas agree immediately); `PATCH
  /roles/{role}/permissions` is one atomic JSONB merge.
* Every route has exactly one requirement; `tests/unit/test_route_permissions.py`
  walks `app.routes` and fails for a route that is missing from the table or
  has a weaker requirement.
* Non-delegable, ADMIN role only: editing the permission matrix, `PUT
  /ml-limits`, creating / promoting an ADMIN and changing an ADMIN account.

### Realtime and CORS (B36)

* `POST /events/token` (permission `dashboard`) returns a 60-second `sse`
  token; `GET /events/stream?token=…` is 401 without a valid one. The token
  is single-use (`SET NX` of `auth:sse_used:{jti}` at connect; Redis down →
  503), so a URL copied from a proxy or ingress log opens nothing; nginx
  does not access-log the SSE location. `dashboard` is checked again at
  connect, and an open stream re-checks the user (active, `ver`,
  `dashboard`) every 60 s and ends when access is gone. Per-process
  subscriber cap (`SSE_MAX_SUBSCRIBERS`, 503 when full) and per-user cap
  (`SSE_MAX_SUBSCRIBERS_PER_USER`, default 5, 429), one Kafka consumer
  group per process (`campaign-manager-sse-{hostname}-{pid}`, latest, no
  commits) with a supervised reconnect, open streams end on shutdown.
* `CORS_ORIGINS` is an explicit list (default `[]`, the UI is same-origin
  behind nginx); `*` is rejected because credentials are allowed.

### Customers (B12)

* Every `/v1/mobile/*` route requires a `customer` token; the path `{user_id}`
  must equal `sub` (403 `forbidden`, checked before any DB / upstream call);
  answering another customer's recommendation is 404, exactly like an unknown
  id. mobile_api calls recommendation_api with its own `service` token.
* Demo IdP: `python3 scripts/mint_customer_token.py --user-id <uuid> [--ttl
  3600]` signs with `MOBILE_JWT_SECRET` / `MOBILE_JWT_ISSUER` from the
  environment or `.env`; root tests, e2e and locust mint tokens the same way
  (`tests/auth_tokens.py`). Production replaces it with the bank IdP: same
  claims, its own issuer in `MOBILE_JWT_ISSUER`.

### ML explanations without side effects (B27)

* recommendation_api accepts only `service` tokens: serving
  (`GET /recommendations/{id}`) only from `iss mobile-api`, the new read-only
  `GET /recommendations/{id}/explain` only from `iss campaign-manager` (wrong
  issuer → 403, invalid token → 401). Explain inserts no row, publishes no
  event, skips the side-effecting BRE rules R3 / R4 and reads budgets without
  row locks.
* The admin page calls campaign_manager `GET /ml/explain/{user_id}`
  (permission `analytics`), which proxies with a service token.

### Operations

* `.env.example` lists every secret empty. `make env` creates `.env` (mode
  600) and fills keys with `openssl rand -hex 32` and passwords with
  `openssl rand -base64 18`; it never overwrites an existing `.env` and reports
  keys that are still empty or equal to each other in it (no service holds
  both `JWT_SECRET` and `MOBILE_JWT_SECRET`, so none can compare them).
  Quick start: `make env && make up`.
* docker-compose passes each service only its keys as `${VAR:?run make env}`
  (no defaults), so compose refuses to start without them;
  `scripts/init_db.sh` hands `ADMIN_BOOTSTRAP_PASSWORD` and
  `DEMO_USERS_PASSWORD` to the migration run.
  The seeder's demo logins get `DEMO_USERS_PASSWORD` (random and printed if
  unset) and lose the old public passwords.
* Helm: `secrets.jwtSecret`, `serviceJwtSecret`, `mobileJwtSecret`,
  `adminBootstrapPassword` are `required` (length / placeholder / distinctness
  checked at render time) unless `externalSecrets.enabled`; they live in a
  separate `<release>-auth` Secret (or ExternalSecret) read key by key, so
  each Deployment mounts only its own keys.
* CI integration jobs generate the three keys per run; nightly-smoke runs
  `make env` first, logs in with the bootstrap password from `.env`, checks
  that `admin/admin` and unauthenticated SSE / mobile / recommendation calls
  are refused, and runs the e2e money path with customer tokens.

## Consequences

* + A leaked mobile_api or recommendation_api pod cannot mint admin tokens;
  a leaked customer-token key cannot call recommendation_api or the admin API.
* + Demotion, deactivation and password changes take effect on the next
  request; refresh-token theft is detected on reuse and kills the family.
* + A new route without a permission fails CI; the UI shows what the server
  enforces.
* + The explanation page no longer changes what customers see.
* − One extra primary-key query per admin request (two for non-ADMIN roles:
  the user row and the role's permission row).
* − Redis is now on the admin login / refresh path (throttle fail-open,
  refresh fail-closed).
* − Developers must run `make env` once; a lost `.env` with a generated
  bootstrap password means resetting the admin password in the database.
* − Key rotation logs everybody out (no `kid`, no overlap window).

## Residual risks

* **Tokens in the browser.** The refresh token lives in `localStorage`, so an
  XSS can steal it (reuse is detected only when the legitimate tab refreshes).
  Two tabs refreshing at the same instant on a non-secure origin (no Web
  Locks) look like reuse and log the user out.
* **Open SSE streams** are re-checked every 60 s (plus up to one 15 s
  heartbeat): a deactivated or demoted user keeps receiving events for at
  most that long. The SSE token still travels in the query string (single
  use, 60 s); the ingress controller in Helm logs it, but a logged token is
  already spent.
* **Access tokens outlive logout.** Logout and refresh-reuse detection revoke
  the refresh family only (contract §3): the family's access token stays
  valid until it expires (≤ 15 min), its SSE stream until the next re-check.
  Deactivation or a password reset (`token_version`) ends both at once —
  the response to a suspected theft.
* **Client IP behind a proxy.** Clients behind one NAT or corporate proxy
  still share the per-IP limit (30 attempts / 15 min) and the (email, IP)
  lockout. A `FORWARDED_ALLOW_IPS` / `campaignManager.forwardedAllowIps`
  wider than the proxy's own address lets anyone in that range forge
  `X-Forwarded-For`.
* **Distinct keys are a convention outside Helm inline mode.** campaign_manager
  never sees `MOBILE_JWT_SECRET` (nor mobile_api `JWT_SECRET`), so neither can
  refuse them being equal; `make env` warns about an existing `.env` and Helm
  checks only the values it renders itself (not `externalSecrets`).
* **Throttle fail-open**: with Redis down, login has no brute-force limit
  (logged as an error).
* **Symmetric service key.** mobile_api and campaign_manager share
  `SERVICE_JWT_SECRET`, so either can sign a token with the other's `iss`
  (worst case: mobile_api reaches the read-only explain endpoint).
  Per-issuer or asymmetric keys would close it.
* **Customer tokens** are stateless and cannot be revoked before `exp`. In the
  demo anyone holding `.env` (`MOBILE_JWT_SECRET`) can mint a token for any
  customer — acceptable for a demo IdP, not for production.
* **Passwords in SQL.** Migrations and the seeder send the plain bootstrap /
  demo password to Postgres inside the statement (pgcrypto); a server with
  `log_statement = all` would log it.
* Admin-created or admin-reset accounts are not forced to change their
  password and keep the old 6-character minimum.
* `/metrics`, `/docs`, `/openapi.json` of campaign_manager are reachable
  through the nginx `/api/campaigns/` proxy; mobile_api's 502 detail includes
  the internal recommendation_api URL.
* Infrastructure services (Postgres, Redis, Kafka, ClickHouse, MLflow,
  Airflow) still use default credentials inside the compose network.

## References

* `docs/AUDIT.md` — B12, B19, B20, B27, B35, B36.
* `services/campaign_manager/app/security.py`, `app/api/auth.py`, `app/rbac.py`,
  `app/api/events.py`, `app/api/ml_explain.py`,
  `tests/unit/test_route_permissions.py`.
* `db_migrations/migrations/versions/002_admin_users.py`, `008_auth_hardening.py`.
* `services/mobile_api/app/security.py`, `app/service_token.py`;
  `scripts/mint_customer_token.py`.
* `services/recommendation_api/app/security.py`, `app/api/recommendations.py`.
* `.env.example`, `Makefile` (`make env`), `docker-compose.yml`,
  `scripts/init_db.sh`, `helm/cashback/templates/secret.yaml`.
* `tests/auth_tokens.py`, `tests/integration/test_stack_secrets.py`,
  `tests/e2e/test_full_cashback_cycle.py`, `.github/workflows/nightly-smoke.yml`.
