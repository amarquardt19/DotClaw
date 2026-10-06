#!/usr/bin/env python3
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv

from flights_search_service import build_search_request, fetch_flights, _flight_duration, _flight_name, _flight_price_text, _flight_stops_text, _flight_time_summary

BOT_TOKEN = ""
ALLOWED_CHAT_ID = 0
API = None
OFFSET = 0
SEARCH_STATE = {}
ROOT = Path(__file__).resolve().parent
UNITED_ATTACH_SCRIPT = ROOT / "scripts" / "united_playwright_attach.py"
UNITED_CDP_URL = os.environ.get("UNITED_BROWSER_CDP_URL") or f"http://127.0.0.1:{os.environ.get('UNITED_BROWSER_CDP_PORT', '18801')}"
HELP_TEXT = (
    "Fast Flights Bot\n\n"
    "Examples:\n"
    "/fly RIC to CDG May 26\n"
    "/fly RIC to CDG May 26 to Jun 2\n"
    "RIC to CDG May 26\n"
    "RIC to CDG May 26 to Jun 2"
)


def load_env():
    global BOT_TOKEN, ALLOWED_CHAT_ID, API
    load_dotenv(Path(__file__).resolve().parent / ".env")
    BOT_TOKEN = (
        os.environ.get("FAST_FLIGHTS_BOT_TOKEN")
        or os.environ.get("FLIGHTS_RESULTS_BOT_TOKEN")
        or os.environ.get("FLIGHT_BOT_TOKEN", "")
    )
    ALLOWED_CHAT_ID = int(
        os.environ.get("FAST_FLIGHTS_BOT_CHAT_ID")
        or os.environ.get("FLIGHTS_RESULTS_BOT_CHAT_ID")
        or os.environ.get("FLIGHT_BOT_CHAT_ID")
        or "0"
    )
    API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_get_updates(timeout=30):
    global OFFSET
    resp = requests.get(
        f"{API}/getUpdates",
        params={"offset": OFFSET, "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 5,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {data.get('description') or data}")
    results = data.get("result", [])
    if results:
        OFFSET = results[-1]["update_id"] + 1
    return results


def tg_send(chat_id, text):
    for i in range(0, len(text), 4000):
        resp = requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text[i:i + 4000]},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {data.get('description') or data}")


def normalize_text(value):
    return " ".join((value or "").strip().split())


def _duration_minutes(result):
    duration = str(getattr(result, "duration", None) or "").strip().lower()
    if not duration:
        return 10**9
    hours_match = re.search(r"(\d+)\s*hr", duration)
    minutes_match = re.search(r"(\d+)\s*min", duration)
    if not hours_match and not minutes_match:
        return 10**9
    hours = int(hours_match.group(1)) if hours_match else 0
    minutes = int(minutes_match.group(1)) if minutes_match else 0
    return hours * 60 + minutes


def _sort_results_by_duration(results):
    best_flights = list(getattr(results, "best_flights", []) or [])
    other_flights = list(getattr(results, "other_flights", []) or [])
    flights = list(getattr(results, "flights", []) or [])
    if best_flights or other_flights:
        combined = sorted(best_flights + other_flights, key=_duration_minutes)
        results.best_flights = combined
        results.other_flights = []
    elif flights:
        results.flights = sorted(flights, key=_duration_minutes)
    return results


def _format_sorted_results(search_request, results, limit=10):
    best_flights = list(getattr(results, "best_flights", []) or [])
    other_flights = list(getattr(results, "other_flights", []) or [])
    flights = list(getattr(results, "flights", []) or [])
    ranked = best_flights + other_flights if (best_flights or other_flights) else flights
    lines = [f"✈️ {search_request['origin']} → {search_request['destination']} ({search_request['depart_date']}"]
    if search_request.get("return_date"):
        lines[0] += f" → {search_request['return_date']}"
    lines[0] += ")"
    if not ranked:
        lines.append("No flights found.")
        return "\n".join(lines)
    for index, result in enumerate(ranked[:limit], 1):
        duration = getattr(result, "duration", None) or "?"
        lines.append(f"\n{index}. {_flight_name(result)} — {_flight_price_text(result)}")
        lines.append(f"   {_flight_time_summary(result)} · {duration} · {_flight_stops_text(result)}")
    lines.append("\nReply with a number to start booking that option on United when available.")
    return "\n".join(lines)


