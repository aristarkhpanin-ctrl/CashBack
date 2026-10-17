"""Locust scenario for the CashBack stack.

Traffic mix (chapter 3.3):

    70 %  GET /recommendations/{user_id}            — Recommendation API
    20 %  POST /v1/mobile/recommendations/{rec}/respond  — Mobile BFF
    10 %  GET /campaigns/applicable/{user_id}       — Campaign Manager

Each user alternates between requesting recommendations, occasionally
responding to one, and querying which campaigns currently apply.
Responding takes an offer the way the app does: ``GET
/v1/mobile/recommendations/{user_id}`` first (mobile_api persists the offer
and returns its ``recommendation_id`` — rec_api's own payload has none),
the user stores ``recommendation_id`` of the first offer and accepts it with
``POST …/respond``. The next respond is a repeated accept of the same offer
(a double tap): an offer is answered once (B13), so that one must get 409
(410 once the offer has expired) and is reported as ``POST /respond
(repeat)``; after it the user opens a fresh offer.

Offers are opened for pool users the 70 % traffic never asks for: every
rec_api answer goes through the BRE, which allows a user
``FREQUENCY_GATE_PER_24H`` (5) offers a day and hides each shown campaign
for ``ANTI_FATIGUE_TTL_DAYS`` (14) — after one ``/recommendations`` call a
user has nothing left to accept. The pool is therefore split: even lines
drive ``/recommendations`` and ``/campaigns/applicable``, odd lines are
walked one by one to open offers. An opened offer list with nothing to
accept fails (``GET /v1/mobile/recommendations/{id}``) instead of silently
skipping the respond share; it means the offer half of the pool is used up
— pass a larger pool, or reseed / clear ``rec_count:*`` and
``recent_offer:*`` in Redis between runs.

Every other non-2xx answer is a failure — 401 (missing token), 404 (unknown
user / offer) and a 409 on a first accept included; a load report full of
"successful" 401s measures nothing. Hence the scenario authenticates like
the real clients (stage B, ADR-0013; secrets from the environment, else the
repo-root ``.env`` of ``make env`` — ``tests/auth_tokens.py``):

* recommendation_api gets the service token mobile_api sends (``iss
  mobile-api``, ``aud recommendation-api``, signed with
  ``SERVICE_JWT_SECRET``, renewed before its 5 min expire);
* mobile_api gets the customer's own token from the demo IdP
  (``MOBILE_JWT_SECRET``): the offers of a user are opened and answered with
  that user's token;
* campaign_manager gets an admin access token from ``/auth/login``
  (``LOAD_ADMIN_EMAIL``, password ``LOAD_ADMIN_PASSWORD`` or else
  ``ADMIN_BOOTSTRAP_PASSWORD``);
* the user-id pool must be real users with features (seeded stack):
  ``LOCUST_USER_POOL_FILE`` — one UUID per line, e.g.
  ``psql … -Atc "SELECT user_id FROM users" > users.txt``. Without it a pool
  of 100 random UUIDs is used, and recommendation_api answers 404 for them.

Run headless::

    locust -f tests/load/locustfile.py --headless \\
           -u 1000 -r 100 -t 10m \\
           --host http://localhost
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import random
import time
import urllib.request
import uuid
from pathlib import Path

from locust import FastHttpUser, between, events, task


def _load_auth_tokens():
    """``tests/auth_tokens.py`` by path: locust puts only tests/load on sys.path."""
    path = Path(__file__).resolve().parents[1] / "auth_tokens.py"
    spec = importlib.util.spec_from_file_location("_cashback_auth_tokens", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


auth_tokens = _load_auth_tokens()


def _load_user_pool() -> list[str]:
    path = os.environ.get("LOCUST_USER_POOL_FILE")
    if path and Path(path).exists():
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    return [str(uuid.uuid4()) for _ in range(100)]


USER_POOL = _load_user_pool()
# Disjoint halves (see the module docstring); a one-line pool is shared.
RECO_USERS = USER_POOL[::2]
OFFER_USERS = itertools.cycle(USER_POOL[1::2] or USER_POOL)

# Read once at start-up: a missing secret stops the run with a clear error
# instead of a report full of 401s.
SERVICE_JWT_SECRET = auth_tokens.require_env("SERVICE_JWT_SECRET")
MOBILE_JWT_SECRET = auth_tokens.require_env("MOBILE_JWT_SECRET")
ADMIN_EMAIL = os.environ.get("LOAD_ADMIN_EMAIL", "admin@bank.ru")
ADMIN_PASSWORD = (os.environ.get("LOAD_ADMIN_PASSWORD")
                  or auth_tokens.require_env("ADMIN_BOOTSTRAP_PASSWORD"))
# campaign_manager access tokens live 15 min (access_token_ttl_minutes).
ADMIN_TOKEN_MAX_AGE_S = 10 * 60
# Service tokens live 5 min (contract §2): renewed after 4.
SERVICE_TOKEN_MAX_AGE_S = 4 * 60


_service_token: dict[str, object] = {"token": None, "at": 0.0}


def service_auth() -> dict[str, str]:
    """mobile_api's service token into recommendation_api, shared by a worker."""
    age = time.monotonic() - float(_service_token["at"])
    if _service_token["token"] is None or age > SERVICE_TOKEN_MAX_AGE_S:
        _service_token["token"] = auth_tokens.service_token(
            auth_tokens.ISSUER_MOBILE_API, secret=SERVICE_JWT_SECRET)
        _service_token["at"] = time.monotonic()
    return auth_tokens.bearer(str(_service_token["token"]))


