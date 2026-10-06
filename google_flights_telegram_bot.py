#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

BOT_TOKEN = ""
ALLOWED_CHAT_ID = 0
API = ""
OFFSET = 0
ROOT = Path(__file__).resolve().parent
GW_TOKEN = ""
GW_URL = ""
BROWSER_PROFILE = os.environ.get("GOOGLE_FLIGHTS_BROWSER_PROFILE", "openclaw")
FLIGHT_RESULTS = {}
DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
HELP_TEXT = (
    "Google Flights Telegram Bot\n\n"
    "Examples:\n"
    "/fly RIC to CDG May 26\n"
    "/fly RIC to CDG May 26 to Jun 2\n"
    "RIC to CDG May 26\n"
    "RIC to CDG May 26 to Jun 2"
)


def load_env():
    global BOT_TOKEN, ALLOWED_CHAT_ID, API, BROWSER_PROFILE
    load_dotenv(ROOT / ".env")
    BOT_TOKEN = os.environ.get("GOOGLE_FLIGHTS_BOT_TOKEN") or os.environ.get("FLIGHT_BOT_TOKEN", "")
    ALLOWED_CHAT_ID = int((os.environ.get("GOOGLE_FLIGHTS_BOT_CHAT_ID") or os.environ.get("FLIGHT_BOT_CHAT_ID") or "0").strip())
    API = f"https://api.telegram.org/bot{BOT_TOKEN}"
    BROWSER_PROFILE = os.environ.get("GOOGLE_FLIGHTS_BROWSER_PROFILE", BROWSER_PROFILE)


def load_gateway():
    global GW_TOKEN, GW_URL
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if not cfg_path.exists():
        return
    cfg = json.loads(cfg_path.read_text())
    GW_TOKEN = cfg.get("gateway", {}).get("auth", {}).get("token", "")
    port = cfg.get("gateway", {}).get("port", 18789)
    GW_URL = f"ws://127.0.0.1:{port}"


def resolve_browser_profile():
    configured = (BROWSER_PROFILE or "").strip()
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if not cfg_path.exists():
        return configured or "openclaw"
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception:
        return configured or "openclaw"
    profiles = cfg.get("browser", {}).get("profiles", {}) or {}
    if configured and configured in profiles:
        return configured
    if profiles:
        return next(iter(profiles.keys()))
    return configured or "openclaw"


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


def browser_cmd(*args, timeout=60, retries=1):
    cmd = ["openclaw", "browser"] + list(args) + ["--browser-profile", BROWSER_PROFILE]
    if GW_TOKEN:
        cmd.extend(["--token", GW_TOKEN])
    if GW_URL:
        cmd.extend(["--url", GW_URL])
    last_err = None
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip()
                if "timeout" in err.lower() and attempt < retries - 1:
                    last_err = err
                    time.sleep(2)
                    continue
                raise RuntimeError(err)
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            last_err = f"subprocess timeout after {timeout}s"
            if attempt < retries - 1:
                time.sleep(2)
                continue
            raise RuntimeError(last_err)
    raise RuntimeError(last_err or "browser command failed")


def browser_navigate(url):
    return browser_cmd("navigate", url)


def browser_snapshot(limit=350):
    raw = browser_cmd("snapshot", "--labels", "--limit", str(limit), timeout=90, retries=2)
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("- RootWebArea") or s.startswith("- root"):
            return "\n".join(lines[i:])
    return raw


def browser_click(ref):
    return browser_cmd("click", ref)


def browser_fill(ref, value):
    fields_json = json.dumps([{"ref": ref, "value": value}])
    return browser_cmd("fill", "--fields", fields_json)


def browser_type(ref, text):
    return browser_cmd("type", ref, text)


def active_tab_ws_url():
    try:
        data = urllib.request.urlopen("http://127.0.0.1:18800/json/list", timeout=5).read()
        tabs = json.loads(data)
        for tab in tabs:
            if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                return tab.get("webSocketDebuggerUrl")
    except Exception:
        return None
    return None


def cdp_key_press(key):
    ws_url = active_tab_ws_url()
    if not ws_url:
        return False
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        for event_type in ["keyDown", "keyUp"]:
            ws.send(json.dumps({
                "id": 1 if event_type == "keyDown" else 2,
                "method": "Input.dispatchKeyEvent",
                "params": {"type": event_type, "key": key, "code": key, "windowsVirtualKeyCode": 40 if key == "ArrowDown" else 13 if key == "Enter" else 0},
            }))
            ws.recv()
        ws.close()
        return True
    except Exception:
        return False


