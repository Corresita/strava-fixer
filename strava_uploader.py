"""Strava OAuth token refresh + activity search.

Refresh is invoked transparently by `get_access_token()` when expired. Refreshed
values (and any other env updates passed through `_persist_env`) are written to:
  - `.env` (local dev), and
  - `$DATA_DIR/creds.env` on a persistent disk when `DATA_DIR` is set (e.g. a
    Fly.io volume mounted at /data), so the rotating Strava session cookie and
    refreshed OAuth tokens survive container restarts and redeploys.
`load_persisted_creds()` is called at import time to apply any values saved on
the volume on top of the deploy-time secrets.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# Persistent-disk location for rotating credentials. On Fly.io this is a mounted
# volume; locally it's unset and we fall back to .env only.
_DATA_DIR = os.environ.get("DATA_DIR", "")
# Keys we persist to the volume so they survive restarts.
_PERSISTED_KEYS = ("ACCESS_TOKEN", "REFRESH_TOKEN", "EXPIRES_AT", "STRAVA_SESSION_COOKIE")


def _creds_path() -> Path | None:
    return Path(_DATA_DIR) / "creds.env" if _DATA_DIR else None


def load_persisted_creds() -> None:
    """Apply credentials saved on the persistent volume over the deploy-time
    env vars. The volume copy is newer (it holds rotated values), so it wins."""
    p = _creds_path()
    if not p or not p.exists():
        return
    for line in p.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            if k in _PERSISTED_KEYS and v:
                os.environ[k] = v


load_persisted_creds()

# Spoofing a real Chrome UA avoids a 401 some Strava endpoints return for the
# default `python-requests/X.Y` UA on cloud IPs.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "User-Agent": _UA}


def _env(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        raise RuntimeError(f"{key} not set in .env")
    return val


def _write_volume_creds(updates: dict[str, str]) -> None:
    """Persist rotating credentials to the mounted volume so they survive
    restarts and redeploys. Noops when DATA_DIR isn't set (local dev).
    Unlike a platform-secrets API call, writing a file triggers no restart."""
    p = _creds_path()
    if not p:
        return
    persisted = {k: v for k, v in updates.items() if k in _PERSISTED_KEYS}
    if not persisted:
        return
    existing: dict[str, str] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k] = v
    existing.update(persisted)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    print(f"[volume] persisted {list(persisted.keys())} to {p}", flush=True)


def _persist_env(updates: dict[str, str]) -> None:
    """Patch os.environ, write to local .env if present, and persist rotating
    credentials to the mounted volume (Fly.io) so they survive restarts."""
    for k, v in updates.items():
        os.environ[k] = v

    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        seen = set()
        for i, line in enumerate(lines):
            for k, v in updates.items():
                if line.startswith(f"{k}="):
                    lines[i] = f"{k}={v}"
                    seen.add(k)
        for k, v in updates.items():
            if k not in seen:
                lines.append(f"{k}={v}")
        env_path.write_text("\n".join(lines) + "\n")

    _write_volume_creds(updates)


def get_access_token() -> str:
    expires_at = int(os.environ.get("EXPIRES_AT", "0") or 0)
    if time.time() < expires_at - 60:
        return _env("ACCESS_TOKEN")

    print("[strava] refreshing access token...")
    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": _env("CLIENT_ID"),
            "client_secret": _env("CLIENT_SECRET"),
            "refresh_token": _env("REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        },
        headers={"User-Agent": _UA},
        timeout=10,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data}")
    _persist_env({
        "ACCESS_TOKEN": data["access_token"],
        "REFRESH_TOKEN": data["refresh_token"],
        "EXPIRES_AT": str(data["expires_at"]),
    })
    print(f"[strava] new token expires at {data['expires_at']}")
    return data["access_token"]


def find_activity_near(
    start_iso: str,
    expected_distance_m: float,
    window_minutes: int = 15,
) -> dict | None:
    """Find a Strava activity within ±window_minutes of start_iso whose
    distance is within 10% of expected_distance_m. Used to locate the
    Garmin auto-synced copy of a just-completed run."""
    token = get_access_token()
    dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    after = int((dt - timedelta(minutes=window_minutes)).timestamp())
    before = int((dt + timedelta(minutes=window_minutes)).timestamp())

    print(f"[strava] searching activities: after={after}, before={before}, "
          f"expected={expected_distance_m:.0f}m")
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=_h(token),
        params={"after": after, "before": before, "per_page": 30},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[strava] activity search failed: {resp.status_code} {resp.text}")
        return None

    candidates = resp.json()
    print(f"[strava]   found {len(candidates)} activities in window")
    best, best_diff = None, None
    for a in candidates:
        d = a.get("distance", 0) or 0
        if expected_distance_m == 0:
            return a
        diff = abs(d - expected_distance_m) / expected_distance_m
        print(f"[strava]     candidate id={a.get('id')} dist={d:.0f}m diff={diff:.4f}")
        if diff <= 0.10 and (best_diff is None or diff < best_diff):
            best, best_diff = a, diff
    if best:
        print(f"[strava]   picked best match: id={best['id']}")
    else:
        print(f"[strava]   no match within 10% distance tolerance")
    return best
