#!/usr/bin/env python3
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import requests
from fast_flights import FlightData, Passengers, create_filter, get_flights_from_filter
from dotenv import load_dotenv

BOT_TOKEN = ""
ALLOWED_CHAT_ID = 0
API = None
OFFSET = 0
DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def load_env():
    global BOT_TOKEN, ALLOWED_CHAT_ID, API
    import os
    load_dotenv(Path(__file__).resolve().parent / ".env")
    BOT_TOKEN = os.environ.get("FLIGHTS_RESULTS_BOT_TOKEN") or os.environ.get("FLIGHT_BOT_TOKEN", "")
    ALLOWED_CHAT_ID = int(os.environ.get("FLIGHTS_RESULTS_BOT_CHAT_ID") or os.environ.get("FLIGHT_BOT_CHAT_ID") or "0")
    API = f"https://api.telegram.org/bot{BOT_TOKEN}"


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


def _parse_date(text):
    text = text.strip().lower()
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return today.strftime("%Y-%m-%d")
    if text == "tomorrow":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    for day_name, day_num in DAY_NAMES.items():
        if day_name in text:
            days_ahead = day_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = today + timedelta(days=days_ahead)
            return target.strftime("%Y-%m-%d")
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in ["%b %d", "%B %d"]:
        try:
            d = datetime.strptime(text, fmt).replace(year=now.year)
            if d < now:
                d = d.replace(year=now.year + 1)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        d = datetime.strptime(text, "%m/%d").replace(year=now.year)
        if d < now:
            d = d.replace(year=now.year + 1)
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_fly_args(args):
    parts = args.strip().split()
    if len(parts) < 3:
        raise ValueError("Usage: /fly <from> <to> <depart date> [to <return date>]")
    origin = parts[0].upper()
    dest = parts[1].upper()
    if not re.fullmatch(r"[A-Z]{3}", origin) or not re.fullmatch(r"[A-Z]{3}", dest):
        raise ValueError("Origin and destination must be 3-letter airport codes.")
    date_text = " ".join(parts[2:]).strip()
    split = re.split(r'\bto\b|\bthrough\b|\bthru\b|\btill\b|\buntil\b|→|–|-{1,2}(?!\d{2}-)', date_text, maxsplit=1)
    depart_text = split[0].strip()
    return_text = split[1].strip() if len(split) > 1 else ""
    for filler in ["the ", "on "]:
        if depart_text.startswith(filler):
            depart_text = depart_text[len(filler):]
        if return_text.startswith(filler):
            return_text = return_text[len(filler):]
    depart_text = re.sub(r'[\.\s,!?]+$', '', depart_text).strip()
    return_text = re.sub(r'[\.\s,!?]+$', '', return_text).strip()
    depart_date = _parse_date(depart_text) if depart_text else None
    return_date = _parse_date(return_text) if return_text else None
    if not depart_date:
        raise ValueError("Need at least a valid departure date.")
    return origin, dest, depart_date, return_date


def _flight_name(result):
    flights = getattr(result, "flights", []) or []
    names = [getattr(f, "name", None) for f in flights if getattr(f, "name", None)]
    return " / ".join(names) if names else "Unknown airline"


def _flight_times(result):
    flights = getattr(result, "flights", []) or []
    if not flights:
        return "?→?"
    first = flights[0]
    last = flights[-1]
    depart = getattr(first, "departure", None) or "?"
    arrive = getattr(last, "arrival", None) or "?"
    return f"{depart}→{arrive}"


def _flight_duration(result):
    flights = getattr(result, "flights", []) or []
    if not flights:
        return "?"
    return getattr(flights[0], "duration", None) or "?"


def _flight_stops(result):
    flights = getattr(result, "flights", []) or []
    if not flights:
        return "?"
    stops = getattr(flights[0], "stops", None)
    if stops is None:
        return "?"
    if str(stops) == "0":
        return "Nonstop"
    return f"{stops} stop" if str(stops) == "1" else f"{stops} stops"


def _flight_price(result):
    return getattr(result, "current_price", None) or getattr(result, "price", None) or "?"


def fetch_flights(origin, dest, depart_date, return_date):
    flight_data = [
        FlightData(date=depart_date, from_airport=origin, to_airport=dest),
    ]
    trip = "one-way"
    if return_date:
        trip = "round-trip"
        flight_data.append(FlightData(date=return_date, from_airport=dest, to_airport=origin))
    flight_filter = create_filter(
        flight_data=flight_data,
        trip=trip,
        seat="economy",
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
    )
    return get_flights_from_filter(filter=flight_filter)


def format_results(origin, dest, depart_date, return_date, results):
    outbound = list(getattr(results, "flights", []) or [])
    best_flights = list(getattr(results, "best_flights", []) or [])
    other_flights = list(getattr(results, "other_flights", []) or [])
    candidates = best_flights + other_flights
    if not candidates and outbound and hasattr(outbound[0], "departure"):
        candidates = outbound
    header = f"✈️ {origin} → {dest} ({depart_date}"
    if return_date:
        header += f" → {return_date}"
    header += ")\n"
    price = getattr(results, "current_price", None)
    if price:
        header += f"Current best price: {price}\n\n"
    else:
        header += "\n"
    if not candidates:
        return header + "No flights returned."
    lines = []
    for i, result in enumerate(candidates[:15], 1):
        lines.append(
            f"{i}. {_flight_name(result)}  {_flight_times(result)}  {_flight_duration(result)}  {_flight_stops(result)}  {_flight_price(result)}"
        )
    return header + "\n".join(lines)


def handle_fly(chat_id, args):
    origin, dest, depart_date, return_date = parse_fly_args(args)
    tg_send(chat_id, f"🔍 Searching {origin} → {dest} ({depart_date}" + (f" → {return_date})" if return_date else ")") + " via Google Flights API...")
    results = fetch_flights(origin, dest, depart_date, return_date)
    tg_send(chat_id, format_results(origin, dest, depart_date, return_date, results))


def handle_help(chat_id, _args):
    tg_send(
        chat_id,
        "Flights Results Bot\n\n"
        "/fly SFO PUJ July 14 to July 17\n"
        "/fly SFO LAX tomorrow\n"
        "/help",
    )


COMMANDS = {
    "/fly": handle_fly,
    "/help": handle_help,
    "/start": handle_help,
}


def process_message(msg):
    chat_id = msg["chat"]["id"]
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        tg_send(chat_id, "⛔ Unauthorized. This bot only responds to its owner.")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    for cmd, handler in COMMANDS.items():
        if text.lower().startswith(cmd):
            args = text[len(cmd):].strip()
            try:
                handler(chat_id, args)
            except Exception as e:
                tg_send(chat_id, f"❌ Error: {e}\n\n{traceback.format_exc()[-700:]}")
            return
    tg_send(chat_id, "Try `/fly SFO PUJ July 14 to July 17` or `/help`.")


def main():
    load_env()
    if not BOT_TOKEN:
        print("ERROR: Set FLIGHTS_RESULTS_BOT_TOKEN or FLIGHT_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)
    print("✈️ Flights Results Bot starting...")
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
            print("\n✈️ Flights Results Bot stopped.")
            break
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