def _ranked_results(results):
    best_flights = list(getattr(results, "best_flights", []) or [])
    other_flights = list(getattr(results, "other_flights", []) or [])
    flights = list(getattr(results, "flights", []) or [])
    return best_flights + other_flights if (best_flights or other_flights) else flights


def _launch_united_booking(search_request, selected=None):
    trip_type = "roundtrip" if search_request.get("return_date") else "oneway"
    command = [
        sys.executable,
        str(UNITED_ATTACH_SCRIPT),
        "--cdp-url",
        UNITED_CDP_URL,
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
    if selected is not None:
        command.extend([
            "--selected-airline",
            _flight_name(selected),
            "--selected-time-summary",
            _flight_time_summary(selected),
            "--selected-duration",
            str(_flight_duration(selected) or ""),
            "--selected-stops",
            _flight_stops_text(selected),
            "--selected-price",
            _flight_price_text(selected),
        ])
    if search_request.get("return_date"):
        command.extend(["--return-date", search_request["return_date"]])
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _handle_selection(chat_id, text):
    match = re.fullmatch(r"(\d{1,2})", text.strip())
    if not match:
        return False
    if chat_id not in SEARCH_STATE:
        tg_send(chat_id, "⚠️ I don't have an active flight list for that selection right now. Please run the search again, then reply with the option number.")
        return True
    state = SEARCH_STATE[chat_id]
    idx = int(match.group(1)) - 1
    results = state.get("results", [])
    if idx < 0 or idx >= len(results):
        tg_send(chat_id, f"⚠️ Pick a number from 1 to {len(results)}.")
        return True
    selected = results[idx]
    airline = (_flight_name(selected) or "").lower()
    if "united" not in airline:
        tg_send(chat_id, f"⚠️ Option {idx + 1} is {_flight_name(selected)}. Right now I only wired browser handoff for United.")
        return True
    _launch_united_booking(state["search_request"], selected)
    tg_send(chat_id, f"✈️ Opening United booking flow for option {idx + 1}: {_flight_name(selected)} {_flight_time_summary(selected)}")
    return True


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


def handle_help(chat_id, _args=""):
    tg_send(chat_id, HELP_TEXT)


def handle_fly(chat_id, args):
    answers = parse_search_message(args)
    search_request = build_search_request(answers)
    results = fetch_flights(search_request)
    results = _sort_results_by_duration(results)
    SEARCH_STATE[chat_id] = {
        "search_request": search_request,
        "results": _ranked_results(results),
    }
    tg_send(chat_id, _format_sorted_results(search_request, results, limit=10))


def process_message(msg):
    chat_id = msg["chat"]["id"]
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        tg_send(chat_id, "⛔ Unauthorized. This bot only responds to its owner.")
        return
    text = normalize_text(msg.get("text") or "")
    if not text:
        return
    lowered = text.lower()
    if lowered in {"/start", "/help", "help", "hi", "hello", "start"}:
        handle_help(chat_id)
        return
    try:
        if _handle_selection(chat_id, text):
            return
        if lowered.startswith("/fly"):
            args = text[4:].strip()
            handle_fly(chat_id, args)
            return
        if re.match(r"^[A-Za-z]{3}\s+to\s+[A-Za-z]{3}\s+.+$", text, flags=re.IGNORECASE):
            handle_fly(chat_id, text)
            return
        tg_send(chat_id, HELP_TEXT)
    except Exception as exc:
        tg_send(chat_id, f"❌ Error: {exc}")


def main():
    load_env()
    if not BOT_TOKEN:
        print(
            "ERROR: Set FAST_FLIGHTS_BOT_TOKEN or FLIGHTS_RESULTS_BOT_TOKEN or FLIGHT_BOT_TOKEN",
            file=sys.stderr,
        )
        sys.exit(1)
    print("✈️ Fast Flights Telegram Bot starting...")
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
            print("\n✈️ Fast Flights Telegram Bot stopped.")
            break
        except requests.exceptions.Timeout:
            continue
        except Exception as exc:
            print(f"Error: {type(exc).__name__}: {exc!r}")
            traceback.print_exc()
            time.sleep(2)


if __name__ == "__main__":
    main()
