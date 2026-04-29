#!/usr/bin/env python3
"""scripts/seed_recommendations.py

Seeds synthetic ``recommendations`` rows so the ML ranker has labelled
training data on the first run.

* Inserts a small set of demo cashback campaigns if the table is empty
* For each user, generates 1-5 recommendations across the 30 days
* Labels each one with ACCEPTED / DECLINED / EXPIRED via a binomial
  draw whose ``accept_rate`` depends on the user's segment_id
  (5–15% range, per the task spec).

Run from the host once Postgres is up and the ETL has populated users::

    python3 scripts/seed_recommendations.py --count 5
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2
from psycopg2.extras import execute_values


DEFAULT_DSN = "postgresql://cashback:cashback@localhost:5432/cashback"

# (mcc_code, name, cashback_rate, channels)
DEMO_CAMPAIGNS = [
    ("5411", "Grocery boost",       3.0, ["ONLINE", "POS", "MOBILE"]),
    ("5812", "Restaurant Friday",   5.0, ["POS", "MOBILE"]),
    ("5541", "Fuel club",           2.5, ["POS"]),
    ("5912", "Pharmacy care",       4.0, ["POS", "ONLINE", "MOBILE"]),
    ("5732", "Electronics quarter", 7.5, ["ONLINE", "POS"]),
    ("4111", "Transport monthly",   2.0, ["MOBILE", "ONLINE"]),
    ("7011", "Hotel weekend",       6.0, ["ONLINE", "MOBILE"]),
]

# Segment-level base accept rates (5–15%).
ACCEPT_RATE_BY_SEGMENT = {
    1: 0.05, 2: 0.06, 3: 0.07, 4: 0.08, 5: 0.10,
    6: 0.10, 7: 0.11, 8: 0.13, 9: 0.14, 10: 0.15,
}


def ensure_campaigns(cur, rng: random.Random) -> list[tuple[str, str]]:
    cur.execute("SELECT campaign_id::text, mcc_code FROM cashback_campaigns "
                "JOIN campaign_categories USING (campaign_id) "
                "LIMIT 500")
    existing = cur.fetchall()
    if existing:
        return existing

    print(">> seeding demo campaigns...")
    campaigns: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc)
    for mcc, name, rate, channels in DEMO_CAMPAIGNS:
        campaign_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO cashback_campaigns (
                campaign_id, name, target_segment_ids, cashback_rate,
                min_transaction_amount, budget_total, status,
                start_date, end_date, allowed_channels,
                require_existing_behavior, rate_tiers
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s, false, %s
            )
            """,
            (
                campaign_id, name,
                rng.sample(range(1, 11), 4),  # target segments
                rate, 100.0, 1_000_000.0,
                now - timedelta(days=30),
                now + timedelta(days=60),
                channels,
                json.dumps(
                    [
                        {"min_amount": 0,    "max_amount": 5000,  "rate": rate},
                        {"min_amount": 5000, "max_amount": 20000, "rate": rate + 1.0},
                        {"min_amount": 20000, "max_amount": None, "rate": rate + 2.0},
                    ]
                ),
            ),
        )
        cur.execute(
            "INSERT INTO campaign_categories (campaign_id, mcc_code, min_transaction_amount) "
            "VALUES (%s, %s, %s)",
            (campaign_id, mcc, 100.0),
        )
        campaigns.append((campaign_id, mcc))
    print(f"   inserted {len(campaigns)} campaigns")
    return campaigns


def fetch_users(cur, limit: int) -> list[tuple[str, int]]:
    cur.execute(
        "SELECT user_id::text, COALESCE(segment_id, 5) AS segment_id "
        "FROM users ORDER BY created_at LIMIT %s",
        (limit,),
    )
    return cur.fetchall()


def make_recommendation_rows(
    users: list[tuple[str, int]],
    campaigns: list[tuple[str, str]],
    *,
    per_user_min: int,
    per_user_max: int,
    rng: random.Random,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    now = datetime.now(timezone.utc)
    for user_id, segment_id in users:
        n = rng.randint(per_user_min, per_user_max)
        for _ in range(n):
            campaign_id, mcc = rng.choice(campaigns)
            score = round(rng.uniform(0.10, 0.95), 4)
            generated_at = now - timedelta(
                days=rng.randint(0, 30),
                hours=rng.randint(0, 23),
                minutes=rng.randint(0, 59),
            )
            expires_at = generated_at + timedelta(days=7)

            accept_rate = ACCEPT_RATE_BY_SEGMENT.get(segment_id, 0.08)
            r = rng.random()
            if r < accept_rate:
                status = "ACCEPTED"
            elif r < accept_rate + 0.5:
                status = "DECLINED"
            elif r < accept_rate + 0.7:
                status = "EXPIRED"
            elif r < accept_rate + 0.85:
                status = "SNOOZE"
            else:
                status = "PENDING"

            rows.append(
                (
                    str(uuid.uuid4()),
                    user_id,
                    campaign_id,
                    mcc,
                    score,
                    generated_at,
                    status,
                    expires_at,
                )
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic recommendations")
    parser.add_argument(
        "--dsn", default=os.getenv("POSTGRES_DSN_LOCAL", DEFAULT_DSN),
        help="Postgres DSN (default: localhost)",
    )
    parser.add_argument("--user-limit", type=int, default=10_000)
    parser.add_argument("--count-min", type=int, default=1)
    parser.add_argument("--count-max", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    print(f">> connecting to {args.dsn.split('@')[-1]} ...")
    with psycopg2.connect(args.dsn) as conn, conn.cursor() as cur:
        campaigns = ensure_campaigns(cur, rng)
        if not campaigns:
            print("!! no campaigns and could not create — aborting", file=sys.stderr)
            return 1

        users = fetch_users(cur, args.user_limit)
        if not users:
            print("!! users table is empty — run `make seed-users` first",
                  file=sys.stderr)
            return 1
        print(f"   loaded {len(users)} users + {len(campaigns)} campaigns")

        rows = make_recommendation_rows(
            users, campaigns,
            per_user_min=args.count_min,
            per_user_max=args.count_max,
            rng=rng,
        )
        print(f">> inserting {len(rows)} recommendations ...")

        execute_values(
            cur,
            """
            INSERT INTO recommendations (
                recommendation_id, user_id, campaign_id, mcc_code,
                model_score, generated_at, response_status, expires_at
            ) VALUES %s
            ON CONFLICT DO NOTHING
            """,
            rows,
            template=None,
            page_size=2000,
        )
        conn.commit()

    accepted = sum(1 for r in rows if r[6] == "ACCEPTED")
    print(f"   done — {accepted} ACCEPTED, "
          f"{len(rows) - accepted} non-ACCEPTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
