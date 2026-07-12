# Strava Distance Fixer

A small tool that rewrites each new running activity's distance on Strava to a repeating-digit form `N.NN km` (e.g. `19.200 km → 19.19 km`, `12.4 km → 12.12 km`). All other data — pace, HR, route, elevation, kudos, comments — stays intact. Garmin Connect's copy stays untouched.

## Why this exists

Strava re-derives distance from the GPS stream for any GPS activity. The API's `UpdatableActivity` schema has no `distance` field. The web edit form removed its distance input around 2024. Deleting a Garmin-synced activity via API returns 401 on default-tier apps. Every "change distance after upload" path is closed — except one: Strava's web **Crop** feature trims the GPS stream's start/end and recomputes distance, and it's reachable from outside the browser with a session cookie.

[docs/journey.md](docs/journey.md) walks through all eight approaches we tried — six dead ends, the manual crop that worked (v2.2), and the Strava webhook that closes the loop back to the original v1 architecture (v2.4).

## How it works

```
Garmin watch ──▶ Garmin Connect ──▶ Strava (auto-sync, original distance)
                                            │
                                            │ activity-create event
                                            ▼
                                    POST /strava-webhook  ──▶ sync_server (on Fly.io)
                                                                    │
                                                                    │ POST /activities/{id}/truncate
                                                                    ▼   (start_index=0, end_index=N)
                                                            Strava activity now N.NN km
                                                            (same activity, all kudos retained)
```

Steps inside the webhook handler (`sync.crop_strava_activity`):

1. Strava notifies us with the new activity's ID (a few seconds after Garmin syncs).
2. `GET /api/v3/activities/{id}` → distance and sport type. Skip if not a run, under 1 km, or already at the `N.NN` target.
3. Compute the `N.NN` km target.
4. `GET /api/v3/activities/{id}/streams?keys=distance` → cumulative-meters array, one entry per GPS point.
5. Binary-search for the smallest index whose cumulative distance is **≥** the target (Strava displays distance as floor to 2 decimals, so we overshoot slightly to land on `N.NN`).
6. `GET /activities/{id}/truncate` (with session cookie) → scrape Rails CSRF token from the form.
7. `POST /activities/{id}/truncate` with `start_index=0, end_index=<computed>`. Strava re-derives distance from the trimmed range.

What changes:
- Activity's distance becomes the closest GPS-point distance ≥ target (typically within a few meters of target).
- A few GPS points at the end of the route are permanently dropped.

What stays untouched:
- Activity ID, kudos, comments, name, sport type.
- HR / cadence / power / altitude — Strava preserves these streams across crop.
- Garmin Connect — the script never writes to Garmin. The full original recording stays in Garmin Connect with all private analytics (Body Battery, Hill Score, Training Effect).

Strava's UI warns "This action cannot be undone." Cropped GPS points really are gone. For a 19.200 km run trimmed to 19.19 km, that's ~10 m off the end of the route — invisible. For an 8.32 km run trimmed to 8.08 km, that's ~240 m — small but visible if you look at the map closely.

## Triggers

There are three ways to invoke the same crop pipeline. The webhook is the default; the others are for fallback and ad-hoc use.

| Trigger | When it fires | Code path |
| --- | --- | --- |
| **Strava webhook** (default) | Automatic, seconds after Garmin → Strava sync completes | `POST /strava-webhook` → `sync.crop_strava_activity()` |
| iOS Shortcut (backup) | You tap a Home Screen icon | `POST /sync` with `X-Sync-Secret` → `sync.run()` (does the full Garmin lookup + wait + crop) |
| CLI (debug / backfill) | You run a command on your laptop | `python sync.py [activity_id] [--force]` → same `sync.run()` |

All three end up calling `strava_cropper.crop_to_distance()` with the same target and producing the same result.

## Requirements