def customer_auth(user_id: str) -> dict[str, str]:
    """The customer's own token (demo IdP): mobile_api serves only ``sub``."""
    return auth_tokens.bearer(
        auth_tokens.customer_token(user_id, secret=MOBILE_JWT_SECRET))


_admin_token: dict[str, object] = {"token": None, "at": 0.0}


def admin_token(campaign_base: str) -> str:
    """Admin access token from campaign_manager, shared by all users of a worker."""
    age = time.monotonic() - float(_admin_token["at"])
    if _admin_token["token"] is None or age > ADMIN_TOKEN_MAX_AGE_S:
        req = urllib.request.Request(
            f"{campaign_base}/auth/login",
            data=json.dumps({"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            _admin_token["token"] = json.load(resp)["access_token"]
        _admin_token["at"] = time.monotonic()
    return str(_admin_token["token"])


def _expect_2xx(resp) -> bool:
    """Mark anything but 2xx as a failure (status 0 = connection error)."""
    if 200 <= resp.status_code < 300:
        return True
    resp.failure(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    return False


@events.init_command_line_parser.add_listener
def _add_args(parser):
    parser.add_argument(
        "--reco-base", default="http://localhost:8001",
        help="Recommendation API base URL",
    )
    parser.add_argument(
        "--mobile-base", default="http://localhost:8003",
        help="Mobile BFF base URL",
    )
    parser.add_argument(
        "--campaign-base", default="http://localhost:8002",
        help="Campaign Manager base URL",
    )


class CashbackUser(FastHttpUser):
    """Models a real-world load mix."""

    wait_time = between(0.05, 0.3)
    network_timeout = 5.0
    connection_timeout = 5.0

    def on_start(self) -> None:
        self.user_id = random.choice(RECO_USERS)
        # FastHttpUser exposes a single base host; we hit upstream services
        # directly with absolute URLs from the env.
        self.reco_base     = self.environment.parsed_options.reco_base
        self.mobile_base   = self.environment.parsed_options.mobile_base
        self.campaign_base = self.environment.parsed_options.campaign_base
        # The offer this user opened in the app (mobile_api recommendation_id),
        # the token of the customer it belongs to and whether it has been
        # accepted already.
        self.offer_id: str | None = None
        self.offer_auth: dict[str, str] = {}
        self.offer_accepted = False

    # ------------------------------------------------------------------
    # 70 %  GET recommendations
    # ------------------------------------------------------------------
    @task(70)
    def get_recommendations(self) -> None:
        url = f"{self.reco_base}/recommendations/{self.user_id}?top_k=5"
        with self.client.get(url, name="GET /recommendations/{id}",
                             headers=service_auth(), catch_response=True) as resp:
            if not _expect_2xx(resp):
                return
            try:
                resp.json()["recommendations"]
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"bad payload: {exc}")
                return
            resp.success()

    # ------------------------------------------------------------------
    # 20 %  POST respond — accept the stored offer; the next respond repeats it
    # ------------------------------------------------------------------
    def _open_offer(self) -> str | None:
        """Open the offers of the next offer-pool user; the first ``recommendation_id``.

        Offers without a campaign are not persisted by mobile_api and carry
        ``recommendation_id = null`` — there is nothing to accept on them.
        The offer is answered with the same customer's token (``offer_auth``).
        """
        user_id = next(OFFER_USERS)
        self.offer_auth = customer_auth(user_id)
        url = f"{self.mobile_base}/v1/mobile/recommendations/{user_id}?top_k=5"
        with self.client.get(url, name="GET /v1/mobile/recommendations/{id}",
                             headers=self.offer_auth, catch_response=True) as resp:
            if not _expect_2xx(resp):
                return None
            try:
                items = resp.json()["recommendations"]
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"bad payload: {exc}")
                return None
            offer_id = next((i["recommendation_id"] for i in items
                             if i.get("recommendation_id")), None)
            if offer_id is None:
                # No user id in the message: one aggregated line in the report.
                resp.failure("no offer to accept: offer pool used up by the BRE "
                             "frequency gate / anti-fatigue")
                return None
            resp.success()
        return offer_id

    @task(20)
    def respond_to_recommendation(self) -> None:
        if self.offer_id is None:
            self.offer_id = self._open_offer()
            self.offer_accepted = False
            if self.offer_id is None:
                return  # reported as a failure by _open_offer
        url = f"{self.mobile_base}/v1/mobile/recommendations/{self.offer_id}/respond"

        if not self.offer_accepted:
            with self.client.post(url, json={"action": "ACCEPTED"},
                                  headers=self.offer_auth,
                                  name="POST /respond", catch_response=True) as resp:
                if resp.status_code == 410:
                    # Expired between opening and accepting: a valid answer.
                    self.offer_id = None
                    resp.success()
                elif _expect_2xx(resp):
                    self.offer_accepted = True
                    resp.success()
                else:
                    self.offer_id = None
            return

        # Double tap on the accepted offer: answered once (B13), so 409 —
        # or 410 once it has expired — is the expected outcome; a 2xx would
        # mean a paid offer can be re-armed.
        self.offer_id = None
        with self.client.post(url, json={"action": "ACCEPTED"},
                              headers=self.offer_auth,
                              name="POST /respond (repeat)", catch_response=True) as resp:
            if resp.status_code in (409, 410):
                resp.success()
            else:
                resp.failure(f"repeated accept answered HTTP {resp.status_code} "
                             f"(expected 409/410): {(resp.text or '')[:200]}")

    # ------------------------------------------------------------------
    # 10 %  GET applicable campaigns
    # ------------------------------------------------------------------
    @task(10)
    def get_applicable_campaigns(self) -> None:
        url = f"{self.campaign_base}/campaigns/applicable/{self.user_id}?limit=20"
        auth = {"Authorization": f"Bearer {admin_token(self.campaign_base)}"}
        with self.client.get(
            url, name="GET /campaigns/applicable/{id}",
            headers=auth, catch_response=True,
        ) as resp:
            if _expect_2xx(resp):
                resp.success()


# ---------------------------------------------------------------------------
# Programmatic load shapes
# ---------------------------------------------------------------------------
# Locust runs ANY LoadTestShape found in the locustfile and then ignores
# --users / --spawn-rate / --run-time, i.e. LOAD_USERS / LOAD_DURATION of
# `make test-load`. The ramp is therefore opt-in: LOAD_STEP_SHAPE=1.
try:
    from locust import LoadTestShape
except ImportError:
    LoadTestShape = None

if LoadTestShape is not None and os.environ.get("LOAD_STEP_SHAPE", "").lower() in {"1", "true", "yes"}:

    class StepShape(LoadTestShape):
        """Stepwise ramp 100 → 1200 RPS over 10 minutes."""

        stages = [
            {"duration":   60, "users":  100, "spawn_rate":  20},
            {"duration":  180, "users":  300, "spawn_rate":  40},
            {"duration":  300, "users":  600, "spawn_rate":  60},
            {"duration":  420, "users":  900, "spawn_rate":  80},
            {"duration":  600, "users": 1200, "spawn_rate": 100},
        ]

        def tick(self):
            run_time = self.get_run_time()
            for stage in self.stages:
                if run_time < stage["duration"]:
                    return stage["users"], stage["spawn_rate"]
            return None
