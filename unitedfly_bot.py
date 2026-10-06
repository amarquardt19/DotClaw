#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import time
import urllib.request
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

from flights_search_service import build_search_request

BOT_TOKEN = ""
ALLOWED_CHAT_ID = 0
API = ""
OFFSET = 0
ROOT = Path(__file__).resolve().parent
UNITED_ATTACH_SCRIPT = ROOT / "scripts" / "united_playwright_attach.py"
UNITED_LOG_DIR = ROOT / ".playwright" / "artifacts"
UNITED_CDP_URL = ""
HELP_TEXT = (
    "UnitedFly Bot\n\n"
    "Examples:\n"
    "/fly RIC to CDG May 26\n"
    "/fly RIC to CDG May 26 to Jun 2\n"
    "RIC to CDG May 26\n"
    "RIC to CDG May 26 to Jun 2"
)


def load_env():
    global BOT_TOKEN, ALLOWED_CHAT_ID, API, UNITED_CDP_URL
    load_dotenv(ROOT / ".env")
    BOT_TOKEN = (
        os.environ.get("UNITEDFLY_BOT_TOKEN")
        or os.environ.get("UNITED_FLY_BOT_TOKEN")
        or os.environ.get("FLIGHT_BOT_TOKEN", "")
    )
    ALLOWED_CHAT_ID = int(
        os.environ.get("UNITEDFLY_BOT_CHAT_ID")
        or os.environ.get("UNITED_FLY_BOT_CHAT_ID")
        or os.environ.get("FLIGHT_BOT_CHAT_ID")
        or "0"
    )
    API = f"https://api.telegram.org/bot{BOT_TOKEN}"
    UNITED_CDP_URL = os.environ.get("UNITED_BROWSER_CDP_URL") or f"http://127.0.0.1:{os.environ.get('UNITED_BROWSER_CDP_PORT', '18801')}"


def resolve_cdp_url(cdp_url):
    if cdp_url.startswith("ws://") or cdp_url.startswith("wss://"):
        return cdp_url
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(version_url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("webSocketDebuggerUrl") or cdp_url
    except Exception:
        return cdp_url


def tg_get_updates(timeout=30):
    global OFFSET
    resp = requests.get(
        f"{API}/getUpdates",
        params={"offset": OFFSET, "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 5,
    )
    data = resp.json()
    if not data.get("ok"):
        return []
    results = data.get("result", [])
    if results:
        OFFSET = results[-1]["update_id"] + 1
    return results


def tg_send(chat_id, text):
    for i in range(0, len(text), 4000):
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text[i:i + 4000]},
            timeout=15,
        )


def normalize_text(value):
    return " ".join((value or "").strip().split())


def parse_search_message(message_body):
    text = normalize_text(message_body)
    match = re.match(r"^([A-Za-z]{3})\s+to\s+([A-Za-z]{3})\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        raise ValueError(HELP_TEXT)
    origin = match.group(1).upper()
    destination = match.group(2).upper()
    remainder = match.group(3).strip()
    split = re.split(r"\s+to\s+", remainder, maxsplit=1, flags=re.IGNORECASE)
    depart_date = split[0].strip()
    return_date = split[1].strip() if len(split) > 1 else None
    return {
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date,
        "return_date": return_date,
        "nonstop_required": False,
        "refundable_required": False,
        "changes_outbound": False,
        "changes_return": True,
        "seat_preference": "seat choice included",
        "early_boarding": False,
        "bags": "checked",
        "preferred_airline": None,
        "avoid_airline": None,
        "miles_status": None,
        "auto_book": "ask",
        "anything_else": None,
    }


def launch_united_booking(search_request):
    trip_type = "roundtrip" if search_request.get("return_date") else "oneway"
    UNITED_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = UNITED_LOG_DIR / f"unitedfly-launch-{time.strftime('%Y%m%d-%H%M%S')}.log"
    attach_cdp_url = resolve_cdp_url(UNITED_CDP_URL)
    command = [
        sys.executable,
        str(UNITED_ATTACH_SCRIPT),
        "--cdp-url",
        attach_cdp_url,
        "--trip-type",
        trip_type,
        "--origin",
        search_request["origin"],
        "--destination",
        search_request["destination"],
        "--depart",
        search_request["depart_date"],
        "--stay-open",
    ]
    if search_request.get("return_date"):
        command.extend(["--return-date", search_request["return_date"]])
    log_handle = open(log_path, "a", encoding="utf-8")
    log_handle.write(f"[unitedfly-bot] launching with cdp={attach_cdp_url}\n")
    log_handle.flush()
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return process, log_path


def handle_help(chat_id):
    tg_send(chat_id, HELP_TEXT)


def handle_fly(chat_id, args):
    answers = parse_search_message(args)
    search_request = build_search_request(answers)
    process, log_path = launch_united_booking(search_request)
    trip_label = f"{search_request['depart_date']}"
    if search_request.get("return_date"):
        trip_label += f" → {search_request['return_date']}"
    tg_send(
        chat_id,
        f"✈️ Opening United booking flow for {search_request['origin']} → {search_request['destination']} ({trip_label}).\nLog: {log_path.name}\nPID: {process.pid}",
    )


def process_message(msg):
    chat_id = msg["chat"]["id"]
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        tg_send(chat_id, "⛔ Unauthorized. This bot only responds to its owner.")
        return
    text = normalize_text(msg.get("text") or "")
    if not text:
        return
    lowered = text.lower()
    try:
        if lowered in {"/start", "/help", "help", "hi", "hello", "start"}:
            handle_help(chat_id)
            return
        if lowered.startswith("/fly"):
            handle_fly(chat_id, text[4:].strip())
            return
        if re.match(r"^[A-Za-z]{3}\s+to\s+[A-Za-z]{3}\s+.+$", text, flags=re.IGNORECASE):
            handle_fly(chat_id, text)
            return
        handle_help(chat_id)
    except Exception as exc:
        tg_send(chat_id, f"❌ Error: {exc}")


def main():
    load_env()
    if not BOT_TOKEN:
        print("ERROR: Set UNITEDFLY_BOT_TOKEN or UNITED_FLY_BOT_TOKEN or FLIGHT_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)
    print("✈️ UnitedFly Telegram Bot starting...")
    if ALLOWED_CHAT_ID:
        print(f"Allowed chat: {ALLOWED_CHAT_ID}")
    else:
        print("⚠️ No bot chat id restriction set")
    while True:
        try:
            for update in tg_get_updates(timeout=30):
                msg = update.get("message")
                if msg:
                    process_message(msg)
        except KeyboardInterrupt:
            print("\n✈️ UnitedFly Telegram Bot stopped.")
            break
        except requests.exceptions.Timeout:
            continue
        except Exception as exc:
            print(f"Error: {exc}")
            time.sleep(2)


if __name__ == "__main__":
    main()