- Garmin Connect → Strava direct sync **enabled** (Garmin Connect → Settings → Connected Apps → Strava). The auto-synced activity is what we crop.
- Strava API app credentials (https://www.strava.com/settings/api) with `activity:read_all` scope.
- A Strava web session cookie (`_strava4_session`) captured from a logged-in browser — see `.env.example`.
- Python 3.12+, dependencies in `requirements.txt`.
- A publicly reachable HTTPS host for the webhook. The repo is set up for Fly.io (`Dockerfile` + `fly.toml`, ~$2/mo for one always-on 256 MB machine).

## Setup

```bash
# Install
conda activate strava-fixer    # or your venv
pip install -r requirements.txt

# Credentials — fill in everything in .env.example
cp .env.example .env

# Authorize Strava OAuth (opens browser, captures the code on localhost:8765)
python reauth_strava.py
```

Strava app's "Authorization Callback Domain" in https://www.strava.com/settings/api must be `localhost` for `reauth_strava.py` to receive the OAuth callback.

First Garmin login is interactive (one-time): a 6-digit MFA code arrives by email, you paste it. After that the cached token in `garmin_tokens/` lives ~1 month.

Then deploy to Fly.io and register the webhook:

```bash
# 1. Create the app + a persistent volume (holds the rotating credentials,
#    Garmin token, and history so they survive restarts).
fly apps create strava-distance-fixer
fly volumes create strava_data --region <region> --size 1

# 2. Push every non-Railway secret from .env, plus the Garmin token blob.
grep -E '^[A-Z]' .env | fly secrets import --stage
python -c "import base64,pathlib; print('GARMIN_TOKEN_B64='+base64.b64encode(pathlib.Path('garmin_tokens/garmin_tokens.json').read_bytes()).decode())" | fly secrets import --stage

# 3. Deploy. fly.toml sets DATA_DIR=/data and mounts the volume there.
fly deploy --ha=false

# 4. Register the Strava webhook (points at strava-distance-fixer.fly.dev).
python subscribe_webhook.py
```

`subscribe_webhook.py` deletes any existing subscription (Strava allows only one per app), then registers a fresh one. Strava verifies by hitting the `/strava-webhook` GET handler with the verify token; sync_server echoes the challenge back if it matches.

Optional (phone-triggered backup): create an iOS Shortcut with three actions — `Get Contents of URL` (POST to `https://strava-distance-fixer.fly.dev/sync` with header `X-Sync-Secret: <SYNC_SECRET>`), `Get Dictionary Value` (key `strava_url`), `Show Notification` — and add it to your Home Screen.

## Failure modes

- **Webhook fires but crop fails** — `history.json` records `pipeline_path=failed` and the exception. Tap the iOS Shortcut or `python sync.py --force` to retry. The Strava activity is untouched on failure (we only ever POST `truncate` if everything before it succeeded).
- **Webhook doesn't fire** — usually a Strava subscription issue. Re-run `python subscribe_webhook.py`. Verify with `curl https://www.strava.com/api/v3/push_subscriptions?client_id=...&client_secret=...`.
- **`401 / login redirect` at the crop step** — `_strava4_session` cookie expired. Recapture from a logged-in browser, then `fly secrets set STRAVA_SESSION_COOKIE=...` (and update local `.env`).
- **Garmin token cache expired (~1 month)** — re-login locally with `python sync.py` (it'll prompt for MFA), then regenerate `GARMIN_TOKEN_B64` and `fly secrets set` it. This is the one recurring manual step.
- **Activity under 1 km / already at target** — skipped silently, recorded as `skipped_short` / `skipped_already_at_target` in history.

In every failure mode the Strava activity is left exactly as Garmin's auto-sync delivered it.

## Files

```
sync.py              entry point: run() for CLI/Shortcut path, crop_strava_activity() for webhook path
sync_server.py       Flask server: /sync (manual), /strava-webhook (auto), / (health)
strava_cropper.py    binary-search for end_index, POST truncate form via session cookie
strava_uploader.py   Strava OAuth refresh, activity search, credential persistence
garmin_client.py     Garmin Connect login + activity fetch (used by CLI/Shortcut path only)
reauth_strava.py     one-shot Strava OAuth re-authorization
subscribe_webhook.py one-shot Strava push-subscription registration
Dockerfile, fly.toml Fly.io deployment (volume-mounted persistent creds)
history.json         per-run record (auto-created)
sync.log             append-only log (auto-created)
garmin_tokens/       cached Garmin OAuth1 token (auto-created)
docs/                journey + v1 README archive
```

## `history.json`

Each crop appends one record:

| Field | Meaning |
| --- | --- |
| `pipeline_path` | `cropped` / `skipped_existing` / `skipped_short` / `skipped_not_run` / `skipped_already_at_target` / `no_activity` / `failed` |
| `garmin_activity_id` | The Garmin activity, or `null` if triggered via webhook (didn't go through Garmin) |
| `original_km` / `target_km` / `final_km` | Original, target, and final-on-Strava distance |
| `points_dropped` | GPS points trimmed off the end |
| `strava_activity_id` | The Strava activity we modified |
| `error` | Diagnostic string when something went wrong, else `null` |

## Security notes

`.env` and `garmin_tokens/` are gitignored. Treat the Garmin OAuth1 token like a credential — it grants read access to the account. The `_strava4_session` cookie grants whatever a logged-in browser can do on Strava; if it leaks, sign out everywhere in Strava settings and recapture.

Strava OAuth tokens refresh themselves when expired; the `_strava4_session` cookie rotates after each successful crop. Both are written back to `.env` (local) and to `$DATA_DIR/creds.env` on the Fly volume when deployed, so they survive restarts. As long as the pipeline runs at least once every few weeks, the cookie effectively never expires.
