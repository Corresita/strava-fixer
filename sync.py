"""Main entry point.

Usage:
    python sync.py                 # process latest Garmin running activity
    python sync.py <activity_id>   # process a specific Garmin activity
    python sync.py --force         # re-process even if already in history

Pipeline:
    1. Find the latest Garmin running activity (or one by ID).
    2. Compute the N.NN km target.
    3. Wait for Garmin Connect's auto-sync to push the activity to Strava.
    4. Crop the Strava activity (via web `truncate` form) down to N.NN km.

The Strava activity stays in place — same ID, same kudos, same comments.
We just trim a handful of GPS points off the end so its distance matches
the target. Nothing is deleted, no re-upload, no duplicates.

The crop requires `_strava4_session` cookie (Strava web session) since the
public OAuth API doesn't expose this operation. Capture the cookie from a
logged-in browser and put it in env var `STRAVA_SESSION_COOKIE`. Cookies
last weeks-to-months; refresh when the script reports a 401 / login redirect.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import garmin_client
import strava_cropper
import strava_uploader

load_dotenv()

ROOT = Path(__file__).parent
# Persist history + log on the mounted volume when DATA_DIR is set (Fly.io),
# so they survive restarts; else keep them next to the code (local dev).
_DATA_DIR = Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else ROOT
_DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY = _DATA_DIR / "history.json"
LOG_FILE = _DATA_DIR / "sync.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sync")


@dataclass
class HistoryEntry:
    ts: str
    garmin_activity_id: int | None  # None for webhook-triggered (Strava-only) runs
    garmin_name: str  # activity name from whichever source had it
    original_km: float
    target_km: float
    pipeline_path: str  # cropped | skipped_existing | skipped_short | skipped_not_run
                       # | skipped_already_at_target | no_activity | failed
    strava_activity_id: int | None
    final_km: float | None
    points_dropped: int | None
    error: str | None


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    try:
        return json.loads(HISTORY.read_text())
    except json.JSONDecodeError:
        return []


def append_history(entry: HistoryEntry) -> None:
    h = load_history()
    h.append(asdict(entry))
    HISTORY.write_text(json.dumps(h, indent=2))


def already_synced(garmin_id: int) -> bool:
    for e in load_history():
        if e.get("garmin_activity_id") == garmin_id and e.get("pipeline_path") == "cropped":
            return True
    return False


def target_km(distance_m: float) -> float | None:
    """Round to the repeating-digit N.NN km form. Return None if < 1 km."""
    km = distance_m / 1000.0
    n = int(km)
    if n == 0:
        return None
    return float(f"{n}.{n:02d}")


def wait_for_garmin_sync_to_strava(
    start_iso: str, expected_m: float, deadline_s: int = 180
) -> dict | None:
    """Garmin auto-sync to Strava is typically <2 min after activity completion.
    Poll until we see the activity show up on Strava."""
    log.info(f"  waiting up to {deadline_s}s for Garmin → Strava auto-sync...")
    end = time.time() + deadline_s
    while time.time() < end:
        found = strava_uploader.find_activity_near(start_iso, expected_m)
        if found:
            log.info(f"  found Strava activity {found['id']} ({found.get('distance', 0)/1000:.4f} km)")
            return found
        time.sleep(10)
    log.info(f"  no auto-synced Strava activity found within {deadline_s}s")
    return None


def run(activity_id: int | None, force: bool) -> dict:
    log.info("=" * 70)
    log.info(f"sync started  force={force}  activity_id={activity_id}")
    ts_now = datetime.now().isoformat(timespec="seconds")

    log.info("Logging into Garmin...")
    client = garmin_client.login()

    if activity_id is None:
        log.info("Looking up latest running activity...")
        activity = garmin_client.latest_running_activity(client)
        if activity is None:
            log.error("No recent GPS running activity found.")
            return {"ok": False, "pipeline_path": "no_activity",
                    "error": "no recent GPS running activity"}
        activity_id = activity["activityId"]
    else:
        activity = garmin_client.get_activity(client, activity_id)

    garmin_name = activity.get("activityName", "?")
    original_m = activity.get("distance") or 0
    original_km = original_m / 1000.0
    start_iso = activity.get("startTimeGMT") or activity.get("startTimeLocal")
    if start_iso and "T" not in start_iso:
        start_iso = start_iso.replace(" ", "T") + "Z"
    log.info(f"Activity {activity_id}: {garmin_name!r}  {original_km:.4f} km  start={start_iso}")

    if not force and already_synced(activity_id):
        log.info("Already synced (use --force to redo). Done.")
        return {"ok": True, "pipeline_path": "skipped_existing",
                "garmin_activity_id": activity_id, "garmin_name": garmin_name,
                "original_km": original_km, "error": None}

    tgt_km = target_km(original_m)
    if tgt_km is None:
        log.info("Under 1 km, skipping.")
        return {"ok": True, "pipeline_path": "skipped_short",
                "garmin_activity_id": activity_id, "garmin_name": garmin_name,
                "original_km": original_km, "error": None}
    target_m = tgt_km * 1000

    log.info(f"Target: {tgt_km} km ({target_m:.0f} m)")

    if not start_iso:
        return {"ok": False, "pipeline_path": "failed",
                "error": "no startTimeGMT/Local on Garmin activity"}

    strava = wait_for_garmin_sync_to_strava(start_iso, original_m)
    if strava is None:
        entry = HistoryEntry(
            ts=ts_now, garmin_activity_id=activity_id, garmin_name=garmin_name,
            original_km=original_km, target_km=tgt_km, pipeline_path="failed",
            strava_activity_id=None, final_km=None, points_dropped=None,
            error="Strava auto-sync did not arrive within 180s",
        )
        append_history(entry)
        log.error("✗ FAILED  no Strava activity to crop")
        return {"ok": False, **asdict(entry), "strava_url": None}

    strava_id = strava["id"]
    log.info(f"Cropping Strava activity {strava_id} to {tgt_km} km...")

    strava_url = f"https://www.strava.com/activities/{strava_id}"
    try:
        result = strava_cropper.crop_to_distance(strava_id, target_m)
    except Exception as e:
        entry = HistoryEntry(
            ts=ts_now, garmin_activity_id=activity_id, garmin_name=garmin_name,
            original_km=original_km, target_km=tgt_km, pipeline_path="failed",
            strava_activity_id=strava_id, final_km=None, points_dropped=None,
            error=f"{type(e).__name__}: {e}",
        )
        append_history(entry)
        log.error(f"✗ FAILED  crop raised: {e}")
        return {"ok": False, **asdict(entry), "strava_url": strava_url}

    entry = HistoryEntry(
        ts=ts_now, garmin_activity_id=activity_id, garmin_name=garmin_name,
        original_km=original_km, target_km=tgt_km, pipeline_path="cropped",
        strava_activity_id=strava_id,
        final_km=result["final_distance_m"] / 1000.0,
        points_dropped=result["points_dropped"],
        error=None,
    )
    append_history(entry)

    log.info(f"✓ DONE  cropped  {strava_url}  "
             f"({original_km:.4f} → {result['final_distance_m']/1000:.4f} km, "
             f"dropped {result['points_dropped']} points)")
    return {"ok": True, **asdict(entry), "strava_url": strava_url}


def crop_strava_activity(strava_id: int) -> dict:
    """Webhook entry point. Called when Strava notifies us that an activity
    was just created. Skips the Garmin lookup entirely — Strava already has
    the just-arrived auto-synced copy, we just need to crop it.

    Idempotent: if the activity is already at its N.NN target, no-op.
    """
    import requests
    log.info("=" * 70)
    log.info(f"webhook crop  strava_id={strava_id}")
    ts_now = datetime.now().isoformat(timespec="seconds")

    token = strava_uploader.get_access_token()
    r = requests.get(
        f"https://www.strava.com/api/v3/activities/{strava_id}",
        headers=strava_uploader._h(token),
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"GET activity {strava_id} failed: {r.status_code}")
    activity = r.json()

    name = activity.get("name", "?")
    sport = activity.get("sport_type") or activity.get("type", "")
    original_m = activity.get("distance", 0) or 0
    original_km = original_m / 1000.0

    if sport not in ("Run", "TrailRun", "VirtualRun"):
        log.info(f"  not a run ({sport}), skip")
        return {"ok": True, "pipeline_path": "skipped_not_run",
                "strava_activity_id": strava_id, "garmin_name": name}

    if original_m < 1000:
        log.info(f"  under 1 km, skip")
        return {"ok": True, "pipeline_path": "skipped_short",
                "strava_activity_id": strava_id, "garmin_name": name}

    tgt_km = target_km(original_m)
    target_m = tgt_km * 1000

    # Idempotent: if already at target, no-op. Strava floors distance to 2
    # decimals for display, so a value in [target_m, target_m+10) shows as
    # N.NN km. Skip if we're already in that window.
    if target_m <= original_m < target_m + 10:
        log.info(f"  already at target {tgt_km} km (raw {original_m:.1f}m), skip")
        return {"ok": True, "pipeline_path": "skipped_already_at_target",
                "strava_activity_id": strava_id, "garmin_name": name,
                "original_km": original_km, "target_km": tgt_km}

    log.info(f"  {name!r}: {original_km:.4f} km → target {tgt_km} km")

    try:
        result = strava_cropper.crop_to_distance(strava_id, target_m)
    except Exception as e:
        entry = HistoryEntry(
            ts=ts_now, garmin_activity_id=None, garmin_name=name,
            original_km=original_km, target_km=tgt_km, pipeline_path="failed",
            strava_activity_id=strava_id, final_km=None, points_dropped=None,
            error=f"{type(e).__name__}: {e}",
        )
        append_history(entry)
        log.error(f"✗ FAILED  crop raised: {e}")
        return {"ok": False, **asdict(entry)}

    entry = HistoryEntry(
        ts=ts_now, garmin_activity_id=None, garmin_name=name,
        original_km=original_km, target_km=tgt_km, pipeline_path="cropped",
        strava_activity_id=strava_id,
        final_km=result["final_distance_m"] / 1000.0,
        points_dropped=result["points_dropped"],
        error=None,
    )
    append_history(entry)
    log.info(f"✓ DONE  webhook-cropped  https://www.strava.com/activities/{strava_id}  "
             f"({original_km:.4f} → {result['final_distance_m']/1000:.4f} km, "
             f"dropped {result['points_dropped']} points)")
    return {"ok": True, **asdict(entry),
            "strava_url": f"https://www.strava.com/activities/{strava_id}"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("activity_id", nargs="?", type=int, default=None,
                   help="Garmin activity ID (default: latest running)")
    p.add_argument("--force", action="store_true",
                   help="re-process even if already in history.json")
    args = p.parse_args()
    result = run(args.activity_id, args.force)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
