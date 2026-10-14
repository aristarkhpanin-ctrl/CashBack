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
"successful" 401s measures nothing. Hence:

* recommendation_api gets a ``type: service`` HS256 token signed with the
  shared ``JWT_SECRET`` (env ``LOAD_JWT_SECRET``, compose default
  ``dev-secret-change-me``) — what mobile_api sends;
* campaign_manager gets an admin access token from ``/auth/login``
  (``LOAD_ADMIN_EMAIL`` / ``LOAD_ADMIN_PASSWORD``);
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

import base64
import hashlib
import hmac
import itertools
import json
import os
import random
import time
import urllib.request
import uuid
from pathlib import Path

from locust import FastHttpUser, between, events, task


def _load_user_pool() -> list[str]:
    path = os.environ.get("LOCUST_USER_POOL_FILE")
    if path and Path(path).exists():
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    return [str(uuid.uuid4()) for _ in range(100)]


USER_POOL = _load_user_pool()
# Disjoint halves (see the module docstring); a one-line pool is shared.
RECO_USERS = USER_POOL[::2]
OFFER_USERS = itertools.cycle(USER_POOL[1::2] or USER_POOL)

JWT_SECRET = os.environ.get("LOAD_JWT_SECRET", "dev-secret-change-me")
ADMIN_EMAIL = os.environ.get("LOAD_ADMIN_EMAIL", "admin@bank.ru")
ADMIN_PASSWORD = os.environ.get("LOAD_ADMIN_PASSWORD", "admin")
# campaign_manager access tokens live 15 min (access_token_ttl_minutes).
ADMIN_TOKEN_MAX_AGE_S = 10 * 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def service_token(secret: str = JWT_SECRET, ttl_s: int = 24 * 3600) -> str:
    """HS256 ``type: service`` JWT — the claims mobile_api mints for rec_api."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"sub": "load-test", "type": "service", "iat": now, "exp": now + ttl_s}
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in (header, claims)
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


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
        self.reco_auth = {"Authorization": f"Bearer {service_token()}"}
        # The offer this user opened in the app (mobile_api recommendation_id)
        # and whether it has been accepted already.
        self.offer_id: str | None = None
        self.offer_accepted = False

    # ------------------------------------------------------------------
    # 70 %  GET recommendations
    # ------------------------------------------------------------------
    @task(70)
    def get_recommendations(self) -> None:
        url = f"{self.reco_base}/recommendations/{self.user_id}?top_k=5"
        with self.client.get(url, name="GET /recommendations/{id}",
                             headers=self.reco_auth, catch_response=True) as resp:
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
        """
        user_id = next(OFFER_USERS)
        url = f"{self.mobile_base}/v1/mobile/recommendations/{user_id}?top_k=5"
        with self.client.get(url, name="GET /v1/mobile/recommendations/{id}",
                             catch_response=True) as resp:
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
