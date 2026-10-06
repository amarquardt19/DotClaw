#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def load_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

BOT_TOKEN = os.environ.get("EMMYCAR_BOT_TOKEN", "")
ALLOWED_CHAT_ID = int(os.environ.get("EMMYCAR_BOT_CHAT_ID", "0") or "0")
HOME_ADDRESS = os.environ.get("HOME_ADDRESS") or os.environ.get("EMMYCAR_HOME_ADDRESS", "2810 Kensington Ave, Richmond VA")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET = 0
BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = BASE_DIR / "scripts" / "ride_orchestrator_v2.py"
YES_WORDS = {"yes", "y", "ok", "okay", "confirm"}
LAST_REQUEST = {}


def tg_request(method, payload=None):
    payload = payload or {}
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(f"{API}/{method}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg_send(chat_id, text):
    for i in range(0, len(text), 4000):
        tg_request("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4000], "disable_web_page_preview": "true"})


def tg_get_updates(timeout=30):
    global OFFSET
    req = urllib.request.Request(
        f"{API}/getUpdates?offset={OFFSET}&timeout={timeout}",
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("result", [])
    if results:
        OFFSET = results[-1]["update_id"] + 1
    return results


def parse_request(text):
    cleaned = text.strip()
    cleaned = re.sub(r'^(ride|uber|lyft|take me|go|call an uber|call me an uber)\s+', '', cleaned, flags=re.IGNORECASE)
    to_match = re.search(r'(.+?)\s+to\s+(.+)', cleaned, re.IGNORECASE)
    if to_match:
        pickup = to_match.group(1).strip()
        destination = to_match.group(2).strip()
    else:
        pickup = HOME_ADDRESS
        destination = cleaned.strip()
    if pickup.lower() == "home":
        pickup = HOME_ADDRESS
    return pickup, destination


def run_ride_orchestrator(pickup, destination):
    env = {
        **os.environ,
        "EMMYCAR_BROWSER_CDP_PORT": "9333",
        "EMMYCAR_LYFT_CDP_URL": "http://127.0.0.1:9333",
    }
    result = subprocess.run(
        ["python3", str(SCRIPT_PATH), pickup, destination],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=str(BASE_DIR),
        env=env,
    )
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if not stdout:
        return {"success": False, "error": f"No output from ride orchestrator. STDERR: {stderr[:500]}"}
    try:
        payload = json.loads(stdout.split("\n")[-1])
    except Exception as e:
        return {"success": False, "error": f"Invalid orchestrator JSON: {e}. STDOUT: {stdout[-500:]} STDERR: {stderr[:500]}"}
    if stderr:
        payload["stderr"] = stderr[:1000]
    return payload


def process_message(msg):
    chat_id = msg.get("chat", {}).get("id", 0)
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        tg_send(chat_id, "⛔ Unauthorized. This bot only responds to its owner.")
        return
    lower = text.lower()
    if lower in {"/start", "/help"}:
        tg_send(chat_id, "Send a ride like: home to Scott's Addition Richmond")
        return
    if lower == "/cancel":
        LAST_REQUEST.pop(chat_id, None)
        tg_send(chat_id, "🛑 Canceled.")
        return
    pickup, destination = parse_request(text)
    LAST_REQUEST[chat_id] = {"pickup": pickup, "destination": destination}
    tg_send(chat_id, f"🚗 Running ride orchestrator\nPickup: {pickup}\nDestination: {destination}")
    try:
        payload = run_ride_orchestrator(pickup, destination)
        if payload.get("success"):
            ride = payload.get("selected_ride", {})
            tg_send(
                chat_id,
                f"✅ Booked {ride.get('full_type', 'ride')} for ${ride.get('price', 0):.2f} ({ride.get('eta_min', '?')} min away)",
            )
        else:
            tg_send(chat_id, f"❌ Ride orchestrator failed: {payload.get('error', 'Unknown error')}")
    except Exception as e:
        tg_send(chat_id, f"❌ Bot error: {e}\n\n{traceback.format_exc()[-1000:]}")


def main():
    if not BOT_TOKEN:
        raise SystemExit("EMMYCAR_BOT_TOKEN is not configured")
    print(f"🚗 Telegram ride orchestrator bot starting for chat {ALLOWED_CHAT_ID or 'any'}")
    while True:
        try:
            updates = tg_get_updates(timeout=30)
            for update in updates:
                msg = update.get("message")
                if msg:
                    process_message(msg)
        except KeyboardInterrupt:
            print("\n🚗 Telegram ride orchestrator bot stopped.")
            break
        except urllib.error.HTTPError as e:
            print(f"Telegram HTTP error: {e.code} {e.read().decode('utf-8', errors='ignore')[:500]}")
            time.sleep(2)
        except Exception as e:
            print(f"Bot loop error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
