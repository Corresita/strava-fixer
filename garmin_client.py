"""Garmin Connect login with cached OAuth1 token.

First login is interactive (MFA via prompt_mfa). After that, the token cache
lets subsequent runs skip the SSO entirely. The cache lives on the persistent
volume ($DATA_DIR/garmin_tokens) when DATA_DIR is set, else in ./garmin_tokens.
"""
from __future__ import annotations

import os
from pathlib import Path

from garminconnect import Garmin

_DATA_DIR = os.environ.get("DATA_DIR", "")
TOKENSTORE = (Path(_DATA_DIR) if _DATA_DIR else Path(__file__).parent) / "garmin_tokens"


def _prompt_mfa() -> str:
    code = os.environ.get("GARMIN_MFA_CODE")
    if code:
        return code.strip()
    return input("Garmin MFA code: ").strip()


def login() -> Garmin:
    email = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    client = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    if TOKENSTORE.exists():
        try:
            client.login(str(TOKENSTORE))
            return client
        except Exception:
            pass  # fall through to fresh login
    if not email or not password:
        raise RuntimeError("GARMIN_EMAIL / GARMIN_PASSWORD required for first-time login")
    client = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    client.login()
    client.client.dump(str(TOKENSTORE))
    return client


def latest_running_activity(client: Garmin) -> dict | None:
    """Return the most recent GPS-running activity, or None."""
    for a in client.get_activities(0, 10):
        sport = (a.get("activityType") or {}).get("typeKey", "")
        if sport in ("running", "trail_running", "treadmill_running", "track_running"):
            if (a.get("distance") or 0) > 0 and a.get("startLatitude") is not None:
                return a
    return None


def get_activity(client: Garmin, activity_id: int) -> dict:
    return client.get_activity(activity_id)