def cdp_eval(expression):
    ws_url = active_tab_ws_url()
    if not ws_url:
        return None
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        raw = json.loads(ws.recv())
        ws.close()
        return raw.get("result", {}).get("result", {}).get("value")
    except Exception:
        return None


def cdp_type_text(text):
    ws_url = active_tab_ws_url()
    if not ws_url:
        return False
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        ws.send(json.dumps({
            "id": 1,
            "method": "Input.insertText",
            "params": {"text": text},
        }))
        ws.recv()
        ws.close()
        return True
    except Exception:
        return False


def normalize_text(value):
    return " ".join((value or "").strip().split())


def parse_date(text):
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
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    for fmt in ["%Y-%m-%d", "%b %d", "%B %d", "%m/%d"]:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt != "%Y-%m-%d":
                parsed = parsed.replace(year=now.year)
                if parsed < now:
                    parsed = parsed.replace(year=now.year + 1)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_search_message(message_body):
    text = normalize_text(message_body)
    match = re.match(r"^([A-Za-z]{3})\s+to\s+([A-Za-z]{3})\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        raise ValueError(HELP_TEXT)
    remainder = match.group(3).strip()
    split = re.split(r"\s+to\s+", remainder, maxsplit=1, flags=re.IGNORECASE)
    depart_date = parse_date(split[0].strip())
    return_date = parse_date(split[1].strip()) if len(split) > 1 else None
    if not depart_date:
        raise ValueError("Could not parse departure date.")
    if len(split) > 1 and not return_date:
        raise ValueError("Could not parse return date.")
    return {
        "origin": match.group(1).upper(),
        "destination": match.group(2).upper(),
        "depart_date": depart_date,
        "return_date": return_date,
    }


def find_ref_matching(snap, predicate):
    for line in snap.split("\n"):
        s = line.strip()
        match = re.search(r"\[ref=(\w+)\]", s)
        if match and predicate(s):
            return match.group(1), s
    return None, None


def wait_snapshot(predicate, retries=8, sleep_seconds=2, limit=500):
    snap = ""
    for _ in range(retries):
        snap = browser_snapshot(limit=limit)
        if predicate(snap):
            return snap
        time.sleep(sleep_seconds)
    return snap


def set_trip_type(is_one_way):
    snap = browser_snapshot(limit=300)
    opener_ref, _ = find_ref_matching(
        snap,
        lambda s: ("one way" in s.lower() or "round trip" in s.lower()) and ("button" in s.lower() or "combobox" in s.lower()),
    )
    if opener_ref:
        browser_click(opener_ref)
        time.sleep(1)
        snap = browser_snapshot(limit=350)
    target = "One way" if is_one_way else "Round trip"
    option_ref, _ = find_ref_matching(
        snap,
        lambda s: target.lower() in s.lower() and ("menuitem" in s.lower() or "option" in s.lower() or "radio" in s.lower() or "link" in s.lower()),
    )
    if not option_ref:
        option_ref, _ = find_ref_matching(snap, lambda s: target.lower() in s.lower())
    if option_ref:
        browser_click(option_ref)
        time.sleep(1)
        return True
    return False


def find_form_refs(snap):
    refs = {}
    for line in snap.split("\n"):
        s = line.strip()
        m = re.search(r"\[ref=(\w+)\]", s)
        if not m:
            continue
        ref = m.group(1)
        lower = s.lower()
        if "where from?" in lower or "leaving from" in lower or "from" in lower and "combobox" in lower:
            refs.setdefault("from", ref)
        elif "where to?" in lower or "to" in lower and "combobox" in lower:
            refs.setdefault("to", ref)
        elif '"departure"' in lower or ("date" in lower and "button" in lower):
            refs.setdefault("depart", ref)
        elif '"return"' in lower:
            refs.setdefault("return", ref)
        elif '"search"' in lower and "button" in lower:
            refs.setdefault("search", ref)
    return refs


def select_airport(combobox_ref, code):
    browser_click(combobox_ref)
    time.sleep(1)
    snap = browser_snapshot(limit=350)
    active_ref = combobox_ref
    for line in snap.split("\n"):
        s = line.strip()
        if "[expanded]" in s and ("combobox" in s.lower() or "textbox" in s.lower()):
            m = re.search(r"\[ref=(\w+)\]", s)
            if m:
                active_ref = m.group(1)
                break
    try:
        browser_fill(active_ref, "")
        time.sleep(0.3)
    except Exception:
        pass
    typed = False
    try:
        browser_type(active_ref, code)
        typed = True
    except Exception:
        typed = False
    if not typed:
        typed = cdp_type_text(code)
    time.sleep(1.5)

    if cdp_key_press("ArrowDown"):
        time.sleep(0.25)
        if cdp_key_press("Enter"):
            time.sleep(1)
            return True

    snap = browser_snapshot(limit=450)
    code_re = re.compile(rf"\b{re.escape(code.upper())}\b")
    for line in snap.split("\n"):
        s = line.strip()
        if "option" in s.lower() and code_re.search(s.upper()):
            m = re.search(r"\[ref=(\w+)\]", s)
            if m:
                browser_click(m.group(1))
                time.sleep(1)
                return True
    for line in snap.split("\n"):
        s = line.strip()
        lower = s.lower()
        if "option" not in lower:
            continue
        m = re.search(r"\[ref=(\w+)\]", s)
        if m:
            browser_click(m.group(1))
            time.sleep(1)
            return True
    if cdp_key_press("Enter"):
            time.sleep(1)
            return True
    return False


def click_calendar_date(snap, iso_date):
    target = datetime.strptime(iso_date, "%Y-%m-%d")
    day_num = str(target.day)
    month_prefix = target.strftime("%b").lower()
    year_text = str(target.year)
    for line in snap.split("\n"):
        s = line.strip()
        sl = s.lower()
        if "button" not in sl:
            continue
        if month_prefix not in sl or year_text not in sl:
            continue
        if re.search(rf"\b{day_num}\b", s):
            m = re.search(r"\[ref=(\w+)\]", s)
            if m:
                browser_click(m.group(1))
                time.sleep(1)
                return True
    return False


def type_modal_date(iso_date):
    target = datetime.strptime(iso_date, "%Y-%m-%d")
    short_value = f"{target.month}/{target.day}"
    focused = cdp_eval(
        """
(() => {
  const el = document.activeElement;
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea';
})()
"""
    )
    if not focused:
        return False
    cleared = cdp_eval(
        """
(() => {
  const el = document.activeElement;
  if (!el) return false;
  el.focus();
  if ('value' in el) {
    el.value = '';
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  return true;
})()
"""
    )
    if not cleared:
        return False
    if not cdp_type_text(short_value):
        return False
    time.sleep(0.3)
    cdp_key_press("Enter")
    time.sleep(0.7)
    return True


def extract_flights(snap):
    flights = []
    for line in snap.split("\n"):
        s = line.strip()
        if 'link "From' not in s and 'link "from' not in s.lower():
            continue
        if "Select flight" not in s:
            continue
        ref_match = re.search(r"\[ref=(\w+)\]", s)
        if not ref_match:
            continue
        price_match = re.search(r"(\d+) US dollars", s)
        depart_match = re.search(r"at (\d+:\d+ [AP]M) on", s)
        arrive_match = re.search(r"arrives at .+ at (\d+:\d+ [AP]M)", s)
        duration_match = re.search(r"Total duration (\d+ hr \d+ min)", s)
        if "Nonstop" in s:
            stops = "Nonstop"
        else:
            stop_match = re.search(r"(\d+ stop)", s)
            stops = stop_match.group(1) if stop_match else "?"
        airline = "?"
        for candidate in ["United", "Delta", "American", "Southwest", "JetBlue", "Alaska", "Spirit", "Frontier", "Air France", "Lufthansa"]:
            if candidate in s:
                airline = candidate
                break
        flights.append({
            "ref": ref_match.group(1),
            "price": f"${price_match.group(1)}" if price_match else "?",
            "airline": airline,
            "depart_time": depart_match.group(1) if depart_match else "?",
            "arrive_time": arrive_match.group(1) if arrive_match else "?",
            "duration": duration_match.group(1) if duration_match else "?",
            "stops": stops,
            "summary": s,
        })
    deduped = []
    seen = set()
    for flight in flights:
        key = (flight["airline"], flight["depart_time"], flight["arrive_time"], flight["price"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(flight)
    return deduped


def format_results(search_request, flights):
    lines = [f"✈️ {search_request['origin']} → {search_request['destination']} ({search_request['depart_date']}"]
    if search_request.get("return_date"):
        lines[0] += f" → {search_request['return_date']}"
    lines[0] += ")"
    if not flights:
        lines.append("No flights found.")
        return "\n".join(lines)
    for index, flight in enumerate(flights[:10], 1):
        lines.append(f"\n{index}. {flight['airline']} — {flight['price']}")
        lines.append(f"   {flight['depart_time']} → {flight['arrive_time']} · {flight['duration']} · {flight['stops']}")
    lines.append("\nReply with a number to open that Google Flights option.")
    return "\n".join(lines)


def search_google_flights(search_request):
    browser_navigate("https://www.google.com/travel/flights?hl=en-US&gl=US")
    time.sleep(4)
    set_trip_type(not bool(search_request.get("return_date")))
    snap = wait_snapshot(lambda s: bool(find_form_refs(s).get("from") and find_form_refs(s).get("to")), retries=6, sleep_seconds=2)
    refs = find_form_refs(snap)
    if not refs.get("from") or not refs.get("to"):
        raise RuntimeError("Google Flights form did not load correctly.")
    if not select_airport(refs["from"], search_request["origin"]):
        raise RuntimeError(f"Could not select origin airport {search_request['origin']}.")
    snap = wait_snapshot(lambda s: bool(find_form_refs(s).get("to")), retries=5, sleep_seconds=1)
    refs = find_form_refs(snap)
    if not select_airport(refs["to"], search_request["destination"]):
        raise RuntimeError(f"Could not select destination airport {search_request['destination']}.")
    snap = browser_snapshot(limit=400)
    refs = find_form_refs(snap)
    if not refs.get("depart"):
        raise RuntimeError("Could not find departure date control.")
    browser_click(refs["depart"])
    time.sleep(1)
    snap = browser_snapshot(limit=500)
    if not type_modal_date(search_request["depart_date"]) and not click_calendar_date(snap, search_request["depart_date"]):
        raise RuntimeError("Could not select departure date.")
    if search_request.get("return_date"):
        snap = browser_snapshot(limit=500)
        if not type_modal_date(search_request["return_date"]) and not click_calendar_date(snap, search_request["return_date"]):
            raise RuntimeError("Could not select return date.")
    time.sleep(1)
    snap = browser_snapshot(limit=400)
    refs = find_form_refs(snap)
    if refs.get("search"):
        browser_click(refs["search"])
    else:
        search_ref, _ = find_ref_matching(snap, lambda s: '"search"' in s.lower())
        if not search_ref:
            raise RuntimeError("Could not find Google Flights search button.")
        browser_click(search_ref)
    snap = wait_snapshot(lambda s: len(extract_flights(s)) > 0, retries=12, sleep_seconds=3, limit=900)
    return extract_flights(snap)


def handle_selection(chat_id, text):
    match = re.fullmatch(r"(\d{1,2})", text.strip())
    if not match:
        return False
    state = FLIGHT_RESULTS.get(chat_id)
    if not state:
        tg_send(chat_id, "⚠️ I don't have an active Google Flights list right now. Run the search again first.")
        return True
    idx = int(match.group(1)) - 1
    flights = state.get("flights", [])
    if idx < 0 or idx >= len(flights):
        tg_send(chat_id, f"⚠️ Pick a number from 1 to {len(flights)}.")
        return True
    flight = flights[idx]
    try:
        browser_click(flight["ref"])
    except Exception:
        browser_cmd("evaluate", "--ref", flight["ref"], "--fn", "(el) => { el.scrollIntoView(); el.click(); return 'clicked'; }", timeout=15)
    tg_send(chat_id, f"✈️ Opening Google Flights option {idx + 1}: {flight['airline']} {flight['depart_time']} → {flight['arrive_time']} {flight['price']}")
    return True


def handle_fly(chat_id, args):
    search_request = parse_search_message(args)
    tg_send(chat_id, f"🔍 Searching Google Flights for {search_request['origin']} → {search_request['destination']} ({search_request['depart_date']}" + (f" → {search_request['return_date']})" if search_request.get('return_date') else ")"))
    flights = search_google_flights(search_request)
    FLIGHT_RESULTS[chat_id] = {"search_request": search_request, "flights": flights}
    tg_send(chat_id, format_results(search_request, flights))


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
        if handle_selection(chat_id, text):
            return
        if lowered in {"/start", "/help", "help", "hi", "hello", "start"}:
            tg_send(chat_id, HELP_TEXT)
            return
        if lowered.startswith("/fly"):
            handle_fly(chat_id, text[4:].strip())
            return
        if re.match(r"^[A-Za-z]{3}\s+to\s+[A-Za-z]{3}\s+.+$", text, flags=re.IGNORECASE):
            handle_fly(chat_id, text)
            return
        tg_send(chat_id, HELP_TEXT)
    except Exception as exc:
        tg_send(chat_id, f"❌ Error: {exc}")


def main():
    load_env()
    load_gateway()
    global BROWSER_PROFILE
    BROWSER_PROFILE = resolve_browser_profile()
    if not BOT_TOKEN:
        print("ERROR: Set GOOGLE_FLIGHTS_BOT_TOKEN or FLIGHT_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)
    print("✈️ Google Flights Telegram Bot starting...")
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
            print("\n✈️ Google Flights Telegram Bot stopped.")
            break
        except requests.exceptions.Timeout:
            continue
        except Exception as exc:
            print(f"Error: {exc}")
            time.sleep(2)


if __name__ == "__main__":
    main()
