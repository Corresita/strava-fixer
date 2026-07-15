# Project journey

A record of every approach we tried — eight in total. Six were dead ends; two work and run in production today (manual crop in v2.2, webhook auto-trigger in v2.4). Kept so the next person (or future-me) doesn't have to repeat the dead ends.

## The goal

Rewrite each new running activity's distance on Strava to a repeating-digit form `N.NN km` (e.g. 19.200 km → 19.19 km, 12.4 km → 12.12 km). All other data — pace, HR, route, elevation — should stay intact.

## Approach 1: Strava API webhook + `PUT /activities` (v1.0 – v1.4)

**Idea.** Subscribe to Strava activity-create webhooks. When a new activity arrives, call `PUT /api/v3/activities/{id}` with the rewritten `distance`.

**What we built.** A Flask service on Railway, OAuth token refresh, retry loop, persistent token cache.

**Why it failed.** Strava's public `UpdatableActivity` model has no `distance` field. The PUT request returns 200 OK and the response body shows the new value, but a few seconds later Strava re-runs distance computation from the GPS stream and reverts. The behavior is **silent** — no error, no warning, the field just resets.

For manually entered activities (no GPS stream) the PUT *did* persist, because there's no stream to recompute from. So this approach is only useful for manual activities, not real runs.

## Approach 2: Web form simulation via session cookie (v1.5 attempt)

**Idea.** The Strava website's edit page used to have a "Distance" input. Logging in with a session cookie, scraping the edit form, posting modified values would bypass the API restriction.

**What we built.** `requests` + BeautifulSoup form scraper, CSRF token capture, unit-aware (km vs miles) submission, post-submit verification via API.

**Why it failed.** Sometime around 2024 Strava removed the distance input from the GPS-activity edit form entirely. The form posts we sent never contained a distance field, so the edit had no effect. Confirmed empirically (Railway log shows `Fields: ['utf8', '_method', 'authenticity_token', 'activity[name]', ...]` — no `activity[distance]`) and by visiting the edit page in a browser. Strava community forum acknowledges this as a "known issue" with no ETA on a fix.

This killed every "modify after upload to Strava" approach. The only path left was to modify data *before* it reaches Strava.

## Approach 3: Garmin Connect Developer API (research, never built)

**Idea.** Subscribe to Garmin Connect webhooks. When a new Garmin activity is detected, download the FIT/TCX, scale the GPS path, upload to Strava as a fresh activity.

**Why it didn't work out.** Two blockers:

- The Garmin Connect Developer Program is described as "for enterprise use only" — the developer forum confirms only large partners (companies, fitness platforms) get approved. Individual / hobby use cases get denied.
- The Access Request Form at `garmin.com/en-US/forms/GarminConnectDeveloperAccess/` has been broken for at least a month (developers in the Garmin forum reporting "under construction" status). Emails to `connectdevservices@garmin.com` go unanswered.

So even if it would have been the cleanest path, the door is closed for our use case.

## Approach 4: Unofficial Garmin client (`garth`) (rejected)

