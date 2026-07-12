"""One-shot: register (or re-register) the Strava push subscription that
delivers activity events to our /strava-webhook endpoint.

Strava allows only one subscription per OAuth app, so we delete any existing
one first. Run once after deploying sync_server.py with the verify token set.
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
CALLBACK_URL = os.environ.get(
    "STRAVA_WEBHOOK_CALLBACK_URL",
    "https://strava-distance-fixer.fly.dev/strava-webhook",
)
VERIFY_TOKEN = os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN")


def main() -> None:
    if not (CLIENT_ID and CLIENT_SECRET and VERIFY_TOKEN):
        sys.exit("CLIENT_ID, CLIENT_SECRET, and STRAVA_WEBHOOK_VERIFY_TOKEN must be set in .env.")

    params = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    print("Checking for existing subscriptions...")
    r = requests.get("https://www.strava.com/api/v3/push_subscriptions", params=params, timeout=10)
    if r.status_code != 200:
        sys.exit(f"List failed: {r.status_code} {r.text}")
    for sub in r.json():
        print(f"Deleting subscription {sub['id']} (callback={sub.get('callback_url')})...")
        d = requests.delete(
            f"https://www.strava.com/api/v3/push_subscriptions/{sub['id']}",
            params=params, timeout=10,
        )
        print(f"  -> {d.status_code} {d.text}")

    # Strava GETs our callback to verify — sync_server's /strava-webhook GET
    # handler echoes the challenge back if the verify token matches.
    print(f"Subscribing {CALLBACK_URL}...")
    r = requests.post(
        "https://www.strava.com/api/v3/push_subscriptions",
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "callback_url": CALLBACK_URL,
            "verify_token": VERIFY_TOKEN,
        },
        timeout=15,
    )
    print(f"  -> {r.status_code} {r.text}")
    if r.status_code not in (200, 201):
        sys.exit("Subscription failed.")
    print(f"\n✓ Subscribed. New activity events will hit {CALLBACK_URL}")


if __name__ == "__main__":
    main()
