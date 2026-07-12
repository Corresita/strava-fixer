"""Webhook server. Deployed on Fly.io. Auto-crops new Strava runs.

Endpoints
---------
GET  /               health check
GET  /strava-webhook Strava subscription-verify handshake
POST /strava-webhook Strava webhook: auto-crop a just-created activity

The webhook is verified by Strava's subscription handshake
(STRAVA_WEBHOOK_VERIFY_TOKEN). For manual runs / backfill, use the CLI
(`python sync.py`) locally.

Token bootstrap
---------------
On startup, if GARMIN_TOKEN_B64 is set and no token cache exists yet (on the
Fly volume at $DATA_DIR/garmin_tokens/), decode the blob and write it. This
lets a fresh volume reuse a token captured locally without hitting Garmin's
rate-limited SSO. Regenerate after each Garmin re-auth:

    python -c "import base64,pathlib; \
        print(base64.b64encode(pathlib.Path('garmin_tokens/garmin_tokens.json').read_bytes()).decode())"
"""
from __future__ import annotations

import base64
import os
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()


def _bootstrap_garmin_token() -> None:
    """Write the Garmin token from the GARMIN_TOKEN_B64 secret if the cache is
    empty. On a persistent volume ($DATA_DIR) the cache survives restarts, so
    this only fires on the very first boot or after a manual token rotation."""
    blob = os.environ.get("GARMIN_TOKEN_B64", "")
    if not blob:
        return
    data_dir = os.environ.get("DATA_DIR", "")
    base = Path(data_dir) if data_dir else Path(__file__).parent
    target = base / "garmin_tokens" / "garmin_tokens.json"
    if target.exists():
        print(f"[sync_server] {target} exists, skipping bootstrap", flush=True)
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(blob))
        print(f"[sync_server] wrote {target} from GARMIN_TOKEN_B64 ({len(blob)}b base64)", flush=True)
    except Exception as e:
        print(f"[sync_server] ERROR bootstrapping Garmin token: {e}", file=sys.stderr, flush=True)


_bootstrap_garmin_token()

# Import sync AFTER bootstrap so the cached token is in place before any
# Garmin call is attempted.
import sync  # noqa: E402

app = Flask(__name__)
WEBHOOK_VERIFY = os.environ.get("STRAVA_WEBHOOK_VERIFY_TOKEN", "")


@app.route("/", methods=["GET"])
def health():
    return "Strava Distance Fixer (v2 sync server) is running.", 200


@app.route("/strava-webhook", methods=["GET"])
def strava_webhook_verify():
    """Strava sends GET ?hub.mode=subscribe&hub.challenge=X&hub.verify_token=...
    when first registering the subscription. Echo the challenge back."""
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == WEBHOOK_VERIFY):
        return jsonify({"hub.challenge": request.args.get("hub.challenge")}), 200
    print(f"[webhook] verify rejected: args={dict(request.args)}", flush=True)
    return "forbidden", 403


@app.route("/strava-webhook", methods=["POST"])
def strava_webhook_event():
    """Strava POSTs activity events here. Spawn a background thread so we
    respond fast (Strava times out webhook responses quickly)."""
    event = request.get_json(silent=True) or {}
    print(f"[webhook] event: {event}", flush=True)

    if event.get("object_type") == "activity" and event.get("aspect_type") == "create":
        activity_id = event.get("object_id")
        if activity_id:
            import threading
            threading.Thread(
                target=_async_crop, args=(int(activity_id),), daemon=True
            ).start()

    return "", 200


def _async_crop(strava_id: int) -> None:
    try:
        sync.crop_strava_activity(strava_id)
    except Exception as e:
        traceback.print_exc()
        print(f"[webhook] crop_strava_activity({strava_id}) raised: {e}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