**Idea.** Use the [garth](https://github.com/matin/garth) library, which logs into Garmin Connect using internal SSO endpoints (the same ones the mobile app uses), and downloads activities without going through the official developer program.

**Why we rejected it.**

- garth's README literally says `[DEPRECATED]` — Garmin changed their auth flow and broke garth's mobile login. Existing sessions still work but new logins do not.
- Sibling library `python-garminconnect` is still active, but issues like [#213](https://github.com/cyberjunky/python-garminconnect/issues/213) report 48+ hour account-level rate-limiting after a small number of failed login attempts. We hit the same 429s on our first three login attempts during this session — a sign Garmin is actively tightening this surface.

We almost went with python-garminconnect anyway, but the rate-limit risk for a polling architecture was too high.

## Approach 5: python-garminconnect + cached token + on-demand sync (v2.0, current)

**Idea.** Same as approach 4, but solve the rate-limit problem two ways: (a) cache the OAuth1 token after first login so subsequent runs skip the SSO entirely, and (b) trigger the sync **on demand** (you run a command after each run) rather than polling, so the login rate is effectively once per year.

**Why it works.**

- A single interactive login (with MFA) populates `garmin_tokens/`. The OAuth1 token there is good for ~1 year per garth/garminconnect docs.
- Every subsequent `python sync.py` reuses that token. Zero login traffic. Zero rate-limit risk.
- Once we have the TCX in memory, the modification is a straight XML transform: scale every trackpoint's lat/lng toward the start position by the same factor, scale per-trackpoint distance and speed by the same factor, scale per-lap distance by the same factor. HR / cadence / power / altitude / time stay untouched.
- Upload to Strava as a new activity. Because the GPS stream itself is now consistent with the target distance, Strava's own distance recomputation produces the target value. No revert.

End-to-end takes ~30 seconds per run. The entire 19.200 km → 19.19 km change is a 0.05 % geometric shrink — invisible on the map, exact on the dashboard.

**Tested end-to-end on 2026-05-14**: 19.200 km Garmin activity → scaled TCX uploaded with shifted timestamps (to bypass Strava's dedup against the auto-synced original) → resulting Strava activity 18512945963 reported `distance: 19190.00 m` and preserved all 14,522 HR samples, cadence, power, altitude. The scaling math works perfectly. The pipeline works perfectly on a "clean" upload (no existing Strava copy).

## Approach 6: Remote trigger via iOS Shortcut → Railway → sync.py (v2.1, attempted 2026-05-16)

**Goal.** Make Approach 5 phone-triggered instead of laptop-required. Wrap `sync.run()` in a Flask endpoint, deploy on Railway, call it from an iOS Shortcut. Same pipeline, but reachable from anywhere.

**What we built.** `sync_server.py` with `POST /sync` gated by an `X-Sync-Secret` header. Garmin OAuth1 token loaded from a `GARMIN_TOKEN_B64` env var on startup so containers never SSO. iOS Shortcut sends `POST` with the secret, reads `strava_url` from the JSON response, shows it in a notification.

**Where it broke.** The pipeline depends on a "find Strava's auto-synced copy of the Garmin activity, DELETE it, upload our scaled version" step. The DELETE call returns:

```
401 {"message":"Authorization Error",
     "errors":[{"resource":"Application","field":"internal","code":"invalid"}]}
```

**Diagnostic trail (all with the same token, same scopes — `read activity:read_all activity:write`):**

| Test                                                | Result                |
| --------------------------------------------------- | --------------------- |
| `GET /api/v3/athlete`                               | 200 ✓                 |
| `GET /api/v3/activities/{id}`                       | 200 ✓                 |
| `PUT /api/v3/activities/{id}` (changing description)| 200 ✓                 |
| `POST /api/v3/uploads`                              | 200, then dedup error |
| `DELETE /api/v3/activities/99999999999` (fake id)   | **404 "Record Not Found"** — proves DELETE permission exists at app level |
| `DELETE /api/v3/activities/{real id}`               | **401 "Application internal invalid"** |

The 401 is **not** about IP origin (same response from a laptop in Vancouver as from a Railway container), **not** about User-Agent (browser-spoofed UA returns the same 401), **not** about token scope (PUT with the same token works fine), and **not** about credential rotation (token was just refreshed; reauth was clean).

The likely root cause: Strava's anti-abuse / app-tiering system. Apps in the default "Limited Access" tier appear to be allowed full READ + write (`POST /uploads`, `PUT /activities`) but blocked from `DELETE /activities/{id}` on real activities. The "Application internal invalid" error is Strava's way of saying "your app isn't in a tier that's permitted to do this." This is undocumented and only visible by hitting the endpoint.

**Side improvements that did land in 2.1:**

- `_persist_env` bug: when `.env` doesn't exist (Railway), the function early-returned and `os.environ` never got the refreshed token, so every API call triggered a redundant token refresh. Now it always patches `os.environ`.
- `find_activity_near` got verbose logging (search window, candidates, distance diffs) — invaluable for diagnosing this.
- `delete_activity` now logs token prefix + uses `flush=True` so the failure line isn't lost to gunicorn stdout buffering (which is what happened in early diagnostic runs).

**Net state.** v2.1 deploys cleanly and survives the iOS Shortcut roundtrip, but the underlying delete-and-reupload pipeline cannot complete its goal on a real run. The pipeline only fully works in artificial tests where there's no Garmin auto-synced Strava copy to fight against.

## Full attack-surface map

After Approach 6 hit the DELETE wall, we mapped out every conceivable place in the chain where distance could be modified. Most of these were already covered, ruled out, or are open questions. Keeping the map here so future-us doesn't have to redo it.

```
┌────────────────────────────────────────────────────────────────────────┐
│ Step 1. Garmin watch — records run, computes distance                  │
├────────────────────────────────────────────────────────────────────────┤
│ A1. Connect IQ data field — can read sensors, cannot overwrite GPS-    │
│     derived distance                                                    │
│ A2. Footpod stride / wheel calibration — indoor only, not useful for   │
│     GPS runs                                                            │
│ A3. Manual pause/resume — requires user action every run, not auto     │
│ A4. Third-party recording app, watch as sensor only — complex, bad UX  │
└────────────────────────────────────────────────────────────────────────┘
                          ↓ BLE
┌────────────────────────────────────────────────────────────────────────┐
│ Step 2. Phone Garmin Connect app — receives BLE stream                 │
├────────────────────────────────────────────────────────────────────────┤
│ B1. App edit screen — GPS activity distance not editable               │
│ B2. Intercept BLE — needs root + offline-protocol RE                   │
└────────────────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────────────────┐
│ Step 3. Garmin Connect cloud — stores activity                         │
├────────────────────────────────────────────────────────────────────────┤
│ C1. Web UI distance edit — locked for GPS activities                   │
│ C2. python-garminconnect API — no set_distance endpoint                │
│ C3. Delete + re-upload TCX — loses Garmin's private analytics fields   │
│     (Body Battery, Hill Score, Training Effect, Running Dynamics)      │
│ C4. Delete + re-upload FIT (preserving unknown_X messages byte-for-    │
│     byte) — preserves analytics if a Python FIT writer can round-trip  │
│     unknown messages losslessly. 3-5 days work, unverified.            │
│ C5. Modify only record.distance and session.total_distance in FIT,     │
│     leaving position_lat/long untouched — preserves the map but        │
│     creates internal inconsistency, unclear if Strava/Garmin use the   │
│     declared distance vs re-derive from positions                      │
└────────────────────────────────────────────────────────────────────────┘
                          ↓ Garmin → Strava auto-sync push
┌────────────────────────────────────────────────────────────────────────┐
│ Step 4. Strava receives the push                                       │
├────────────────────────────────────────────────────────────────────────┤
│ D1. Intercept the Garmin → Strava push — impossible, server-to-server  │
│ D2. Beat Garmin to it (upload modified copy first, let Garmin's copy   │
│     get dedup-rejected) — timing window is 1-5 min, unreliable         │
└────────────────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────────────────┐
│ Step 5. Activity now lives on Strava                                   │
├────────────────────────────────────────────────────────────────────────┤
│ E1. PUT /activities/{id} with distance field — field not in            │
│     UpdatableActivity schema, silently ignored                         │
│ E2. POST /uploads with modified file — dedup rejects                   │
│ E3. DELETE /activities/{id} — 401 Application internal invalid for     │
│     Limited-tier apps (Approach 6)                                     │
│ E4. Strava web Crop feature — UI still exists, trims start/end of GPS  │
│     track to reduce distance. Web-only, no public API. Reachable via   │
│     session cookie? Unverified — next thing to investigate.            │
│ E5. Strava web distance edit — removed around 2024 (Approach 2)        │
│ E6. Request elevated app tier from Strava — 2-4 week wait, ~30%        │
│     approval odds based on community reports                           │
│ E7. Time-shifted upload (Plan B) — creates two activities for one      │
│     run, visible to friends. Functionally works but UX rejected.       │
└────────────────────────────────────────────────────────────────────────┘
                          ↓
                  Strava displays distance
```

## Approach 7: Strava web Crop / truncate via session cookie (v2.2, works ✓)

After Approach 6 hit the DELETE wall, mapping the attack surface (above) made E4 the obvious next thing to try: Strava's website still has a "Crop Activity" feature that trims the start/end of the GPS stream and recomputes distance. The OAuth API doesn't expose it, but the web form is reachable with a session cookie.

**What the form looks like.** Viewing source of `/activities/{id}/truncate`:

```html
<form action='/activities/{id}/truncate' method='post'>
  <input name='authenticity_token' type='hidden' value='…'>
  <input name='start_index' type='hidden' value='0'>
  <input name='end_index' type='hidden' value='2675'>
</form>
```

Three fields: a Rails CSRF token and two integer indices into the activity's GPS-point array. Cropping is a slider in the UI; the slider sets these two indices; submit POSTs them back. Strava re-derives distance from the trimmed point range.

**Implementation (`strava_cropper.py`).**

1. `GET /api/v3/activities/{id}/streams?keys=distance` (OAuth) → array of cumulative meters per GPS point.
2. Binary-search for the largest index where `distance[i] ≤ target_m`. That's the `end_index` that gets us to N.NN km without overshooting.
3. `GET /activities/{id}/truncate` (session cookie) → scrape the `authenticity_token` from the HTML.
4. `POST /activities/{id}/truncate` (session cookie + CSRF) with `start_index=0`, `end_index=<computed>`.

Strava processes synchronously and the activity is updated in place — same ID, all kudos and comments retained.

**End-to-end test result, 2026-05-16:** activity 18532273416 went from 8086.30 m → 8079.80 m (= 8.08 km) in under 5 seconds. 18 existing kudos preserved. HR / cadence / pace unchanged. Two GPS points (≈6 m) trimmed off the end.

**Why this beats every previous approach:**

| Property | API PUT (v1) | Web edit form (v1.5) | Delete + reupload (v2.1) | Crop (v2.2) |
| --- | --- | --- | --- | --- |
| Persists for GPS activities | ✗ (silently reverted) | ✗ (field removed) | ✗ (DELETE 401) | **✓** |
| Preserves kudos / comments | n/a | n/a | ✗ (delete loses them) | **✓** |
| Single Strava activity | ✓ | ✓ | needs `delete` | **✓** |
| Garmin data untouched | ✓ | ✓ | ✓ | **✓** |
| Works on Railway | ✓ | n/a | ✗ (DELETE 401) | **✓** |

**The costs we accept.**

- **Session cookie maintenance.** `_strava4_session` lives weeks-to-months. When it expires, the script logs an auth failure at the crop step and the user has to recapture from a browser. Same operational burden v1.5 had — and exactly the kind of fragility that killed v1.5 (Strava removed the distance field). The risk is Strava someday removing or restricting the crop form too.
- **GPS data is irreversibly trimmed.** Strava's own warning: "This action cannot be undone." We chop a few meters off the end of every run. Visible if you look hard at the map; invisible otherwise.
- **Crop isn't geometric scaling.** v2.0 scaled every GPS point uniformly toward the start — the route shape was preserved, just shrunk by 0.05 %. Crop just lops off the end. For a typical 19 km run trimmed to 19.19 km we lose ~10 m of route ending; for 8.32 km trimmed to 8.08 km we lose ~240 m. Bigger reductions = more visible map clip.

In exchange we get a feature that *actually works in production* on Strava's current platform.

## Production form: phone-triggered + self-maintaining (v2.3)

The crop pipeline from Approach 7 works, but a CLI you have to open your laptop for after every run isn't really a feature — most runs end with the person not near their laptop, often without a desire to ever open it.

Three deltas turn the CLI into something you actually use:

**1. HTTP wrapper + Railway deployment.** `sync_server.py` exposes `POST /sync` as a Flask endpoint, gated by an `X-Sync-Secret` header. Deployed to Railway with `Procfile` + `railway.toml`. The server reuses the same `sync.run()` from the CLI — no logic forked.

**2. iOS Shortcut.** Three actions: `Get Contents of URL` (POST to the Railway endpoint with the secret header), `Get Dictionary Value` (extract `strava_url` from the JSON response), `Show Notification` (display the URL). Added to the iPhone home screen. After a run: tap → ~30 seconds → notification with the new Strava URL.

**3. Credential auto-rotation.** The two credentials that previously needed periodic manual refresh now refresh themselves and write back to Railway's env vars:

- **Strava OAuth access token** — already auto-refreshes inside the running process. v2.3 adds a call to Railway's GraphQL `variableCollectionUpsert` mutation after each refresh, so the new token survives container restarts.
- **`_strava4_session` cookie** — Strava issues a fresh cookie on most responses. We capture it from `requests.Session.cookies` after every `truncate` POST. If it changed, push back to Railway. As long as you run at least once every few weeks, the cookie effectively never expires.

The only credential the system *cannot* auto-rotate is the Garmin OAuth1 token. That's ~1 year, requires interactive MFA over email/SMS, and Strava's rate-limited login endpoint makes the auto path too risky. Documented as a "once-a-year touch" in the README.

End-state architecture:

```
iPhone home screen
       │ tap "Strava Fix"
       ▼
iOS Shortcuts: POST → https://<railway>/sync
       │
       ▼
Railway container (sync_server.py)
       │
       ├─ login Garmin (cached token, no network)
       ├─ find latest running activity
       ├─ wait for Garmin → Strava auto-sync
       ├─ crop the Strava activity to N.NN
       ├─ capture rotated cookie & token, push back to Railway env vars
       └─ return JSON
       │
       ▼
iPhone notification: "Strava Fix: https://strava.com/activities/…"
```

Cost of ownership: opening Garmin Connect once a year to re-issue a token. That's it.

## Approach 8: Strava webhook auto-trigger (v2.4, the actual end state)

v2.3 is a feature you tap. Most runs end with the user not next to their phone, or already focused on something else. "Open phone, tap icon, wait, dismiss notification" is small but real friction.

The fix turned out to be where this project started, six versions ago: a **Strava webhook**. The v1.0 receiver was the right shape; v1.0's only real mistake was what it *did* on the event. Once we knew to call the web `truncate` form instead of the OAuth `PUT /activities/{id}`, the same architecture works.

**What changed for v2.4.**

- `sync_server.py` gained two endpoints. `GET /strava-webhook` echoes Strava's `hub.challenge` to complete the subscription handshake. `POST /strava-webhook` accepts activity events; on `aspect_type=create` it spawns a background thread so Strava's webhook timeout isn't blocked.
- New `sync.crop_strava_activity(strava_id)`: the webhook entry point. Skips Garmin entirely — Strava just told us the activity ID. Fetches the activity from Strava's API for distance + sport type, computes the `N.NN` target, calls the shared `strava_cropper.crop_to_distance()`. Idempotent via a `[target_m, target_m+10)` window check so any duplicate or re-delivered event is a no-op.
- `subscribe_webhook.py`: one-shot CLI that deletes any existing subscription (Strava allows one per OAuth app) and registers a fresh one pointing at the Railway domain.

**One subtle floor-display bug we caught and fixed late.** Strava displays distance as `floor(stored_m / 10) / 100`, **not** rounded. The original cropper picked the largest index where `distance ≤ target`, which lands a couple of meters under target and floors to `N.NN - 0.01`. Visible immediately on a 27.30 km → 27.27 target attempt that landed at 27.2680 m and displayed as `27.26 km`. The fix: pick the *smallest* index where `distance ≥ target`. GPS points are ~2 m apart so the overshoot stays well inside the `[target, target+10)` window the floor display tolerates. (The 27.26 activity is stuck — Strava crop is irreversible.)

**End-state architecture (v2.4, verified 2026-05-20):**

```
Garmin watch ──▶ Garmin Connect ──▶ Strava (auto-sync, original distance)
                                            │
                                            │ POST /strava-webhook
                                            ▼   (activity-create event)
                                    Railway sync_server
                                            │ (background thread)
                                            ├─ GET /activities/{id}
                                            ├─ GET /activities/{id}/streams
                                            ├─ binary-search for end_index
                                            ├─ GET /activities/{id}/truncate (CSRF)
                                            └─ POST /activities/{id}/truncate
                                            │
                                            ▼
                                    Strava activity now N.NN km
                                    (~5 s after Strava receives the auto-sync)
```

iOS Shortcut and the CLI are kept as backup paths against the day the webhook or the crop form changes. Three different triggers, one shared crop function.

**Cost of ownership at v2.4:** almost nothing. The cookie auto-rotates, the OAuth tokens auto-refresh. The one manual step is re-issuing the Garmin token when it expires — which turned out to be **monthly**, not annual (see the epilogue).

## Epilogue: the hosting migration (v2.5)

Two things broke the "set it and forget it" promise, about a month after v2.4 shipped:

1. **The Garmin OAuth1 token expired in ~3-4 weeks, not a year.** The iOS Shortcut started prompting for MFA on every tap — because the server, unable to use the stale cached token, fell back to a fresh SSO login whose MFA prompt nothing on the server side could answer. Lesson banked: *don't trust a vendor's implied token lifetime; measure it.* The fix is unavoidably manual (Garmin MFA can't be automated safely), so it's now documented as a monthly touch instead of annual.

2. **Railway's free trial credit ran out.** No permanent free tier; the cheapest always-on option is $5/mo. Rather than pay Railway's floor, migrated to Fly.io (~$2/mo for one 256 MB machine).

The migration also let us **replace the credential-persistence mechanism**. On Railway, rotated tokens were pushed back via the platform's GraphQL variable API — which *triggers a container restart on every write*, a latent hazard (a token refresh mid-webhook could kill an in-flight crop). Fly's equivalent secrets API has the same restart behavior, so instead we mounted a **persistent volume** and write rotating credentials to a file on it. No API call, no restart, and it survives redeploys. The Garmin token cache, `history.json`, and `sync.log` moved onto the volume too.

Verified end to end on Fly 2026-07-11: the one real unknown — whether Strava's `_strava4_session` cookie would be honored from a datacenter IP for the crop POST — came back fine.

The iOS Shortcut went away here too. It was built (v2.1–2.4) as a manual backup, but once the webhook proved reliable the Shortcut was just an extra HTTP endpoint (`/sync`) and a shared secret to maintain, exercised by nobody. Removed it and its `SYNC_SECRET`; the triggers are now webhook (automatic) + local CLI (backfill and the monthly Garmin re-auth).

**Cost of ownership at v2.5:** ~$2/mo, plus re-issuing the Garmin token monthly. Everything else self-maintains.

## Known limitation: crop resets elevation gain

Cropping changes more than distance. Any edit to a GPS activity makes Strava **re-derive every stream metric** from the trimmed track using its own algorithm — and for elevation that means discarding the device's barometric value and recomputing.

Confirmed on activity 19288038293 (`Paton Peak + Coliseum Mt`, a 30.31 km run with real climbing):

| Elevation source | Gain |
| --- | --- |
| Garmin fēnix 7x Pro barometric value (what Strava showed pre-crop) | 1658 m |
| Strava's recompute from the altitude stream, with its smoothing (post-crop) | **1701 m** |
| Raw altitude stream, no smoothing (25,763 points, Σ positive deltas) | 1916 m |

The +43 m is **not** from the ~10 m of track we trimmed — removing points can only lower or hold a gain, never raise it. It's Strava's smoothing being slightly less aggressive than Garmin's. The crop flips the elevation source from *device-reported* to *Strava-computed*, and the two disagree by a few percent. On flat runs the two nearly match, which is why this went unnoticed until a 1600 m-climb activity exposed it.

There is no lever. The `/truncate` form takes only `start_index` / `end_index` — no elevation field — and no Strava API lets you set elevation gain on a GPS activity (same fundamental limit as distance). It can't be minimized by choosing which end to trim either: the recompute is global, triggered by *any* edit, not by the specific points removed. The only way to keep the device elevation would be to never let Strava re-derive — i.e. modify the file before upload — which is the delete-and-reupload path we ruled out (DELETE 401, loses kudos).

Accepted as an inherent cost of the crop method: you trade an exact distance for a slightly-recomputed elevation number.

## Lessons

- **Don't try to modify data Strava already owns.** Strava treats every GPS activity's distance as a derived value. There is no API and no UI surface to override it, and the company appears to be removing the few that existed.
- **Intercept before the source-of-truth.** Modifying TCX/FIT before upload is the only stable pattern.
- **Webhook architectures aren't worth building for personal use** when the underlying API is closed (Garmin) or hostile to your goal (Strava). A 30-second CLI you run yourself is more reliable and cheaper than a 24/7 service that fails silently.
- **Test with the actual page, not just the API.** We spent v1.5 building a web form scraper assuming the distance field still existed in the HTML. It didn't. One DevTools peek would have saved that work.
- **A working pipeline isn't a finished feature.** v2.1 deploys, the iOS Shortcut fires, the server responds — looks complete. But the Strava `DELETE` step it depends on silently 401s in production. Always probe each step's actual *outcome* on the real target, not just whether the code ran.
- **Token rotation doesn't mean token replacement.** After regenerating Strava's Client Secret and re-authorizing, the old refresh token was still active (Strava reuses refresh tokens across re-auths within the same grant context). We had to explicitly *revoke* the app in Strava settings before reauth would actually mint a new refresh token.
- **Editing one derived field re-derives them all.** The crop targets distance, but any track edit makes Strava recompute elevation, pace, and moving time from scratch — swapping the device's barometric elevation for Strava's own. You don't get to touch a single number in isolation; the whole derived set moves. Verify the *other* metrics after an edit, not just the one you meant to change.
- **The right architecture from day one was a webhook.** v1.0 was a Flask service receiving Strava activity-create events on Railway. v2.4 is — almost identically — a Flask service receiving Strava activity-create events on Railway. Six versions in between were all wrong about the *call to make on the event*, not wrong about the trigger shape. The discovery work (web Crop form, session-cookie auth, CSRF parsing, floor display) was the entire content of those six versions. Architecturally, the project ends where it started.
- **Verify on the actual UI, not the API response.** Today's crop log said `27.2680m → 27.27 km`. The Strava API echoed that distance. Strava's web UI displayed `27.26 km`. The whole `27.27 → 27.26` floor-display bug would have been visible if the first crop test had ended with a screenshot of Strava's activity page, not just a JSON dump.
- **Persist credentials without triggering restarts.** Both Railway's and Fly's "set a secret" APIs redeploy the machine. Using them for per-request token rotation means a refresh mid-webhook can restart the container and kill an in-flight job. A file on a mounted volume persists just as well, with no restart and no API call.
- **Measure token lifetimes; don't trust the implied number.** The Garmin token was assumed good for ~a year; it expired in under a month and broke the whole trigger silently. The failure mode (server falling back to an un-answerable MFA prompt) was invisible until the phone shortcut started asking for codes.

## Archive

The v1 webhook implementation's README is preserved at [README-v1-archive.md](README-v1-archive.md) for reference. All of the v1 code is in the git history (look for tags `1.0.0` through `1.4.0` and the merge commit `fef1064`).
