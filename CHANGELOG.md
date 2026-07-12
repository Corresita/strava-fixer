# Changelog

## [2.5.0] - 2026-07-11

Migrate off Railway (its free trial credit ran out) to Fly.io. One always-on
256 MB machine, ~$2/mo, no cold starts. Credential persistence moves from
Railway's env-var API to a mounted volume — simpler, and it doesn't trigger a
restart on every token rotation.

### Added
- `Dockerfile`, `fly.toml` — Fly.io deployment. A 1 GB volume mounts at
  `/data`; `DATA_DIR=/data` routes the credential store, Garmin token cache,
  `history.json`, and `sync.log` onto it so they survive restarts and redeploys.
- `strava_uploader.load_persisted_creds()` / `_write_volume_creds()` — rotating
  Strava tokens + `_strava4_session` cookie are persisted to `$DATA_DIR/creds.env`
  and re-applied over the deploy-time secrets on startup.

### Changed
- `garmin_client` and `sync_server` bootstrap the Garmin token cache under
  `$DATA_DIR` when set, else the repo dir (local dev unchanged).
- `subscribe_webhook.py` default callback URL → `strava-distance-fixer.fly.dev`;
  wrapped its body in `main()` so importing it no longer fires a live API call.

### Removed
- `railway.toml`, `Procfile`, and `_railway_upsert_vars()` — Railway is gone.
  The `RAILWAY_*` env vars are dropped from `.env.example`.
- The iOS Shortcut path: `POST /sync` endpoint, `_authorized()`, and the
  `SYNC_SECRET` env var. The webhook fully automates the common case; manual
  runs / backfill use the local CLI (`python sync.py`). Triggers are now
  webhook + CLI.

### Verified
- 2026-07-11: full pipeline runs on Fly — Garmin token loads from the volume,
  Strava OAuth refreshes, and the `_strava4_session` cookie crops successfully
  from Fly's datacenter IP (23561601674 → 11.11 km). Volume holds `creds.env`,
  `garmin_tokens/`, `history.json`, `sync.log` across restarts.

---

## [2.4.0] - 2026-05-18

Full automation. Strava pushes activity-create events to `/strava-webhook` on Railway; sync_server spawns a background thread that crops the activity in ~5 seconds. No phone tap, no laptop. iOS Shortcut and CLI are kept as backup paths against the day the webhook or the crop form changes.

