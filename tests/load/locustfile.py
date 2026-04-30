"""Locust scenario for the CashBack stack.

Traffic mix (chapter 3.3):

    70 %  GET /recommendations/{user_id}            — Recommendation API
    20 %  POST /v1/mobile/recommendations/{rec}/respond  — Mobile BFF
    10 %  GET /campaigns/applicable/{user_id}       — Campaign Manager

Each user alternates between requesting recommendations, occasionally
responding to one, and querying which campaigns currently apply.
The user-id pool is loaded from ``LOCUST_USER_POOL_FILE`` (one UUID per
line) — falls back to a generated pool of 100 random UUIDs.

Run headless::

    locust -f tests/load/locustfile.py --headless \\
           -u 1000 -r 100 -t 10m \\
           --host http://localhost
"""
from __future__ import annotations

import os
import random
import uuid
from pathlib import Path

from locust import FastHttpUser, between, events, task


def _load_user_pool() -> list[str]:
    path = os.environ.get("LOCUST_USER_POOL_FILE")
    if path and Path(path).exists():
        return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]
    return [str(uuid.uuid4()) for _ in range(100)]


USER_POOL = _load_user_pool()


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
        self.user_id = random.choice(USER_POOL)
        self.last_recommendation_id: str | None = None
        # FastHttpUser exposes a single base host; we hit upstream services
        # directly with absolute URLs from the env.
        self.reco_base     = self.environment.parsed_options.reco_base
        self.mobile_base   = self.environment.parsed_options.mobile_base
        self.campaign_base = self.environment.parsed_options.campaign_base

    # ------------------------------------------------------------------
    # 70 %  GET recommendations
    # ------------------------------------------------------------------
    @task(70)
    def get_recommendations(self) -> None:
        url = f"{self.reco_base}/recommendations/{self.user_id}?top_k=5"
        with self.client.get(url, name="GET /recommendations/{id}",
                             catch_response=True) as resp:
            if resp.status_code == 404:
                # Cold-start user — soft success for the load mix.
                resp.success()
                return
            if resp.status_code >= 500:
                resp.failure(f"server error {resp.status_code}")
                return
            try:
                items = resp.json().get("recommendations", [])
                if items and items[0].get("campaign_id"):
                    self.last_recommendation_id = items[0]["campaign_id"]
                resp.success()
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"bad payload: {exc}")

    # ------------------------------------------------------------------
    # 20 %  POST respond — accept the most-recently-seen offer
    # ------------------------------------------------------------------
    @task(20)
    def respond_to_recommendation(self) -> None:
        rec_id = self.last_recommendation_id or str(uuid.uuid4())
        url = f"{self.mobile_base}/v1/mobile/recommendations/{rec_id}/respond"
        with self.client.post(
            url, json={"action": "ACCEPTED"},
            name="POST /respond",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 201, 404):
                resp.success()
            elif resp.status_code >= 500:
                resp.failure(f"server error {resp.status_code}")
            else:
                resp.success()

    # ------------------------------------------------------------------
    # 10 %  GET applicable campaigns
    # ------------------------------------------------------------------
    @task(10)
    def get_applicable_campaigns(self) -> None:
        url = f"{self.campaign_base}/campaigns/applicable/{self.user_id}?limit=20"
        with self.client.get(
            url, name="GET /campaigns/applicable/{id}",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404):
                resp.success()
            elif resp.status_code >= 500:
                resp.failure(f"server error {resp.status_code}")
            else:
                resp.success()


# ---------------------------------------------------------------------------
# Programmatic load shapes
# ---------------------------------------------------------------------------
try:
    from locust import LoadTestShape

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
except ImportError:
    pass
