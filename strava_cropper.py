"""Crop a Strava activity to a target distance by replaying its web 'truncate'
(crop) form. The activity stays in place — same ID, all kudos and comments
preserved. See docs/journey.md Approach 7 for context.

Needs `STRAVA_SESSION_COOKIE` (captured from a logged-in browser) because the
OAuth API doesn't expose /truncate. The cookie auto-rotates back to .env (and
the persistent volume when deployed) after each successful POST.
"""
from __future__ import annotations

import os
import re

import requests
from dotenv import load_dotenv

import strava_uploader

load_dotenv()

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")


def _session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.cookies.set("_strava4_session", cookie, domain=".strava.com")
    s.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _csrf_from_truncate_page(html: str) -> str | None:
    m = re.search(
        r"name=['\"]authenticity_token['\"]\s+type=['\"]hidden['\"]\s+value=['\"]([^'\"]+)['\"]",
        html,
    )
    if m:
        return m.group(1)
    m = re.search(r"authenticity_token[^>]*value=['\"]([^'\"]+)['\"]", html)
    return m.group(1) if m else None


def _distance_stream(activity_id: int, oauth_token: str) -> list[float]:
    r = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}/streams",
        params={"keys": "distance", "key_by_type": "true"},
        headers={"Authorization": f"Bearer {oauth_token}", "User-Agent": _UA},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"streams GET failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    if isinstance(body, dict) and "distance" in body:
        return body["distance"]["data"]
    if isinstance(body, list):
        for stream in body:
            if stream.get("type") == "distance":
                return stream["data"]
    raise RuntimeError(f"no distance stream in response: {body!r}")


def _pick_end_index(distances: list[float], target_m: float) -> int:
    """Smallest index i such that distances[i] >= target_m.

    Strava displays distance as floor to 2 decimals, not rounded — a target
    of 27.27 km (=27270 m) needs the stored value to land in [27270, 27280)
    for the UI to show '27.27 km'. So we overshoot slightly (picking >=)
    rather than under (<=). GPS points are typically ~2 m apart so the
    overshoot stays well inside the 10 m display window.
    """
    lo, hi = 0, len(distances) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if distances[mid] >= target_m:
            hi = mid
        else:
            lo = mid + 1
    return lo


def crop_to_distance(activity_id: int, target_m: float) -> dict:
    """Crop the Strava activity so its total distance lands at the target.
    Returns a result dict on success; raises RuntimeError on any failure."""
    cookie = os.environ.get("STRAVA_SESSION_COOKIE", "")
    if not cookie:
        raise RuntimeError("STRAVA_SESSION_COOKIE not set")
    oauth_token = strava_uploader.get_access_token()

    print(f"[crop] activity {activity_id}, target {target_m:.2f}m")

    distances = _distance_stream(activity_id, oauth_token)
    original_m = distances[-1]
    print(f"[crop]   stream has {len(distances)} points, original distance {original_m:.2f}m")

    if target_m >= original_m:
        raise RuntimeError(f"target {target_m} >= original {original_m}, nothing to crop")

    end_idx = _pick_end_index(distances, target_m)
    chosen_m = distances[end_idx]
    print(f"[crop]   chosen end_index={end_idx} -> distance {chosen_m:.2f}m "
          f"(dropping {len(distances) - 1 - end_idx} points, "
          f"{original_m - chosen_m:.1f}m off the end)")

    s = _session(cookie)
    truncate_url = f"https://www.strava.com/activities/{activity_id}/truncate"

    page = s.get(truncate_url, timeout=15)
    if page.status_code != 200:
        raise RuntimeError(f"GET truncate page: {page.status_code} (cookie expired?)")

    csrf = _csrf_from_truncate_page(page.text)
    if not csrf:
        raise RuntimeError("couldn't parse authenticity_token from truncate page")

    resp = s.post(
        truncate_url,
        data={"authenticity_token": csrf, "start_index": "0", "end_index": str(end_idx)},
        headers={"X-CSRF-Token": csrf, "Referer": truncate_url},
        timeout=20,
        allow_redirects=False,
    )
    if resp.status_code not in (200, 302):
        raise RuntimeError(f"POST truncate failed: {resp.status_code} {resp.text[:200]}")
    print(f"[crop]   POST truncate -> {resp.status_code}")

    # Strava issues a fresh _strava4_session on most responses. Persist the new
    # value so the cookie effectively never expires as long as sync runs at
    # least every few weeks.
    rotated = s.cookies.get("_strava4_session", domain=".strava.com") \
        or s.cookies.get("_strava4_session")
    if rotated and rotated != cookie:
        print(f"[crop]   rotating _strava4_session ({cookie[:6]}... -> {rotated[:6]}...)", flush=True)
        strava_uploader._persist_env({"STRAVA_SESSION_COOKIE": rotated})

    return {
        "activity_id": activity_id,
        "original_distance_m": original_m,
        "final_distance_m": chosen_m,
        "points_dropped": len(distances) - 1 - end_idx,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python strava_cropper.py <activity_id> <target_km>", file=sys.stderr)
        sys.exit(1)
    print(crop_to_distance(int(sys.argv[1]), float(sys.argv[2]) * 1000))