### Added
- `sync.crop_strava_activity(strava_id)` — webhook entry point. Skips the Garmin lookup since Strava just told us the activity ID. Idempotent via `[target_m, target_m+10)` window check.
- `sync_server.py`: `GET /strava-webhook` (hub.challenge handshake), `POST /strava-webhook` (event receiver, threads off to keep response under Strava's timeout).
- `subscribe_webhook.py` — one-shot CLI that deletes any existing subscription (Strava allows one per app) and registers a fresh one pointing at the Railway domain.
- `STRAVA_WEBHOOK_VERIFY_TOKEN` env var (shared between subscribe_webhook and the GET verify handler).
- `pipeline_path` values: `skipped_not_run`, `skipped_already_at_target`.
- `HistoryEntry.garmin_activity_id` is now `int | None` (`None` for webhook-triggered runs).

### Verified
- 2026-05-20: runs after a Garmin auto-sync, Strava activity becomes `N.NN km` within seconds of the webhook firing. Logs in Railway show the full pipeline. iOS Shortcut and CLI continue to work as backup.

---

## [2.3.0] - 2026-05-17

Credential auto-rotation. Refreshed Strava OAuth tokens and rotated `_strava4_session` cookies are pushed back to Railway's project variables via the GraphQL `variableCollectionUpsert` mutation. As long as sync runs at least once every few weeks, the cookie effectively never expires.

### Added
- `_railway_upsert_vars()` in `strava_uploader.py`. Calls Railway's GraphQL API to upsert env vars; silently noops without the four `RAILWAY_*` env vars.
- `_persist_env()` updates os.environ even when `.env` is absent (Railway containers don't have a `.env`).
- Cookie capture from `requests.Session.cookies` after the `truncate` POST, with auto-persist.
- `RAILWAY_API_TOKEN`, `RAILWAY_PROJECT_ID`, `RAILWAY_SERVICE_ID`, `RAILWAY_ENVIRONMENT_ID` env vars.

### Fixed
- `_pick_end_index` now picks the smallest index whose cumulative distance is `≥` target, not the largest `≤`. Strava displays distance with floor truncation to 2 decimals, so the old algorithm landed at `N.NN - 0.01`. New algorithm overshoots by ~2 m (GPS spacing) into the `[target, target+10)` floor-display window, landing at `N.NN`.

### Removed
- `tcx_scaler.py`, `strava_uploader.upload_tcx`, `strava_uploader.delete_activity`, `garmin_client.download_tcx` — dead code from the v2.0–v2.1 TCX-scaling and delete+reupload pipelines. Total: 1200 → 824 LOC (-31%).

---

## [2.2.0] - 2026-05-16

Pipeline pivot: replace the "delete the auto-synced Strava copy, upload a scaled version" approach (blocked by Strava's `DELETE` 401 — see 2.1.0) with **modify the existing Strava activity in place** by replaying its web "Crop / truncate" form. Same activity ID, same kudos, same comments — just trim a few GPS points off the end to land on N.NN km.

### Added
- `strava_cropper.py` — fetches the activity's distance stream via OAuth, binary-searches for the `end_index` whose cumulative distance is closest to (but ≤) the target, scrapes the CSRF token from `/activities/{id}/truncate`, and POSTs the same form a logged-in browser submits to crop. Result: distance lands within a few meters of target (~6m for an 8 km run with ~3m-spaced GPS points).
- `STRAVA_SESSION_COOKIE` env var required for the crop step (OAuth API doesn't expose `/truncate`).

### Changed
- `sync.py` is now ~30% shorter and much simpler. New flow: find latest Garmin running activity → wait for it to auto-sync to Strava → crop that Strava activity. No TCX download, no GPS scaling, no upload, no delete.
- `HistoryEntry` schema replaced: `pipeline_path` is now `cropped` / `skipped_existing` / `skipped_short` / `no_activity` / `failed`; new fields `final_km` and `points_dropped` replace `scale_factor` / `strava_deleted_id` / `strava_new_id` / `fallback_reason`.
- `sync_server.py` drops the `?no_delete=` URL parameter (the underlying pipeline no longer has a delete step to skip).

### Removed (still in repo, no longer wired up)
- `tcx_scaler.py` — GPS-path scaling. Kept in the tree as a reference / fallback option but no longer imported by sync. The crop approach made it obsolete.

### Verified
- 2026-05-16 17:50 local: cropped activity 18532273416 (`Morning Run`) from 8086.30 m → 8079.80 m (= 8.08 km). All 18 kudos preserved, HR / cadence / pace unchanged, activity ID unchanged. The whole pipeline ran in under 5 seconds.

### Operational note
The cookie expires periodically. When sync stops working with HTTP 401s at the crop step, capture a fresh `_strava4_session` from a logged-in browser and update the env var (locally and on Railway). Sign-out-everywhere in Strava settings invalidates all existing cookies — useful to rotate after any credential exposure.

---

## [2.1.0] - 2026-05-16

Add HTTP wrapper so the sync can be triggered remotely from an iOS Shortcut — the original `python sync.py` CLI still works locally, but a Flask server (`sync_server.py`) exposes the same pipeline at `POST /sync` for phone-based invocation. Designed for Railway deployment.

### Added
- `sync_server.py` — Flask wrapper around `sync.run()`, gated by `X-Sync-Secret` header, bootstraps Garmin OAuth token from `GARMIN_TOKEN_B64` env var on startup so fresh containers don't trigger rate-limited SSO logins
- `Procfile` + `railway.toml` — gunicorn entrypoint for Railway (Railpack builder)
- `flask`, `gunicorn` back in `requirements.txt`
- `?no_delete=1` URL parameter on `/sync` to skip the Strava find-and-delete step
- Debug logging in `find_activity_near`: search window, candidate count, per-activity distance diff, final match decision
- Debug logging in `delete_activity`: token prefix, explicit `flush=True` to defeat gunicorn stdout buffering
- Browser-style `User-Agent` header on all Strava requests (attempt to work around suspected cloud-IP throttling)
- `_persist_env` now patches `os.environ` even when `.env` is absent (Railway has no `.env` file, only platform env vars) — avoids re-refreshing the Strava access token on every single API call

### Discovered (the wall this version hit)
End-to-end pipeline runs correctly on Railway, but Strava's `DELETE /activities/{id}` returns **`401 "Application internal invalid"`** for this OAuth app — for **any** real activity, from **any** origin (local laptop or Railway), regardless of User-Agent. Same token at the same instant can `PUT` the activity (200) and `DELETE` a nonexistent ID (404 — proving generic delete permission exists), but cannot `DELETE` a real activity. Strava appears to have restricted DELETE on apps in the default "Limited Access" tier.

Net effect for v2.1: the delete-and-reupload pipeline cannot complete. The script logs the failure, falls back to uploading the unmodified TCX, which then also dedup-fails against the original auto-synced copy. See `docs/journey.md` Approach 6 for the full diagnostic trail.

### Operational note
Until the DELETE block is worked around (Strava app tier upgrade, or a different attack vector — see journey.md), the current state on Railway is **non-functional for GPS runs**. Running locally with `python sync.py` has the same DELETE failure. The pipeline only fully works in test scenarios where no Strava auto-synced copy exists (e.g., the time-shifted upload test from 2.0.0 validation).

---

## [2.0.0] - 2026-05-15

Rewrite. The "modify Strava activity after upload" approach proved fundamentally unworkable — Strava's `UpdatableActivity` schema has no `distance` field, and the web edit form for GPS activities lost its distance input around 2024. The webhook-and-fallback architecture was abandoned.

The replacement is a local command-line tool that intercepts the data path **before** Strava sees it: pull the activity from Garmin Connect, scale the GPS track itself, upload the modified TCX to Strava as a fresh activity.

### Added
- `sync.py` — single entry point, processes the latest Garmin running activity (or one by ID) in ~30 seconds
- `garmin_client.py` — Garmin Connect login with cached OAuth1 token (good for ~1 year, MFA only on first run)
- `tcx_scaler.py` — uniform GPS-path scaling anchored at the first trackpoint; scales lat/lng, per-trackpoint distance and speed, lap distances. Times / HR / cadence / power / altitude untouched.
- `strava_uploader.py` — Strava OAuth refresh, multipart upload, processing-status poll
- `reauth_strava.py` — local-callback OAuth helper for one-shot token rotation
- `history.json` — per-run audit trail; `sync.log` — append-only log
- TCX scaling falls back to uploading the unmodified file on failure, so the activity always lands on Strava

### Removed
- Flask webhook server (`app.py`, `test_app.py`)
- Railway deployment config (`Procfile`)
- Web form simulation, session cookie management, CSRF handling — all dead ends with Strava's current GPS-activity policy
- `STRAVA_SESSION_COOKIE`, `RAILWAY_*` env vars

### Operational note
Keep Garmin Connect → Strava direct sync **enabled**. The script waits for that auto-synced copy to appear on Strava, deletes it, then uploads the scaled version. This keeps non-running activities (cycling, strength, swims) flowing untouched through the original auto-sync, while runs get the `N.NN` treatment.

If the scaled upload fails the script falls back to uploading the original (un-scaled) TCX, so a run never disappears from Strava — at worst its distance is unmodified.

---

## [1.x] (deprecated, see git history)

The 1.x series was a Flask webhook server that tried to rewrite Strava distances after upload. It worked only for manually-entered activities; GPS activities were silently reverted by Strava's stream-recomputation. Replaced wholesale in 2.0.

---

## [1.4.0] - 2026-05-10

### Added
- Verify distance after PUT: GET activity again after 5s, if Strava reverted, wait 60s and retry (up to 5 times)

---

## [1.3.0] - 2026-05-09

### Added
- Auto-update Railway env vars (ACCESS_TOKEN, REFRESH_TOKEN, EXPIRES_AT) via Railway GraphQL API after every token refresh, so tokens survive container restarts without needing a Volume

---

## [1.2.0] - 2026-05-08

### Added
- Persist tokens to /tmp/tokens.json after refresh; load on startup to avoid unnecessary re-auth
- `threading.Lock` for thread-safe token access

### Fixed
- Read EXPIRES_AT from env var on startup instead of hardcoding 0, preventing unnecessary token rotation on every container restart

---

## [1.1.0] - 2026-05-07

### Fixed
- Activities under 1 km no longer get zeroed out (n=0 guard)
- Check HTTP status on GET before processing activity data
- Add retry loop (5 attempts, 30s interval) for activities that haven't loaded distance yet
- Add timeout=10 to all requests to prevent hanging threads

---

## [1.0.0] - 2026-05-06

### Added
- Initial Flask webhook server
- Strava OAuth2 token refresh
- Distance formula: n = int(km), new = n.nn (e.g. 13.50 → 13.13)
- Background thread per activity to avoid blocking webhook response
