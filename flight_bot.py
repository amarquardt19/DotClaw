#!/usr/bin/env python3
"""
Telegram Flight Bot — bridges your Telegram messages to the OpenClaw browser.

You text commands from your phone, this bot executes them in the browser
and replies with what it sees.

Commands:
  /go <url>          - Navigate to a URL
  /snap              - Take a snapshot and describe the page
  /click <ref>       - Click an element by ref
  /type <ref> <text> - Type text into a field
  /fill <ref> <val>  - Fill a field value
  /screenshot        - Send a screenshot image
  /search            - Search Google Flights (interactive)
  /book              - Trigger Apple Pay approval
  /pay               - Fill payment form with stored card
  /help              - Show commands

Or just type naturally — the bot interprets common phrases.

Usage:
  export FLIGHT_BOT_TOKEN="your-bot-token-from-botfather"
  python3 flight_bot.py
"""

import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
import websocket
from dotenv import load_dotenv
# fast_flights removed — using Expedia deep links for search + booking

# ── Flight search state (per chat) ──────────────────────────────────────────
FLIGHT_RESULTS = {}   # chat_id -> {"flights": [...], "filter": TFSData, "origin": str, "dest": str, "phase": str}

# ── Config ──────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.environ.get("FLIGHT_BOT_TOKEN", "")
ALLOWED_CHAT_ID = int((os.environ.get("FLIGHT_BOT_CHAT_ID") or "0").strip())

# Gateway creds from OpenClaw config
def _load_gateway():
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if not cfg_path.exists():
        return "", ""
    cfg = json.loads(cfg_path.read_text())
    token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
    port = cfg.get("gateway", {}).get("port", 18789)
    return token, f"ws://127.0.0.1:{port}"

GW_TOKEN, GW_URL = _load_gateway()

BOOK_PY = str(Path(__file__).resolve().parent / "secure_checkout" / "book.py")
FILL_PY = str(Path(__file__).resolve().parent / "secure_checkout" / "fill_checkout.py")

# ── Telegram helpers ────────────────────────────────────────────────────────

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET = 0


def tg_get_updates(timeout=30):
    global OFFSET
    resp = requests.get(f"{API}/getUpdates", params={
        "offset": OFFSET, "timeout": timeout, "allowed_updates": '["message"]'
    }, timeout=timeout + 5)
    data = resp.json()
    if not data.get("ok"):
        return []
    results = data.get("result", [])
    if results:
        OFFSET = results[-1]["update_id"] + 1
    return results


def tg_send(chat_id, text, parse_mode=None):
    """Send a text message, splitting if > 4096 chars."""
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        params = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            params["parse_mode"] = parse_mode
        requests.post(f"{API}/sendMessage", json=params, timeout=15)


def tg_send_photo(chat_id, photo_path, caption=""):
    with open(photo_path, "rb") as f:
        requests.post(f"{API}/sendPhoto",
                      data={"chat_id": chat_id, "caption": caption[:1024]},
                      files={"photo": f}, timeout=30)


# ── Browser helpers ─────────────────────────────────────────────────────────

BROWSER_PROFILE = "openclaw"  # OpenClaw's managed browser
BROWSER_CDP_PORT = 18800      # CDP port for the openclaw profile

def browser_cmd(*args, timeout=60, retries=1):
    """Run an openclaw browser command and return stdout."""
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
                raise RuntimeError(f"browser {args[0]} failed: {err}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            last_err = f"subprocess timeout after {timeout}s"
            if attempt < retries - 1:
                time.sleep(2)
                continue
            raise RuntimeError(f"browser {args[0]} failed: {last_err}")
    raise RuntimeError(f"browser {args[0]} failed after {retries} retries: {last_err}")


def browser_navigate(url):
    if not _ensure_browser_page():
        raise RuntimeError("browser navigate failed: Error: No pages available in the connected browser")
    try:
        return browser_cmd("navigate", url)
    except RuntimeError as err:
        msg = str(err).lower()
        if "frame was detached" not in msg and "no frame with given id found" not in msg:
            raise
        time.sleep(1)
        if not _ensure_browser_page():
            raise
        return browser_cmd("navigate", url)


def browser_snapshot(limit=200):
    raw = browser_cmd("snapshot", "--labels", "--limit", str(limit), timeout=90, retries=2)
    # Find the aria tree start — skip doctor warnings and other noise
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("- RootWebArea") or s.startswith("- root"):
            return "\n".join(lines[i:])
    # Fallback: skip lines that look like doctor warnings (box-drawing chars)
    clean = []
    for line in lines:
        s = line.strip()
        if any(c in s for c in '│├╮╯◇─') or 'Doctor warnings' in s or s == '':
            continue
        clean.append(line)
    return "\n".join(clean)


def browser_click(ref):
    return browser_cmd("click", ref)


def browser_scrollintoview(ref):
    return browser_cmd("scrollintoview", ref)


def browser_evaluate(ref, fn):
    """Run JavaScript against an element ref."""
    args = ["evaluate", "--ref", ref, "--fn", fn]
    return browser_cmd(*args, timeout=15)


def browser_js_click(ref):
    """Click an element via JS — works when browser_click fails on hidden elements."""
    try:
        browser_scrollintoview(ref)
    except RuntimeError:
        pass
    return browser_evaluate(ref, "(el) => { el.scrollIntoView(); el.click(); return 'clicked'; }")


def browser_focus(target_id):
    return browser_cmd("focus", target_id)


def _list_tabs():
    """List browser tabs via CDP HTTP."""
    import urllib.request
    try:
        data = urllib.request.urlopen(f"http://127.0.0.1:{BROWSER_CDP_PORT}/json/list", timeout=5).read()
        tabs = json.loads(data)
        return [t for t in tabs if t.get("type") == "page"]
    except Exception:
        return []


def _ensure_browser_page(wait=5):
    end_time = time.time() + wait
    while time.time() < end_time:
        tabs = _list_tabs()
        if tabs:
            try:
                browser_focus(tabs[0]["id"])
            except Exception:
                pass
            return True
        time.sleep(1)
    return False


def _close_extra_tabs():
    """Close all tabs except the first one to prevent browser slowdown."""
    import urllib.request
    tabs = _list_tabs()
    if len(tabs) <= 1:
        return
    kept = False
    for t in tabs:
        if not kept:
            kept = True
            browser_focus(t["id"])
            continue
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{BROWSER_CDP_PORT}/json/close/{t['id']}", timeout=3)
        except Exception:
            pass


def _cdp_eval(js_code, timeout=10):
    """Evaluate JS directly via Chrome DevTools Protocol — bypasses openclaw gateway.
    Works even on heavy pages where browser_snapshot times out."""
    try:
        tabs = _list_tabs()
        if not tabs:
            return "error: no browser tabs found"
        # Pick the first non-blank tab, prefer checkout/expedia tabs
        target = tabs[0]
        for t in tabs:
            url = t.get("url", "")
            if "checkout" in url.lower() or "payment" in url.lower():
                target = t
                break
        ws_url = target.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return "error: no webSocketDebuggerUrl"
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js_code,
                "returnByValue": True,
                "awaitPromise": False,
            }
        }))
        raw = ws.recv()
        ws.close()
        resp = json.loads(raw)
        result = resp.get("result", {}).get("result", {})
        if result.get("type") == "string":
            return result["value"]
        elif result.get("type") == "undefined":
            return "undefined"
        elif "value" in result:
            return str(result["value"])
        return json.dumps(result)
    except Exception as e:
        return f"error: {e}"


def _switch_to_new_tab(keyword="delta", wait=8):
    """Wait for a new tab matching keyword and switch to it. Returns target_id or None."""
    for _ in range(wait):
        tabs = _list_tabs()
        for t in tabs:
            if keyword.lower() in t.get("title", "").lower() or keyword.lower() in t.get("url", "").lower():
                browser_focus(t["id"])
                return t["id"]
        time.sleep(1)
    return None


def _handle_delta_dialog():
    """Handle Delta's 'Continue with Main Basic?' restrictions dialog."""
    snap = browser_snapshot(limit=300)
    # Look for Accept Restrictions checkbox
    for line in snap.split("\n"):
        s = line.strip()
        if "Accept Restrictions" in s and "checkbox" in s.lower():
            m = re.search(r'\[ref=(\w+)\]', s)
            if m:
                browser_click(m.group(1))
                time.sleep(1)
                break
    # Click "Select Main Basic/Classic" button
    snap2 = browser_snapshot(limit=300)
    for line in snap2.split("\n"):
        s = line.strip()
        if 'button "Select Main' in s:
            m = re.search(r'\[ref=(\w+)\]', s)
            if m:
                browser_click(m.group(1))
                time.sleep(4)
                return True
    return False


def browser_type(ref, text):
    return browser_cmd("type", ref, text)


def browser_fill(ref, value):
    fields_json = json.dumps([{"ref": ref, "value": value}])
    return browser_cmd("fill", "--fields", fields_json)


def browser_screenshot():
    raw = browser_cmd("screenshot", "--json", timeout=15)
    # Find path in output
    for line in raw.split("\n"):
        if ".png" in line or ".jpg" in line:
            # Extract path
            m = re.search(r'(/.+\.(?:png|jpg))', line)
            if m:
                return m.group(1)
    # Try JSON parse
    try:
        data = json.loads(raw)
        return data.get("path", data.get("file", ""))
    except:
        pass
    # Check MEDIA line
    m = re.search(r'MEDIA:(.+\.png)', raw)
    if m:
        return m.group(1)
    return ""


def summarize_snapshot(snap, max_lines=40):
    """Extract the most useful info from a snapshot for Telegram."""
    lines = snap.split("\n")
    useful = []
    for line in lines:
        s = line.strip()
        # Skip deeply nested layout noise
        if s.startswith("- none") or s.startswith("- generic") or s.startswith("- banner"):
            continue
        if any(kw in s.lower() for kw in [
            "button", "link", "textbox", "combobox", "heading", "option",
            "text:", "tab ", "gridcell", "search", "$", "dollar",
            "flight", "delta", "united", "american", "southwest",
            "depart", "return", "nonstop", "stop", "price",
            "checkout", "payment", "card", "book", "select",
        ]):
            useful.append(s)
        if len(useful) >= max_lines:
            break
    if not useful:
        # Fallback: first N non-empty lines
        useful = [l.strip() for l in lines if l.strip()][:max_lines]
    return "\n".join(useful)


# ── Command handlers ────────────────────────────────────────────────────────

def handle_go(chat_id, args):
    url = args.strip()
    if not url.startswith("http"):
        url = "https://" + url
    browser_navigate(url)
    time.sleep(2)
    snap = browser_snapshot()
    summary = summarize_snapshot(snap)
    tg_send(chat_id, f"✅ Navigated to {url}\n\nPage content:\n{summary}")


def handle_snap(chat_id, args):
    snap = browser_snapshot()
    summary = summarize_snapshot(snap, max_lines=60)
    tg_send(chat_id, f"📋 Page snapshot:\n\n{summary}")


def handle_click(chat_id, args):
    ref = args.strip()
    if not ref:
        tg_send(chat_id, "Usage: /click <ref>  (e.g. /click e67)")
        return
    try:
        browser_click(ref)
    except RuntimeError as e:
        if "not found" in str(e).lower() or "not visible" in str(e).lower():
            # Try JS click as fallback (handles elements below the fold)
            try:
                browser_js_click(ref)
            except RuntimeError:
                tg_send(chat_id, f"⚠️ Ref {ref} failed. Taking fresh snapshot...")
                snap = browser_snapshot(limit=500)
                booking = _parse_booking_page(snap)
                if booking:
                    tg_send(chat_id, booking)
                else:
                    flights = _extract_flights(snap)
                    if flights:
                        msg = "Current flights:\n\n"
                        for i, f in enumerate(flights, 1):
                            msg += f"{i}. {f['summary']}\n   💰 {f['price']}  |  /click {f['ref']}\n\n"
                        tg_send(chat_id, msg)
                    else:
                        summary = summarize_snapshot(snap, max_lines=30)
                        tg_send(chat_id, f"📋 Current page:\n\n{summary}")
                return
        else:
            raise
    time.sleep(1.5)
    snap = browser_snapshot()
    summary = summarize_snapshot(snap, max_lines=30)
    tg_send(chat_id, f"✅ Clicked ref {ref}\n\nPage now shows:\n{summary}")


def handle_type(chat_id, args):
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        tg_send(chat_id, "Usage: /type <ref> <text>")
        return
    ref, text = parts
    browser_type(ref, text)
    time.sleep(1.5)
    snap = browser_snapshot()
    summary = summarize_snapshot(snap, max_lines=20)
    tg_send(chat_id, f"✅ Typed '{text}' into ref {ref}\n\n{summary}")


def handle_fill(chat_id, args):
    parts = args.strip().split(None, 1)
    if len(parts) < 2:
        tg_send(chat_id, "Usage: /fill <ref> <value>")
        return
    ref, value = parts
    browser_fill(ref, value)
    tg_send(chat_id, f"✅ Filled ref {ref}")


def handle_cvv(chat_id, args):
    """Fill CVV on Delta checkout and offer to complete purchase."""
    cvv = args.strip()
    if not cvv or not cvv.isdigit() or len(cvv) not in (3, 4):
        tg_send(chat_id, "🔒 Enter your CVV:\n/cvv 123")
        return
    tg_send(chat_id, "🔒 Filling CVV...")
    # Use direct JS to fill CVV — bypasses heavy snapshot entirely
    try:
        result = browser_evaluate("e1", f"""(el) => {{
            // Try multiple selectors for the CVV field
            const selectors = [
                '#id_paymentCardSecurityCode_wallet',
                'input[name="paymentCardSecurityCode"]',
                'input[placeholder="CVV"]',
                'input[type="password"][placeholder*="CVV"]'
            ];
            let field = null;
            for (const sel of selectors) {{
                field = document.querySelector(sel);
                if (field) break;
            }}
            if (!field) return 'not found';
            // Focus, clear, set value, and trigger events
            field.scrollIntoView({{block: 'center'}});
            field.focus();
            field.value = '';
            // Use native input setter to bypass Angular/React controls
            const nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeSet.call(field, '{cvv}');
            field.dispatchEvent(new Event('input', {{bubbles: true}}));
            field.dispatchEvent(new Event('change', {{bubbles: true}}));
            field.dispatchEvent(new Event('blur', {{bubbles: true}}));
            return 'filled: ' + field.value.length + ' digits';
        }}""")
        if "filled" in result:
            tg_send(chat_id, f"✅ CVV filled!\n\n💳 Say 'complete purchase' when ready.")
        elif "not found" in result:
            tg_send(chat_id, "⚠️ CVV field not found. Make sure card payment is selected.\nTry /snap to check.")
        else:
            tg_send(chat_id, f"⚠️ Unexpected result: {result}")
    except RuntimeError as e:
        tg_send(chat_id, f"⚠️ Failed to fill CVV: {e}")


def handle_reload(chat_id, args):
    """Reload the current page and wait for it to load."""
    tg_send(chat_id, "🔄 Reloading page...")
    try:
        browser_evaluate("e1", "(el) => { location.reload(); return 'reloaded'; }")
    except RuntimeError:
        tg_send(chat_id, "⚠️ Reload via JS failed, trying navigate...")
        try:
            snap = browser_snapshot(limit=50)
            url_match = re.search(r'/url:\s*(https?://\S+)', snap)
            if url_match:
                browser_navigate(url_match.group(1))
        except RuntimeError:
            tg_send(chat_id, "⚠️ Could not reload. Try again.")
            return
    tg_send(chat_id, "⏳ Waiting for page to load (up to 60s)...")
    snap = ""
    for i in range(15):
        time.sleep(4)
        try:
            snap = browser_snapshot(limit=300)
        except RuntimeError:
            continue  # Timeout during load, keep trying
        if any(x in snap for x in ["Loading Trip Details", "Loading Passenger", "Loading Payment"]):
            if i == 5:
                tg_send(chat_id, "⏳ Still loading...")
            continue
        break
    summary = summarize_snapshot(snap, max_lines=50)
    tg_send(chat_id, f"✅ Page reloaded:\n\n{summary}")
    if "CVV" in snap:
        tg_send(chat_id, "\n🔒 CVV field detected! Use /cvv <code> to fill it.")


def handle_wait(chat_id, args):
    """Wait for page to finish loading, then show snapshot."""
    tg_send(chat_id, "⏳ Waiting for page to finish loading (up to 60s)...")
    snap = ""
    for i in range(15):
        time.sleep(4)
        try:
            snap = browser_snapshot(limit=300)
        except RuntimeError:
            continue  # Timeout during load, keep trying
        if any(x in snap for x in ["Loading Trip Details", "Loading Passenger", "Loading Payment"]):
            if i == 5:
                tg_send(chat_id, "⏳ Still loading...")
            continue
        break
    if not snap:
        tg_send(chat_id, "⚠️ Page still loading or gateway timeout. Try /reload.")
        return
    summary = summarize_snapshot(snap, max_lines=50)
    tg_send(chat_id, f"📋 Page loaded:\n\n{summary}")
    if "CVV" in snap:
        tg_send(chat_id, "\n🔒 CVV field detected! Use /cvv <code> to fill it.")


def handle_screenshot(chat_id, args):
    tg_send(chat_id, "�📸 Taking screenshot...")
    path = browser_screenshot()
    if path and Path(path).exists():
        tg_send_photo(chat_id, path, "Browser screenshot")
    else:
        tg_send(chat_id, f"⚠️ Could not get screenshot. Raw: {path}")


def _find_refs(snap):
    """Parse snapshot to find Google Flights form refs."""
    refs = {}
    for line in snap.split("\n"):
        s = line.strip()
        m = re.search(r'\[ref=(\w+)\]', s)
        if not m:
            continue
        ref = m.group(1)
        if "Where from?" in s or "Leaving from" in s:
            refs["from"] = ref
        elif "Where to?" in s:
            refs["to"] = ref
        elif '"Departure"' in s:
            refs["depart"] = ref
        elif '"Return"' in s:
            refs["return"] = ref
        elif '"Search"' in s and 'button' in s.lower():
            refs["search"] = ref
    return refs


def _wait_and_find_refs(max_retries=3):
    """Snapshot with retries until we find form refs."""
    for i in range(max_retries):
        snap = browser_snapshot()
        refs = _find_refs(snap)
        if refs.get("from") or refs.get("to"):
            return snap, refs
        time.sleep(2)
    return snap, refs


def _select_airport(combobox_ref, code):
    """Click a combobox, type an airport code, wait for dropdown, select first match."""
    browser_click(combobox_ref)
    time.sleep(1.5)

    # After clicking, the DOM changes — snapshot to find the NEW active combobox
    snap = browser_snapshot()
    active_ref = None
    for line in snap.split("\n"):
        s = line.strip()
        if "[expanded]" in s and "combobox" in s.lower():
            m = re.search(r'\[ref=(\w+)\]', s)
            if m:
                active_ref = m.group(1)
                break
    if not active_ref:
        active_ref = combobox_ref

    # Clear any stale airport text before typing the new code.
    try:
        browser_fill(active_ref, "")
        time.sleep(0.5)
    except RuntimeError:
        pass

    try:
        browser_type(active_ref, code)
    except RuntimeError:
        # Ref went stale — re-snapshot and try again
        time.sleep(1)
        snap = browser_snapshot()
        for line in snap.split("\n"):
            s = line.strip()
            if ("combobox" in s.lower() or "textbox" in s.lower()) and "[expanded]" in s:
                m = re.search(r'\[ref=(\w+)\]', s)
                if m:
                    active_ref = m.group(1)
                    break
        try:
            browser_fill(active_ref, "")
            time.sleep(0.5)
        except RuntimeError:
            pass
        browser_type(active_ref, code)

    time.sleep(2)

    # Snapshot to find the matching airport option
    snap = browser_snapshot()
    code_re = re.compile(rf'\b{re.escape(code.upper())}\b')

    # First try: exact airport-code match
    for line in snap.split("\n"):
        s = line.strip()
        if "option" in s.lower() and code_re.search(s.upper()):
            m = re.search(r'\[ref=(\w+)\]', s)
            if m:
                browser_click(m.group(1))
                time.sleep(1)
                return True

    # Second try: airport option containing the code
    for line in snap.split("\n"):
        s = line.strip()
        if "option" in s.lower() and code.upper() in s.upper() and "airport" in s.lower():
            m = re.search(r'\[ref=(\w+)\]', s)
            if m:
                browser_click(m.group(1))
                time.sleep(1)
                return True

    # Third try: match any option with the code
    for line in snap.split("\n"):
        s = line.strip()
        if "option" in s.lower() and code.upper() in s.upper():
            m = re.search(r'\[ref=(\w+)\]', s)
            if m:
                browser_click(m.group(1))
                time.sleep(1)
                return True
    return False


def _click_calendar_date(snap, date_str):
    """Find and click a date button in the Google Flights calendar.
    
    date_str is like "Mar 13" or "March 16".
    Calendar buttons look like: button "Friday, March 13, 2026 , 339 US dollars" [ref=e8]
    """
    # Extract day number from date_str
    day_match = re.search(r'(\d+)', date_str)
    if not day_match:
        return False
    day_num = day_match.group(1)
    
    # Extract month from date_str
    month_match = re.search(r'([A-Za-z]+)', date_str)
    month_prefix = month_match.group(1).lower()[:3] if month_match else ""
    
    for line in snap.split("\n"):
        s = line.strip()
        sl = s.lower()
        # Match calendar buttons: must have "button", the month, and the exact day number
        if "button" not in sl:
            continue
        # Check month (e.g. "march" contains "mar")
        if month_prefix and month_prefix not in sl:
            continue
        # Check for exact day number — look for patterns like "March 13," or ": 13 $"
        if re.search(rf'\b{day_num}\b', s):
            m = re.search(r'\[ref=(\w+)\]', s)
            if m:
                browser_click(m.group(1))
                time.sleep(1)
                return True
    return False


def handle_search(chat_id, args):
    """Open Google Flights and show the form."""
    tg_send(chat_id, "🔍 Opening Google Flights...")
    browser_navigate("https://www.google.com/travel/flights")
    time.sleep(4)

    snap, refs = _wait_and_find_refs()
    tg_send(chat_id,
            f"✅ Google Flights loaded!\n\n"
            f"Use /fly to search, e.g.:\n"
            f"/fly SFO SLC Mar 13 Mar 16")


DAY_NAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6,
             "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}



def _parse_date(text):
    """Parse flexible date text into YYYY-MM-DD.
    Handles: 'Apr 15', 'April 15', '4/15', '2026-04-15',
             'this thursday', 'next monday', 'following friday',
             'tomorrow', 'today'."""
    text = text.strip().lower()
    from datetime import timedelta
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # "today" / "tomorrow"
    if text == "today":
        return today.strftime("%Y-%m-%d")
    if text == "tomorrow":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")

    # "this thursday", "next monday", "following friday", just "thursday"
    for day_name, day_num in DAY_NAMES.items():
        if day_name in text:
            days_ahead = day_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = today + timedelta(days=days_ahead)
            return target.strftime("%Y-%m-%d")

    # Try ISO format
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass
    # Try "Apr 15" / "April 15"
    for fmt in ["%b %d", "%B %d"]:
        try:
            d = datetime.strptime(text, fmt).replace(year=now.year)
            if d < now:
                d = d.replace(year=now.year + 1)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Try "4/15"
    try:
        d = datetime.strptime(text, "%m/%d").replace(year=now.year)
        if d < now:
            d = d.replace(year=now.year + 1)
        return d.strftime("%Y-%m-%d")
    except ValueError:
        pass
    return None


def _parse_expedia_flights(snap):
    """Parse flight results from an Expedia search results snapshot.
    Handles both outbound ('priced at $375') and return ('total $542') formats."""
    flights = []
    seen_refs = set()
    for line in snap.split("\n"):
        s = line.strip()
        if 'select' not in s.lower() or 'flight' not in s.lower() or 'departing' not in s.lower():
            continue
        ref_m = re.search(r'\[ref=(\w+)\]', s)
        if not ref_m:
            continue
        ref = ref_m.group(1)
        if ref in seen_refs:
            continue
        seen_refs.add(ref)

        airline = "?"
        for al in ["United", "Delta", "American Airlines", "American", "Southwest Airlines",
                    "Southwest", "JetBlue", "Alaska Airlines", "Alaska",
                    "Spirit", "Frontier", "Sun Country", "Hawaiian", "Allegiant", "Breeze"]:
            if al.lower() in s.lower():
                airline = al.replace(" Airlines", "")
                break

        dep_m = re.search(r'departing (?:at )?(\d{1,2}:\d{2}(?:am|pm))', s, re.IGNORECASE)
        arr_m = re.search(r'arriving (?:at )?(\d{1,2}:\d{2}(?:am|pm))', s, re.IGNORECASE)
        dep_time = dep_m.group(1) if dep_m else "?"
        arr_time = arr_m.group(1) if arr_m else "?"

        # Price: "priced at $375" (outbound) or "total $542" (return)
        price_m = re.search(r'[Pp]riced at \$(\d[\d,]*)', s)
        if not price_m:
            price_m = re.search(r'total \$(\d[\d,]*)', s)
        price = f"${price_m.group(1)}" if price_m else "?"

        # Additional price for return flights
        add_m = re.search(r'additional \$(\d[\d,]*)', s)
        additional = f"+${add_m.group(1)}" if add_m else ""

        if 'nonstop' in s.lower():
            stops = "Nonstop"
        elif 'two stop' in s.lower() or '2 stop' in s.lower():
            stops = "2 stops"
        elif 'one stop' in s.lower() or '1 stop' in s.lower():
            stops = "1 stop"
        else:
            stops = "?"

        dur_m = re.search(r'(\d+)\s*hours?\s*(\d+)\s*min', s, re.IGNORECASE)
        duration = f"{dur_m.group(1)}h {dur_m.group(2)}m" if dur_m else ""

        flights.append({
            "airline": airline, "dep_time": dep_time, "arr_time": arr_time,
            "duration": duration, "stops": stops, "price": price,
            "additional": additional, "ref": ref,
        })
    return flights


def _parse_expedia_fares(snap):
    """Parse fare options from Expedia's fare selection dialog.
    Matches buttons like: 'Select Basic Economy for $375' [ref=e85]"""
    fares = []
    for line in snap.split("\n"):
        s = line.strip()
        m = re.search(r'button "Select (.+?) for \$(\d[\d,]*)".*\[ref=(\w+)\]', s)
        if m:
            fares.append({
                "label": m.group(1),
                "price": f"${m.group(2)}",
                "ref": m.group(3),
            })
    return fares


def _build_expedia_url(state):
    """Build Expedia deep link URL from flight search state."""
    origin, dest = state["origin"], state["dest"]
    depart = state["depart_date"]
    ret = state.get("return_date")
    trip = "Roundtrip" if ret else "Oneway"
    end_date = ret if ret else depart
    return (f"https://www.expedia.com/go/flight/search/{trip}"
            f"/{depart}/{end_date}"
            f"?load=1&FromAirport={origin}&ToAirport={dest}"
            f"&FromTime=362&ToTime=362&NumAdult=1")


def _click_ref(ref):
    """Click a ref, trying normal click then JS click. Returns True on success."""
    try:
        browser_scrollintoview(ref)
        time.sleep(1)
    except RuntimeError:
        pass
    try:
        browser_click(ref)
        return True
    except RuntimeError:
        try:
            browser_js_click(ref)
            return True
        except RuntimeError:
            return False


def _find_flight_ref(snap, airline, dep_time):
    """Find a flight button ref matching the given airline and departure time."""
    target = dep_time.lower().replace(' ', '')
    best_ref = None
    for line in snap.split("\n"):
        s = line.strip()
        if 'select' not in s.lower() or 'flight' not in s.lower() or 'departing' not in s.lower():
            continue
        ref_m = re.search(r'\[ref=(\w+)\]', s)
        if not ref_m:
            continue
        dep_m = re.search(r'departing (?:at )?(\d{1,2}:\d{2}(?:am|pm))', s, re.IGNORECASE)
        if dep_m and dep_m.group(1).lower().replace(' ', '') == target:
            if airline.lower() != "?" and airline.lower() in s.lower():
                return ref_m.group(1)
            elif best_ref is None:
                best_ref = ref_m.group(1)
    return best_ref


def _find_fare_ref(snap, fare_label, fare_idx=0):
    """Find a fare button ref matching the label, or fallback to index."""
    fares = _parse_expedia_fares(snap)
    for ff in fares:
        if fare_label.lower() in ff["label"].lower():
            return ff["ref"]
    if fares and fare_idx < len(fares):
        return fares[fare_idx]["ref"]
    return None


def _dismiss_popups():
    """Dismiss Expedia popups using snapshot + click (proven reliable method).
    Handles: 'Continue Booking', 'No thanks', close dialog, X buttons."""
    dismiss_keywords = ['continue booking', 'continue checkout',
                        'no thanks', 'no, thanks', 'close dialog',
                        'got it', 'dismiss']
    try:
        snap = browser_snapshot(limit=500)
    except RuntimeError:
        return False
    for line in snap.split("\n"):
        lo = line.strip().lower()
        ref_m = re.search(r'\[ref=(\w+)\]', line)
        if not ref_m:
            continue
        # Match buttons with specific dismiss keywords ONLY
        if any(kw in lo for kw in dismiss_keywords):
            # Don't match "close, go back" — that's the fare panel, not a popup
            if 'go back' in lo:
                continue
            ref = ref_m.group(1)
            print(f"[DEBUG] Dismissing popup via click: {line.strip()[:80]}")
            _click_ref(ref)
            time.sleep(2)
            return True
    return False


def _try_fast_click(ref):
    """Try clicking a ref on the current page. Returns snapshot on success, None on fail."""
    try:
        browser_scrollintoview(ref)
        time.sleep(0.5)
    except RuntimeError:
        pass
    try:
        browser_click(ref)
        time.sleep(3)
        return browser_snapshot(limit=1000)
    except RuntimeError:
        try:
            browser_js_click(ref)
            time.sleep(3)
            return browser_snapshot(limit=1000)
        except RuntimeError:
            return None


def _classify_seat(col):
    """Classify seat column as aisle, middle, or window.
    Narrow-body (A-F): A/F=window, B/E=middle, C/D=aisle
    Wide-body (A-K): A/K=window, C/D/G/H=aisle, rest=middle"""
    col = col.upper()
    if col in ('C', 'D'):
        return 'aisle'
    if col in ('G', 'H'):
        return 'aisle'
    if col in ('A', 'F', 'K'):
        return 'window'
    if col in ('B', 'E', 'J'):
        return 'middle'
    return 'middle'


def _parse_seat_map(snap):
    """Parse available seats from Expedia seat map snapshot.
    Format: checkbox "Seat 17B, Aisle seat, $23" [ref=eXX]
    Unavailable: checkbox "Seat 7A, Unavailable" [ref=eXX] [disabled]
    Returns list of dicts: {row, col, type, ref, label, price}"""
    seats = []
    for line in snap.split("\n"):
        s = line.strip()
        lo = s.lower()
        # Must be a checkbox with a ref, and NOT disabled
        if 'checkbox' not in lo or '[ref=' not in s or '[disabled]' in lo:
            continue
        if 'unavailable' in lo:
            continue
        # Match: Seat {row}{col}, {type} seat, ${price}
        seat_m = re.search(r'Seat\s+(\d{1,2})([A-K]),\s*(\w+)\s+seat', s, re.IGNORECASE)
        if not seat_m:
            continue
        ref_m = re.search(r'\[ref=(\w+)\]', s)
        if not ref_m:
            continue
        row = int(seat_m.group(1))
        col = seat_m.group(2).upper()
        seat_type = seat_m.group(3).lower()  # 'aisle', 'window', 'middle'
        price_m = re.search(r'\$(\d+)', s)
        price = int(price_m.group(1)) if price_m else 0
        seats.append({
            "row": row, "col": col, "type": seat_type,
            "ref": ref_m.group(1), "label": f"{row}{col}", "price": price,
        })
    return seats


def _seat_rank_key(seat):
    col_rank = {
        'D': 0,
        'C': 1,
        'F': 2,
        'A': 3,
        'E': 4,
        'B': 5,
        'J': 6,
        'H': 7,
        'G': 8,
        'K': 9,
    }
    return (seat["price"], seat["row"], col_rank.get(seat["col"], 99), seat["label"])


def _select_best_seat(snap):
    """Find best available seat: front-most aisle, fallback to front-most middle.
    Sort by price ascending then row ascending (front of plane preferred).
    Returns (seat_dict, ref) or (None, None)."""
    seats = _parse_seat_map(snap)
    if not seats:
        return None, None
    print(f"[DEBUG] Found {len(seats)} available seats")
    aisle = sorted([s for s in seats if s["type"] == "aisle"], key=_seat_rank_key)
    middle = sorted([s for s in seats if s["type"] == "middle"], key=_seat_rank_key)
    window = sorted([s for s in seats if s["type"] == "window"], key=_seat_rank_key)
    print("[DEBUG] Top aisle candidates:", [f"{s['label']}(${s['price']})" for s in aisle[:5]])
    print("[DEBUG] Top middle candidates:", [f"{s['label']}(${s['price']})" for s in middle[:5]])
    print("[DEBUG] Top window candidates:", [f"{s['label']}(${s['price']})" for s in window[:5]])
    if aisle:
        best = aisle[0]
        print(f"[DEBUG] Best aisle seat: {best['label']} (row {best['row']}, ${best['price']})")
        return best, best["ref"]
    if middle:
        best = middle[0]
        print(f"[DEBUG] Best middle seat: {best['label']} (row {best['row']}, ${best['price']})")
        return best, best["ref"]
    if window:
        best = window[0]
        print(f"[DEBUG] Best window seat: {best['label']} (row {best['row']}, ${best['price']})")
        return best, best["ref"]
    # Last resort: any available seat, cheapest + front
    any_seat = sorted(seats, key=_seat_rank_key)
    best = any_seat[0]
    print(f"[DEBUG] Fallback seat: {best['label']} (row {best['row']}, ${best['price']})")
    return best, best["ref"]


def _js_click_button(text_match):
    """Click a button/link by text content using JS. Returns result string.
    Takes a fresh snapshot first to ensure refs are valid on the current tab."""
    # Fresh snapshot to populate refs on current tab
    snap = browser_snapshot(limit=500)
    # Grab any valid ref from the snapshot
    ref = "e1"
    for line in snap.split("\n"):
        m = re.search(r'\[ref=(\w+)\]', line)
        if m:
            ref = m.group(1)
            break
    try:
        result = browser_evaluate(
            ref,
            f"(el) => {{"
            f"  const candidates = [...document.querySelectorAll('a, button')];"
            f"  for (const c of candidates) {{"
            f"    const txt = (c.textContent || '').toLowerCase().trim();"
            f"    if ({text_match}) {{ c.scrollIntoView(); c.click(); return 'clicked: ' + txt; }}"
            f"  }}"
            f"  return 'not found';"
            f"}}"
        )
        return str(result)
    except RuntimeError as e:
        return f"error: {e}"


def _click_next_bags_direct():
    return str(_cdp_eval("""
    (() => {
        function isVisible(el) {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        }
        function hardClick(el) {
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
            const rect = el.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;
            const target = document.elementFromPoint(x, y) || el;
            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                target.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    clientX: x,
                    clientY: y
                }));
            }
            target.focus?.();
            if (typeof el.click === 'function') el.click();
            return true;
        }
        const els = [...document.querySelectorAll('button, a, [role="button"]')];
        for (const el of els) {
            const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            if (!txt || !isVisible(el)) continue;
            if (txt === 'next: bags' || txt === 'next bags') {
                hardClick(el);
                return 'clicked: ' + txt;
            }
        }
        const fallback = [...document.querySelectorAll('*')].find(el => {
            const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            return isVisible(el) && (txt === 'next: bags' || txt === 'next bags');
        });
        if (fallback) {
            hardClick(fallback);
            return 'clicked-fallback';
        }
        return 'not found';
    })()
    """))


def _transition_after_selected_seats(chat_id):
    bags_announced = False
    for attempt in range(10):
        state = str(_cdp_eval("""
        (() => {
            const txt = (document.body.innerText || '').toLowerCase();
            if (txt.includes('secure booking') || txt.includes("who's traveling") || txt.includes('traveler name') ||
                txt.includes('how would you like to pay') || txt.includes('billing address') || txt.includes('remember this card') ||
                txt.includes('complete booking') || txt.includes('review and book your trip')) return 'checkout';
            if (txt.includes('next flight') || txt.includes('next: checkout') || txt.includes('add your bags')) return 'bags';
            if (txt.includes('next: bags') || (txt.includes('next') && txt.includes('bags'))) return 'seats';
            return 'unknown';
        })()
        """)).strip().lower()
        print(f"[DEBUG] Post-seat driver state attempt {attempt+1}: {state}")

        if state == 'checkout':
            return 'checkout'

        if state == 'seats':
            result = _click_next_bags_direct()
            print(f"[DEBUG] Post-seat driver Next:Bags attempt {attempt+1}: {result}")
            time.sleep(3)
            continue

        if state == 'bags':
            if not bags_announced:
                tg_send(chat_id, "🧳 Bags step detected — continuing to checkout...")
                bags_announced = True

            next_flight = str(_cdp_eval("""
            (() => {
                function isVisible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                }
                const els = [...document.querySelectorAll('button, a, [role="button"]')];
                for (const el of els) {
                    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    if (isVisible(el) && (txt === 'next flight' || (txt.includes('next') && txt.includes('flight')))) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return 'clicked: ' + txt;
                    }
                }
                return 'not found';
            })()
            """))
            print(f"[DEBUG] Post-seat driver Next flight attempt {attempt+1}: {next_flight}")
            if 'clicked' in next_flight.lower():
                time.sleep(4)
                continue

            next_checkout = str(_cdp_eval("""
            (() => {
                function isVisible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                }
                const els = [...document.querySelectorAll('button, a, [role="button"]')];
                for (const el of els) {
                    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    if (isVisible(el) && (txt === 'next: checkout' || txt === 'next checkout' || (txt.includes('next') && txt.includes('checkout')))) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return 'clicked: ' + txt;
                    }
                }
                return 'not found';
            })()
            """))
            print(f"[DEBUG] Post-seat driver Next:Checkout attempt {attempt+1}: {next_checkout}")
            if 'clicked' in next_checkout.lower():
                time.sleep(4)
                continue

            time.sleep(2)
            continue

        time.sleep(2)

    tg_send(chat_id, "⚠️ Seat/bags flow did not reach checkout.")
    return 'unknown'


def _handle_seat_selection(chat_id):
    """Auto-select seats on Expedia checkout page.
    1. Click 'Next: Seats' to open the seat map
    2. Parse available seats from checkboxes
    3. Select highest-row aisle seat (or middle, or any)
    4. Click 'Next: Checkout' to advance through legs
    Handles both outbound and return legs."""

    FLIGHT_RESULTS[chat_id] = FLIGHT_RESULTS.get(chat_id, {})
    if not FLIGHT_RESULTS[chat_id].get("seat_selected"):
        FLIGHT_RESULTS[chat_id]["seat_selected"] = False

    initial_snap = browser_snapshot(limit=1000)
    initial_lo = initial_snap.lower()
    review_to_seats = 'review your trip' in initial_lo and 'next: seats' in initial_lo
    already_checkout = any(kw in initial_lo for kw in [
        'how would you like to pay', 'billing address', 'remember this card',
        'complete booking', 'review and book your trip', 'review your trip'
    ]) and not review_to_seats
    already_bags = 'add your bags' in initial_lo or ('bags' in initial_lo and ('next flight' in initial_lo or 'next: checkout' in initial_lo))
    already_seats = 'choose your seats' in initial_lo or 'next: bags' in initial_lo or 'seat key' in initial_lo

    if FLIGHT_RESULTS[chat_id].get("seat_selected") and already_bags:
        print("[DEBUG] Seat already selected for this run; treating bags page as post-seat flow")
        already_seats = False

    if already_checkout:
        print("[DEBUG] Already on checkout/review page; no seats step needed")
        return []

    if review_to_seats:
        result = _js_click_button(
            "(txt.includes('next') && txt.includes('seat')) || "
            "(txt.includes('choose') && txt.includes('seat'))"
        )
        print(f"[DEBUG] Review-page Next:Seats click result: {result}")
        if 'clicked' not in result.lower():
            print("[DEBUG] Could not click review-page 'Next: Seats'")
            return []
    elif already_bags:
        print("[DEBUG] Already on bags page; skipping initial Next: Seats click")
    elif already_seats:
        print("[DEBUG] Already on seats page; skipping initial Next: Seats click")
    else:
        # Step 1: Click "Next: Seats" button
        result = _js_click_button(
            "(txt.includes('next') && txt.includes('seat')) || "
            "(txt.includes('choose') && txt.includes('seat'))"
        )
        print(f"[DEBUG] Next:Seats click result: {result}")
        if 'clicked' not in result.lower():
            print("[DEBUG] No 'Next: Seats' button found")
            return []

    selected_seats = []
    # Step 2-4: For each leg, parse seat map and select best seat
    for leg in range(2):
        time.sleep(8)
        snap = browser_snapshot(limit=1000)

        # Check for flight/leg tabs — click the right one for this leg
        # Expedia shows tabs like: tab "San Francisco to Palm Springs" / tab "Palm Springs to San Francisco"
        # or tab "Flight 1" / tab "Flight 2"
        if leg == 1:
            # Look for a second flight tab that is NOT [selected]
            # Expedia uses en-dash/em-dash between cities: "Palm Springs – San Francisco"
            for line in snap.split("\n"):
                lo = line.strip().lower()
                if 'tab' in lo and '[ref=' in lo and '[selected]' not in lo:
                    ref_m = re.search(r'\[ref=(\w+)\]', line)
                    if ref_m:
                        print(f"[DEBUG] Clicking return leg tab: {line.strip()[:100]}")
                        _click_ref(ref_m.group(1))
                        time.sleep(5)
                        snap = browser_snapshot(limit=1000)
                        break

        seats = _parse_seat_map(snap)
        print(f"[DEBUG] Leg {leg+1}: found {len(seats)} available seats")
        if not seats:
            # No seats parsed — either airline blocks it or page hasn't loaded
            # Check for explicit "not available" messages
            lo_snap = snap.lower()
            next_bags_ready = _cdp_eval("""
            (() => {
                function isVisible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                }
                const els = [...document.querySelectorAll('button, a, [role="button"], span, div')];
                for (const el of els) {
                    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    if (!txt || !isVisible(el)) continue;
                    if (txt.includes('next: bags') || (txt.includes('next') && txt.includes('bags'))) {
                        return 'true';
                    }
                }
                return 'false';
            })()
            """)
            no_selection = any(kw in lo_snap for kw in [
                'seat selection is not available',
                'not available for this flight',
                'seats are not available',
                'no seats available',
                'assigned at the gate',
                'assigned at check-in',
                'skip seat selection',
                'choose your seats after booking',
                'you can select seats for this flight by contacting',
            ])
            if no_selection:
                print(f"[DEBUG] Leg {leg+1}: airline doesn't allow seat selection")
                tg_send(chat_id, f"💺 Leg {leg+1}: seat selection not available — assigned at check-in")
                if str(next_bags_ready).lower() in ('true', '1'):
                    print(f"[DEBUG] Leg {leg+1}: Next: Bags available; exiting seat loop")
                    break
            else:
                print(f"[DEBUG] Leg {leg+1}: no seats parsed from snapshot")
                tg_send(chat_id, f"💺 Leg {leg+1}: no seat map found — skipping")
            # Always continue to check for return leg or proceed to checkout
            continue

        # Send pricing summary to Telegram
        aisle = [s for s in seats if s["type"] == "aisle"]
        middle = [s for s in seats if s["type"] == "middle"]
        window = [s for s in seats if s["type"] == "window"]
        summary = f"🗺 Leg {leg+1} seat map: {len(seats)} available\n"
        summary += f"  Aisle: {len(aisle)} seats"
        if aisle:
            summary += f" (${min(s['price'] for s in aisle)}-${max(s['price'] for s in aisle)})"
        summary += f"\n  Middle: {len(middle)} seats"
        if middle:
            summary += f" (${min(s['price'] for s in middle)}-${max(s['price'] for s in middle)})"
        summary += f"\n  Window: {len(window)} seats"
        if window:
            summary += f" (${min(s['price'] for s in window)}-${max(s['price'] for s in window)})"
        tg_send(chat_id, summary)

        seat, ref = _select_best_seat(snap)
        if not seat:
            break

        tg_send(chat_id, f"💺 Leg {leg+1}: selecting {seat['label']} — {seat['type']} seat, ${seat['price']}")
        _click_ref(ref)
        confirm_result = _cdp_eval("""
        (() => {
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            }
            function hardClick(el) {
                if (!el) return false;
                el.scrollIntoView({block: 'center'});
                for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                }
                if (typeof el.click === 'function') el.click();
                return true;
            }
            const buttons = [...document.querySelectorAll('button, [role="button"], a')];
            for (const el of buttons) {
                const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                if (!txt || !isVisible(el)) continue;
                if (txt === 'select seat' || txt === 'select' || (txt.includes('select') && txt.includes('seat'))) {
                    hardClick(el);
                    return 'clicked_button: ' + txt;
                }
            }
            const popup = [...document.querySelectorAll('div, section, article')].find(el => {
                const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                return isVisible(el) && txt.includes('seat ') && txt.includes('select seat');
            });
            if (popup) {
                const nested = [...popup.querySelectorAll('button, [role="button"], a')].find(el => {
                    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    return txt === 'select seat' || txt === 'select' || (txt.includes('select') && txt.includes('seat'));
                });
                if (nested) {
                    hardClick(nested);
                    return 'clicked_popup_button';
                }
            }
            return 'not found';
        })()
        """)
        print(f"[DEBUG] Select-seat confirm result: {confirm_result}")
        if 'clicked' not in str(confirm_result).lower():
            tg_send(chat_id, f"⚠️ Leg {leg+1}: seat popup confirm failed for {seat['label']}")
            break
        FLIGHT_RESULTS[chat_id]["seat_selected"] = True
        time.sleep(3)
        selected_seats.append(f"{seat['label']} ({seat['type']}, ${seat['price']})")

        next_bags_after_confirm = _cdp_eval("""
        (() => {
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            }
            function hardClick(el) {
                if (!el) return false;
                el.scrollIntoView({block: 'center'});
                for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                }
                if (typeof el.click === 'function') el.click();
                return true;
            }
            const els = [...document.querySelectorAll('button, a, [role="button"], span, div')];
            for (const el of els) {
                const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                if (!txt || !isVisible(el)) continue;
                if (txt.includes('next: bags') || (txt.includes('next') && txt.includes('bags'))) {
                    hardClick(el);
                    return 'clicked: ' + txt;
                }
            }
            return 'not found';
        })()
        """)
        print(f"[DEBUG] Next:Bags after confirm result: {next_bags_after_confirm}")
        if 'clicked' in str(next_bags_after_confirm).lower():
            print("[DEBUG] Clicked Next: Bags immediately after seat confirmation")
            break

        # After first leg, check for return leg tab before advancing
        if leg == 0:
            next_bags_ready = _cdp_eval("""
            (() => {
                function isVisible(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                }
                const els = [...document.querySelectorAll('button, a, [role="button"], span, div')];
                for (const el of els) {
                    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    if (!txt || !isVisible(el)) continue;
                    if (txt.includes('next: bags') || (txt.includes('next') && txt.includes('bags'))) {
                        return 'true';
                    }
                }
                return 'false';
            })()
            """)
            print(f"[DEBUG] Next:Bags available after leg 1: {next_bags_ready}")
            if str(next_bags_ready).lower() in ('true', '1'):
                print("[DEBUG] Next: Bags is available after leg 1; not forcing leg 2 seat selection")
                break

            snap = browser_snapshot(limit=1000)
            has_second_tab = False
            for line in snap.split("\n"):
                lo = line.strip().lower()
                if 'tab' in lo and '[ref=' in lo and '[selected]' not in lo:
                    has_second_tab = True
                    break
            if has_second_tab:
                print("[DEBUG] Found return leg tab, continuing to leg 2")
                continue

    next_bags_direct = _click_next_bags_direct()
    print(f"[DEBUG] Direct Next:Bags click result: {next_bags_direct}")
    if 'clicked' in str(next_bags_direct).lower():
        time.sleep(4)

    # Always advance after the loop (both legs done or skipped)
    time.sleep(2)

    def _advance_after_seats():
        bags_announced = False
        last_kind = None

        def _cdp_page_kind():
            return str(_cdp_eval("""
            (() => {
                const txt = (document.body.innerText || '').toLowerCase();
                if (txt.includes('review your trip') && txt.includes('next: seats')) return 'seats';
                if (
                    txt.includes('how would you like to pay') ||
                    txt.includes('billing address') ||
                    txt.includes('remember this card') ||
                    txt.includes('complete booking') ||
                    txt.includes('review and book your trip') ||
                    txt.includes('review your trip') ||
                    txt.includes('secure booking') ||
                    txt.includes("who's traveling") ||
                    txt.includes('traveler name')
                ) return 'checkout';
                if (txt.includes('add your bags') || (txt.includes('bags') && (txt.includes('next: checkout') || txt.includes('next flight')))) return 'bags';
                if (txt.includes('choose your seats') || txt.includes('next: bags') || txt.includes('next: seats') || txt.includes('seat key')) return 'seats';
                return 'unknown';
            })()
            """)).strip().lower()

        def _cdp_click_button(*needles):
            needles_json = json.dumps([n.lower() for n in needles])
            return str(_cdp_eval(f"""
            (() => {{
                function isVisible(el) {{
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                }}
                const needles = {needles_json};
                const els = [...document.querySelectorAll('button, a, [role="button"], span, div')];
                for (const el of els) {{
                    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    if (!txt || !isVisible(el)) continue;
                    if (needles.every(n => txt.includes(n))) {{
                        el.scrollIntoView({{block: 'center'}});
                        el.click();
                        return 'clicked: ' + txt;
                    }}
                }}
                return 'not found';
            }})()
            """))

        def _page_kind():
            return _cdp_page_kind()

        for attempt in range(8):
            kind = _page_kind()
            print(f"[DEBUG] Post-seat flow attempt {attempt+1}: {kind}")
            state_changed = kind != last_kind
            last_kind = kind

            if kind == 'checkout':
                return 'already at checkout'

            if kind == 'seats':
                next_bags = _cdp_click_button('next', 'bags')
                if 'clicked' not in next_bags.lower():
                    next_bags = _cdp_click_button('continue', 'bags')
                print(f"[DEBUG] Seats next-bags click result: {next_bags}")
                if 'clicked' in next_bags.lower():
                    time.sleep(4)
                    continue

            if kind == 'bags':
                if not bags_announced:
                    tg_send(chat_id, "🧳 Bags step detected — continuing to checkout...")
                    bags_announced = True

                next_flight = _cdp_click_button('next flight')
                if 'clicked' not in next_flight.lower():
                    next_flight = _cdp_click_button('next', 'flight')
                print(f"[DEBUG] Bags next-flight click result: {next_flight}")
                if 'clicked' in next_flight.lower():
                    time.sleep(4)
                    continue

                next_checkout = _cdp_click_button('next', 'checkout')
                if 'clicked' not in next_checkout.lower():
                    next_checkout = _cdp_click_button('continue', 'checkout')
                print(f"[DEBUG] Bags next-checkout click result: {next_checkout}")
                if 'clicked' in next_checkout.lower():
                    time.sleep(4)
                    continue

                if not state_changed:
                    time.sleep(2)
                    continue

            fallback = _cdp_click_button('save', 'close')
            if 'clicked' not in fallback.lower():
                fallback = _cdp_click_button('next', 'checkout')
            if 'clicked' not in fallback.lower():
                fallback = _cdp_click_button('next', 'bags')
            if 'clicked' not in fallback.lower():
                fallback = _cdp_click_button('continue')
            print(f"[DEBUG] Post-seat fallback click result: {fallback}")
            if 'clicked' in fallback.lower():
                time.sleep(4)
                continue

            time.sleep(2)

        return 'not at checkout'

    result = _advance_after_seats()
    print(f"[DEBUG] Advance to checkout: {result}")

    return selected_seats


# ── Checkout form auto-fill ────────────────────────────────────────────────

BILLING_ADDRESS = {
    "street": "808 Irwin Court",
    "city": "Hillsborough",
    "state": "CA",
    "zip": "94010",
}

def _fill_traveler_and_protection(chat_id):
    """Select 'Adam Marquardt' from traveler dropdown and 'No protection'.
    Uses CDP direct eval — bypasses gateway (checkout page too heavy for snapshots)."""
    result = _cdp_eval("""
    (() => {
        const results = [];
        const selects = document.querySelectorAll('select');
        let travelerSet = false;
        for (const sel of selects) {
            let preferred = null;
            let shortPreferred = null;
            let fallback = null;
            for (const opt of sel.options) {
                const t = opt.text.trim().toLowerCase();
                if (t === 'adam marquardt') shortPreferred = opt;
                else if ((t === 'adam f marquardt' || t.includes('adam frederick marquardt')) && !preferred) preferred = opt;
                else if (t.includes('adam') && t.includes('marquardt') && !fallback) fallback = opt;
            }
            const chosen = shortPreferred || preferred || fallback;
            if (chosen) {
                sel.value = chosen.value;
                sel.dispatchEvent(new Event('input', {bubbles: true}));
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                sel.dispatchEvent(new Event('blur', {bubbles: true}));
                travelerSet = true;
                results.push('traveler:' + chosen.text.trim());
                break;
            }
        }
        if (!travelerSet) results.push('traveler:NOT_FOUND');

        const els = document.querySelectorAll('label, span, div, button, input');
        let protectionSet = false;
        for (const el of els) {
            const txt = (el.textContent || '').toLowerCase().trim();
            if (txt.includes('no protection') || txt.includes('decline') ||
                (txt.includes('no') && txt.includes('trip protection'))) {
                el.click();
                protectionSet = true;
                results.push('protection:declined');
                break;
            }
        }
        if (!protectionSet) results.push('protection:NOT_FOUND');
        return results.join(', ');
    })()
    """)
    print(f"[DEBUG] Traveler & protection fill: {result}")
    return result


def _fill_card_info(chat_id, card_num, exp_date, cvv):
    """Fill card fields using CDP direct eval — bypasses gateway timeout.
    Scrolls to payment section, fills card/billing/checkbox all via JS."""
    addr = BILLING_ADDRESS

    # Parse exp_date: "03/31" -> month="03", year="2031"
    exp_parts = exp_date.replace("-", "/").split("/")
    exp_month = exp_parts[0].zfill(2) if len(exp_parts) >= 1 else "01"
    exp_year_raw = exp_parts[1] if len(exp_parts) >= 2 else "2030"
    exp_year = exp_year_raw if len(exp_year_raw) == 4 else "20" + exp_year_raw

    # First scroll down to payment section
    _cdp_eval("window.scrollTo(0, document.body.scrollHeight * 0.6);")
    time.sleep(2)

    result = _cdp_eval(f"""
    (function() {{
        function isVisible(el) {{
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        }}
        function setVal(input, val) {{
            if (!input || !isVisible(input)) return false;
            input.focus();
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
                || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(input, val);
            else input.value = val;
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
            input.dispatchEvent(new Event('blur', {{bubbles: true}}));
            return input.value === val;
        }}
        function setSelect(sel, val) {{
            if (!sel || !isVisible(sel)) return false;
            for (const opt of sel.options) {{
                if (opt.value === val || opt.text.toLowerCase().includes(val.toLowerCase())) {{
                    sel.value = opt.value;
                    sel.selectedIndex = opt.index;
                    sel.dispatchEvent(new Event('input', {{bubbles: true}}));
                    sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    sel.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    return true;
                }}
            }}
            // Try partial match on value
            for (const opt of sel.options) {{
                if (opt.value.includes(val) || val.includes(opt.value)) {{
                    sel.value = opt.value;
                    sel.selectedIndex = opt.index;
                    sel.dispatchEvent(new Event('input', {{bubbles: true}}));
                    sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    sel.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    return true;
                }}
            }}
            return false;
        }}
        const monthSelect = document.querySelector('select[name="creditCards[0].expiration_month"]');
        const yearSelect = document.querySelector('select[name="creditCards[0].expiration_year"]');
        const cvvInput = document.querySelector('input[name="creditCards[0].stored_card_security_code"], input[name="creditCards[0].security_code"], input[aria-label="Security code"]');
        const streetInput = document.querySelector('input[aria-label="Billing address 1"], input[name*="address1"], input[name*="line1"], input[placeholder*="123 Main"]');
        const cityInput = document.querySelector('input[aria-label="City"], input[name*="city"]');
        const stateSelect = document.querySelector('select[aria-label="State"], select[name*="state"], select[name*="province"], select[name*="region"]');
        const zipInput = document.querySelector('input[aria-label="ZIP code"], input[name*="zip"], input[name*="postal"]');
        const results = [];
        const done = new Set();

        if (monthSelect) {{
            const ok = setSelect(monthSelect, String(parseInt('{exp_month}', 10)));
            results.push(ok ? 'exp_month:ok(exact)' : 'exp_month:fail(exact)');
            if (ok) done.add('exp_month');
        }}
        if (yearSelect) {{
            const ok = setSelect(yearSelect, '{exp_year}');
            results.push(ok ? 'exp_year:ok(exact)' : 'exp_year:fail(exact)');
            if (ok) done.add('exp_year');
        }}
        if (cvvInput) {{
            const ok = setVal(cvvInput, '{cvv}');
            results.push(ok ? 'cvv:ok(exact)' : 'cvv:fail(exact)');
            if (ok) done.add('cvv');
        }}
        if (streetInput) {{
            const ok = setVal(streetInput, '{addr["street"]}');
            results.push(ok ? 'street:ok(exact)' : 'street:fail(exact)');
            if (ok) done.add('street');
        }}
        if (cityInput) {{
            const ok = setVal(cityInput, '{addr["city"]}');
            results.push(ok ? 'city:ok(exact)' : 'city:fail(exact)');
            if (ok) done.add('city');
        }}
        if (stateSelect) {{
            const ok = setSelect(stateSelect, '{addr["state"]}');
            results.push(ok ? 'state:ok(exact)' : 'state:fail(exact)');
            if (ok) done.add('state');
        }}
        if (zipInput) {{
            const ok = setVal(zipInput, '{addr["zip"]}');
            results.push(ok ? 'zip:ok(exact)' : 'zip:fail(exact)');
            if (ok) done.add('zip');
        }}

        const inputs = document.querySelectorAll('input, select');
        for (const el of inputs) {{
            if (!isVisible(el)) continue;
            const id = (el.id || '').toLowerCase();
            const name = (el.name || '').toLowerCase();
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            const ph = (el.placeholder || '').toLowerCase();
            const key = id + ' ' + name + ' ' + label + ' ' + ph;
            const isSelect = el.tagName === 'SELECT';

            // Card number
            if (!done.has('card') && key.match(/card.?num|debit.?num|credit.?num|cc.?num/)) {{
                setVal(el, '{card_num}');
                results.push('card:ok');
                done.add('card');
            }}
            // Expiration MONTH (select dropdown)
            else if (!done.has('exp_month') && isSelect && key.match(/month|exp.*month/)) {{
                const ok = setSelect(el, String(parseInt('{exp_month}', 10)));
                results.push(ok ? 'exp_month:ok' : 'exp_month:fail');
                if (ok) done.add('exp_month');
            }}
            // Expiration YEAR (select dropdown)
            else if (!done.has('exp_year') && isSelect && key.match(/year|exp.*year/)) {{
                const ok = setSelect(el, '{exp_year}');
                results.push(ok ? 'exp_year:ok' : 'exp_year:fail');
                if (ok) done.add('exp_year');
            }}
            // Expiration as text input (some sites use single field)
            else if (!done.has('exp') && !isSelect && key.match(/expir|exp.?date/) && !key.match(/month|year/)) {{
                setVal(el, '{exp_date}');
                results.push('exp:ok');
                done.add('exp');
            }}
            // CVV
            else if (!done.has('cvv') && key.match(/cvv|cvc|secur|csc/)) {{
                const ok = setVal(el, '{cvv}');
                results.push(ok ? 'cvv:ok' : 'cvv:fail');
                if (ok) done.add('cvv');
            }}
            // Street address
            else if (!done.has('street') && key.match(/street|address.?1|billing.?addr|address.?line/)) {{
                const ok = setVal(el, '{addr["street"]}');
                results.push(ok ? 'street:ok' : 'street:fail');
                if (ok) done.add('street');
            }}
            // City
            else if (!done.has('city') && key.match(/city/) && !key.match(/electric/)) {{
                const ok = setVal(el, '{addr["city"]}');
                results.push(ok ? 'city:ok' : 'city:fail');
                if (ok) done.add('city');
            }}
            // State (select dropdown)
            else if (!done.has('state') && isSelect && key.match(/state|province|region/)) {{
                const ok = setSelect(el, '{addr["state"]}');
                results.push(ok ? 'state:ok' : 'state:fail');
                if (ok) done.add('state');
            }}
            // ZIP code
            else if (!done.has('zip') && key.match(/zip|postal/)) {{
                const ok = setVal(el, '{addr["zip"]}');
                results.push(ok ? 'zip:ok' : 'zip:fail');
                if (ok) done.add('zip');
            }}
        }}

        // If month/year selects not found by label, try matching by select position near "Expiration"
        if (!done.has('exp_month')) {{
            const selects = document.querySelectorAll('select');
            for (const sel of selects) {{
                const opts = Array.from(sel.options).map(o => o.text.toLowerCase());
                if (opts.some(o => o.includes('jan') || o.includes('feb') || o.includes('mar'))) {{
                    const ok = setSelect(sel, String(parseInt('{exp_month}', 10)));
                    results.push(ok ? 'exp_month:ok(fallback)' : 'exp_month:fail(fallback)');
                    if (ok) done.add('exp_month');
                    break;
                }}
            }}
        }}
        if (!done.has('exp_year')) {{
            const selects = document.querySelectorAll('select');
            for (const sel of selects) {{
                const opts = Array.from(sel.options).map(o => o.text);
                if (opts.some(o => o.match(/^20\\d\\d$/))) {{
                    const ok = setSelect(sel, '{exp_year}');
                    results.push(ok ? 'exp_year:ok(fallback)' : 'exp_year:fail(fallback)');
                    if (ok) done.add('exp_year');
                    break;
                }}
            }}
        }}

        // Check "Remember this card" / "Save card"
        const checkboxes = document.querySelectorAll('input[type="checkbox"]');
        for (const cb of checkboxes) {{
            const lbl = (cb.getAttribute('aria-label') || '').toLowerCase();
            const parent = (cb.closest('label') || {{}}).textContent || '';
            if (lbl.includes('remember') || parent.toLowerCase().includes('remember') ||
                lbl.includes('save') || parent.toLowerCase().includes('save this card')) {{
                if (!cb.checked) {{ cb.click(); }}
                results.push('remember:ok');
                break;
            }}
        }}
        return results.join(', ') || 'no fields matched';
    }})()
    """)
    print(f"[DEBUG] Card fill result (main frame): {result}")

    # If main frame didn't find card fields, try iframes (PCI compliance)
    if 'card:ok' not in str(result):
        print("[DEBUG] Card not in main frame, checking iframes...")
        iframe_result = _cdp_eval(f"""
        (function() {{
            const results = [];
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {{
                try {{
                    const doc = iframe.contentDocument || iframe.contentWindow.document;
                    const inputs = doc.querySelectorAll('input');
                    for (const el of inputs) {{
                        const id = (el.id || '').toLowerCase();
                        const name = (el.name || '').toLowerCase();
                        const label = (el.getAttribute('aria-label') || '').toLowerCase();
                        const ph = (el.placeholder || '').toLowerCase();
                        const key = id + ' ' + name + ' ' + label + ' ' + ph;
                        if (key.match(/card.?num|debit.?num|credit.?num|cc.?num/)) {{
                            el.focus(); el.value = '{card_num}';
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                            el.dispatchEvent(new Event('change', {{bubbles: true}}));
                            results.push('iframe_card:ok');
                        }}
                        else if (key.match(/expir|exp.?date/)) {{
                            el.focus(); el.value = '{exp_date}';
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                            results.push('iframe_exp:ok');
                        }}
                        else if (key.match(/cvv|cvc|secur|csc/)) {{
                            el.focus(); el.value = '{cvv}';
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                            results.push('iframe_cvv:ok');
                        }}
                    }}
                }} catch(e) {{
                    results.push('iframe_blocked:' + e.message);
                }}
            }}
            return results.join(', ') || 'no iframes or fields found';
        }})()
        """)
        print(f"[DEBUG] Card fill result (iframes): {iframe_result}")
        result = str(result) + ', ' + str(iframe_result)

    _cdp_eval("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)
    protection_result = _cdp_eval("""
    (() => {
        function clickTarget(el) {
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
            el.click();
            return true;
        }

        const insuranceRadios = [...document.querySelectorAll('input[type="radio"][name="insurance_piid"]')];
        for (const radio of insuranceRadios) {
            const parentText = ((radio.closest('label, div') || {}).textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
            if (parentText.includes('no protection')) {
                radio.checked = true;
                radio.dispatchEvent(new Event('input', {bubbles: true}));
                radio.dispatchEvent(new Event('change', {bubbles: true}));
                radio.click();
                return 'protection:ok(exact-radio)';
            }
        }

        const texts = [
            ...document.querySelectorAll('label, div, span, button, input')
        ];

        for (const el of texts) {
            const txt = (el.textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
            if (txt.includes('no protection')) {
                const label = el.closest('label');
                if (clickTarget(label || el)) return 'protection:ok(text)';
            }
        }

        const radios = [...document.querySelectorAll('input[type="radio"]')];
        for (const radio of radios) {
            const name = (radio.getAttribute('name') || '').toLowerCase();
            const aria = (radio.getAttribute('aria-label') || '').toLowerCase();
            const parentText = ((radio.closest('label, div') || {}).textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
            if (aria.includes('no protection') || parentText.includes('no protection') ||
                ((name.includes('protection') || name.includes('insurance')) && parentText.includes('no'))) {
                if (!radio.checked) radio.click();
                return 'protection:ok(radio)';
            }
        }

        return 'protection:not_found';
    })()
    """)
    print(f"[DEBUG] Protection result: {protection_result}")
    result = str(result) + ', ' + str(protection_result)

    return result


def _fill_saved_card_cvv(chat_id, cvv):
    """Fill saved card CVV using CDP direct eval — bypasses gateway timeout.
    Scrolls to payment section, fills CVV all via JS."""
    # First scroll down to payment section
    _cdp_eval("window.scrollTo(0, document.body.scrollHeight * 0.6);")
    time.sleep(2)

    result = _cdp_eval(f"""
    (function() {{
        function isVisible(el) {{
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        }}
        function setVal(input, val) {{
            if (!input || !isVisible(input)) return false;
            input.focus();
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
                || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(input, val);
            else input.value = val;
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
            input.dispatchEvent(new Event('blur', {{bubbles: true}}));
            return input.value === val;
        }}
        const cvvInput = document.querySelector('input[name="creditCards[0].stored_card_security_code"], input[name="creditCards[0].security_code"], input[aria-label="Security code"]');
        const results = [];
        if (cvvInput) {{
            const ok = setVal(cvvInput, '{cvv}');
            results.push(ok ? 'cvv:ok(exact)' : 'cvv:fail(exact)');
        }}
        return results.join(', ') || 'no fields matched';
    }})()
    """)
    print(f"[DEBUG] Saved-card CVV fill result: {result}")

    # If main frame didn't find card fields, try iframes (PCI compliance)
    if 'cvv:ok' not in str(result):
        print("[DEBUG] CVV not in main frame, checking iframes...")
        iframe_result = _cdp_eval(f"""
        (function() {{
            const results = [];
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {{
                try {{
                    const doc = iframe.contentDocument || iframe.contentWindow.document;
                    const inputs = doc.querySelectorAll('input');
                    for (const el of inputs) {{
                        const id = (el.id || '').toLowerCase();
                        const name = (el.name || '').toLowerCase();
                        const label = (el.getAttribute('aria-label') || '').toLowerCase();
                        const ph = (el.placeholder || '').toLowerCase();
                        const key = id + ' ' + name + ' ' + label + ' ' + ph;
                        if (key.match(/cvv|cvc|secur|csc/)) {{
                            el.focus(); el.value = '{cvv}';
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));
                            results.push('iframe_cvv:ok');
                        }}
                    }}
                }} catch(e) {{
                    results.push('iframe_blocked:' + e.message);
                }}
            }}
            return results.join(', ') || 'no iframes or fields found';
        }})()
        """)
        print(f"[DEBUG] Saved-card CVV fill result (iframes): {iframe_result}")
        result = str(result) + ', ' + str(iframe_result)

    _cdp_eval("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)
    protection_result = _cdp_eval("""
    (() => {
        function clickTarget(el) {
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
            el.click();
            return true;
        }

        const insuranceRadios = [...document.querySelectorAll('input[type="radio"][name="insurance_piid"]')];
        for (const radio of insuranceRadios) {
            const parentText = ((radio.closest('label, div') || {}).textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
            if (parentText.includes('no protection')) {
                radio.checked = true;
                radio.dispatchEvent(new Event('input', {bubbles: true}));
                radio.dispatchEvent(new Event('change', {bubbles: true}));
                radio.click();
                return 'protection:ok(exact-radio)';
            }
        }

        const texts = [
            ...document.querySelectorAll('label, div, span, button, input')
        ];

        for (const el of texts) {
            const txt = (el.textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
            if (txt.includes('no protection')) {
                const label = el.closest('label');
                if (clickTarget(label || el)) return 'protection:ok(text)';
            }
        }

        const radios = [...document.querySelectorAll('input[type="radio"]')];
        for (const radio of radios) {
            const name = (radio.getAttribute('name') || '').toLowerCase();
            const aria = (radio.getAttribute('aria-label') || '').toLowerCase();
            const parentText = ((radio.closest('label, div') || {}).textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
            if (aria.includes('no protection') || parentText.includes('no protection') ||
                ((name.includes('protection') || name.includes('insurance')) && parentText.includes('no'))) {
                if (!radio.checked) radio.click();
                return 'protection:ok(radio)';
            }
        }

        return 'protection:not_found';
    })()
    """)
    print(f"[DEBUG] Protection result: {protection_result}")
    result = str(result) + ', ' + str(protection_result)

    return result


def _js_eval(js_code):
    """Run arbitrary JS on the page using a fresh snapshot ref."""
    snap = browser_snapshot(limit=100)
    ref = "e1"
    for line in snap.split("\n"):
        m = re.search(r'\[ref=(\w+)\]', line)
        if m:
            ref = m.group(1)
            break
    try:
        result = browser_evaluate(ref, f"(el) => {{ {js_code} }}")
        return str(result)
    except RuntimeError as e:
        return f"error: {e}"


def _handle_checkout_from_current(chat_id):
    """Test shortcut: fill checkout form on whatever page is currently open.
    Use /checkout in Telegram to trigger this."""
    tg_send(chat_id, "🔧 Testing checkout form fill on current page...")
    FLIGHT_RESULTS[chat_id] = FLIGHT_RESULTS.get(chat_id, {})
    _handle_checkout_form(chat_id)


def _handle_seats_from_current(chat_id):
    """Test shortcut: select seats → Next:Checkout → fill form.
    Use /seats in Telegram to trigger from the seat map page."""
    FLIGHT_RESULTS[chat_id] = FLIGHT_RESULTS.get(chat_id, {})
    tg_send(chat_id, "💺 Selecting seats on current page...")
    selected = _handle_seat_selection(chat_id)
    transition_state = 'checkout'
    if selected:
        tg_send(chat_id, f"💺 Selected seats: {', '.join(selected)}")
        transition_state = _transition_after_selected_seats(chat_id)
    else:
        tg_send(chat_id, "💺 No seat map found — continuing to checkout.")
    if transition_state == 'seats':
        return
    _handle_checkout_form(chat_id)


def _handle_checkout_form(chat_id):
    """Auto-fill checkout form: select traveler, fill address, prompt for card."""
    time.sleep(3)

    modal_result = _cdp_eval("""
    (() => {
        function isVisible(el) {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        }
        const texts = [...document.querySelectorAll('button, a, div, span')];
        const bodyText = (document.body.innerText || '').toLowerCase();
        if (!bodyText.includes('this booking is almost yours')) return 'modal:not_present';
        for (const el of texts) {
            const txt = (el.textContent || '').replace(/\s+/g, ' ').trim();
            if (isVisible(el) && txt.includes('Continue Booking')) {
                el.scrollIntoView({block: 'center'});
                el.click();
                return 'modal:continue_booking';
            }
        }
        return 'modal:present_not_clicked';
    })()
    """)
    print(f"[DEBUG] Continue-booking modal result: {modal_result}")
    if 'continue_booking' in str(modal_result).lower():
        time.sleep(4)

    review_step = _cdp_eval("""
    (() => {
        const txt = (document.body.innerText || '').toLowerCase();
        return txt.includes('review your trip') || txt.includes('next: checkout');
    })()
    """)
    if str(review_step).lower() in ('true', '1'):
        advance_result = _cdp_eval("""
        (() => {
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            }
            const btns = [...document.querySelectorAll('button, a')];
            for (const b of btns) {
                const txt = (b.textContent || '').replace(/\s+/g, ' ').trim();
                if (isVisible(b) && txt.includes('Next: Checkout')) {
                    b.scrollIntoView({block: 'center'});
                    b.click();
                    return 'clicked: ' + txt;
                }
            }
            return 'not found';
        })()
        """)
        print(f"[DEBUG] Review-step Next: Checkout result: {advance_result}")
        if 'clicked' in str(advance_result).lower():
            tg_send(chat_id, "➡️ Advancing from review page to payment step...")
            time.sleep(5)

    checkout_ready = _cdp_eval("""
    (() => {
        const txt = (document.body.innerText || '').toLowerCase();
        return txt.includes('how would you like to pay') ||
               txt.includes('billing address') ||
               txt.includes('secure booking') ||
               txt.includes("who's traveling") ||
               txt.includes('traveler name') ||
               txt.includes('remember this card') ||
               txt.includes('complete booking') ||
               txt.includes('review and book your trip');
    })()
    """)
    if str(checkout_ready).lower() not in ('true', '1'):
        tg_send(chat_id, "⚠️ Still not on checkout yet — not asking for card info. Use /snap if needed.")
        return

    # Step 1: Select traveler and no-protection
    tg_send(chat_id, "📝 Filling checkout form...")
    _fill_traveler_and_protection(chat_id)
    time.sleep(2)

    # Step 2: Prompt for card info
    tg_send(chat_id,
            "💳 Your saved card is selected. Send me just the CVV:\n\n"
            "`123`\n\n"
            "I'll fill it in and you just review & submit.")
    # Set state to await card info
    if chat_id in FLIGHT_RESULTS:
        FLIGHT_RESULTS[chat_id]["phase"] = "awaiting_card"


def _replay_expedia(state, up_to):
    """Replay the Expedia booking flow from scratch up to a given phase.
    Phases: 'search' → 'outbound_clicked' → 'outbound_fare_clicked' →
            'return_clicked' → 'return_fare_clicked'
    Returns (snapshot, error_or_None)."""
    _close_extra_tabs()
    browser_navigate(_build_expedia_url(state))
    time.sleep(12)

    if up_to == "search":
        return browser_snapshot(limit=3000), None

    # Click outbound flight
    snap = browser_snapshot(limit=3000)
    fl = state["selected_flight"]
    ref = _find_flight_ref(snap, fl["airline"], fl["dep_time"])
    if not ref:
        return None, f"Couldn't find outbound {fl['airline']} {fl['dep_time']}"
    if not _click_ref(ref):
        return None, "Couldn't click outbound flight"
    time.sleep(5)

    if up_to == "outbound_clicked":
        return browser_snapshot(limit=3000), None

    # Click outbound fare
    snap = browser_snapshot(limit=1000)
    sf = state["selected_fare"]
    fare_ref = _find_fare_ref(snap, sf["label"], state.get("selected_fare_idx", 0))
    if not fare_ref:
        return None, f"Couldn't find fare '{sf['label']}'"
    if not _click_ref(fare_ref):
        return None, "Couldn't click outbound fare"
    time.sleep(5)

    if up_to == "outbound_fare_clicked":
        return browser_snapshot(limit=1000), None

    # Click return flight
    snap = browser_snapshot(limit=1000)
    rfl = state["selected_return_flight"]
    ref = _find_flight_ref(snap, rfl["airline"], rfl["dep_time"])
    if not ref:
        return None, f"Couldn't find return {rfl['airline']} {rfl['dep_time']}"
    if not _click_ref(ref):
        return None, "Couldn't click return flight"
    time.sleep(5)

    if up_to == "return_clicked":
        return browser_snapshot(limit=1000), None

    # Click return fare
    snap = browser_snapshot(limit=1000)
    rsf = state["selected_return_fare"]
    fare_ref = _find_fare_ref(snap, rsf["label"], state.get("selected_return_fare_idx", 0))
    if not fare_ref:
        return None, f"Couldn't find return fare '{rsf['label']}'"
    if not _click_ref(fare_ref):
        return None, "Couldn't click return fare"
    time.sleep(5)

    return browser_snapshot(limit=1000), None


def handle_fly(chat_id, args):
    """Expedia-powered flight search: /fly SFO SLC Apr 15 Apr 18"""
    parts = args.strip().split()
    if len(parts) < 2:
        tg_send(chat_id, "Usage: /fly <from> <to> [depart_date] [return_date]\n"
                         "Example: /fly SFO SLC Apr 15 Apr 18")
        return

    origin = parts[0].upper()
    dest = parts[1].upper()
    date_text = " ".join(parts[2:]) if len(parts) > 2 else ""

    # Parse dates — split on "to", "through", "thru", "-", "→"
    depart_date = None
    return_date = None
    if date_text:
        split = re.split(r'\bto\b|\bthrough\b|\bthru\b|\btill\b|\buntil\b|→|–|-{1,2}(?!\d{2}-)', date_text, maxsplit=1)
        depart_text = split[0].strip()
        return_text = split[1].strip() if len(split) > 1 else ""
        for filler in ["the ", "on "]:
            depart_text = depart_text[len(filler):] if depart_text.startswith(filler) else depart_text
            return_text = return_text[len(filler):] if return_text.startswith(filler) else return_text
        depart_text = re.sub(r'[\.\s,!?]+$', '', depart_text).strip()
        return_text = re.sub(r'[\.\s,!?]+$', '', return_text).strip()
        depart_date = _parse_date(depart_text) if depart_text else None
        return_date = _parse_date(return_text) if return_text else None

    if not depart_date:
        tg_send(chat_id, "⚠️ Need at least a departure date.\nExample: /fly SFO SLC Apr 15 Apr 18")
        return

    # Build Expedia deep link
    trip = "Roundtrip" if return_date else "Oneway"
    end_date = return_date if return_date else depart_date
    expedia_url = (f"https://www.expedia.com/go/flight/search/{trip}"
                   f"/{depart_date}/{end_date}"
                   f"?load=1&FromAirport={origin}&ToAirport={dest}"
                   f"&FromTime=362&ToTime=362&NumAdult=1")

    tg_send(chat_id, f"🔍 Searching {origin} → {dest} ({depart_date}" +
            (f" → {return_date})" if return_date else ")") +
            " on Expedia...")

    _close_extra_tabs()
    browser_navigate(expedia_url)

    # Poll for flight results to load (up to ~30s)
    flights = []
    for attempt in range(5):
        time.sleep(6 if attempt == 0 else 4)
        # Scroll past filters
        try:
            browser_cmd("evaluate", "--fn",
                        "(el) => { window.scrollBy(0, 800); return 'scrolled'; }",
                        "--ref", "e1", timeout=10)
        except RuntimeError:
            pass
        time.sleep(2)
        snap = browser_snapshot(limit=3000)
        flights = _parse_expedia_flights(snap)
        if flights:
            print(f"[DEBUG] Parsed {len(flights)} flights on attempt {attempt+1}")
            break
        print(f"[DEBUG] No flights parsed yet, attempt {attempt+1}")

    if not flights:
        tg_send(chat_id, f"⚠️ Expedia loaded but couldn't parse flight results.\n"
                "Use /snap or /screenshot to see the page.")
        return

    # Store results for number-picking (refs point to elements on current Expedia page)
    FLIGHT_RESULTS[chat_id] = {
        "flights": flights,
        "origin": origin,
        "dest": dest,
        "depart_date": depart_date,
        "return_date": return_date,
    }

    # Format message
    msg = f"✈️ {origin} → {dest} ({depart_date}"
    if return_date:
        msg += f" → {return_date}"
    msg += f")\nFound {len(flights)} flights on Expedia:\n\n"
    for i, fl in enumerate(flights, 1):
        msg += f"{i}. {fl['airline']}  {fl['dep_time']}→{fl['arr_time']}  {fl['duration']}  {fl['stops']}  {fl['price']}\n"

    msg += f"\n📋 Reply with a number (1-{len(flights)}) to select."
    tg_send(chat_id, msg)


def _parse_booking_page(snap):
    """Check if we're on a Google Flights booking page and format it nicely."""
    if "Booking options" not in snap and "Itinerary summary" not in snap:
        return None
    
    lines = snap.split("\n")
    flights_info = []
    booking_options = []
    total_price = None
    
    for line in lines:
        s = line.strip()
        # Price
        price_match = re.search(r'\$(\d+)\s+Lowest total price', s)
        if price_match:
            total_price = f"${price_match.group(1)}"
        # Flight legs
        if "Delta" in s and ("PM" in s or "AM" in s) and "Nonstop" in s:
            flights_info.append(s.replace("- text: ", ""))
        # Booking buttons
        if "Continue to book" in s:
            ref_match = re.search(r'\[ref=(\w+)\]', s)
            price_match2 = re.search(r'(\d+) US dollars', s)
            name_match = re.search(r'Delta (.+?) for', s)
            if ref_match and price_match2:
                name = name_match.group(1) if name_match else "?"
                booking_options.append({
                    "name": f"Delta {name}",
                    "price": f"${price_match2.group(1)}",
                    "ref": ref_match.group(1),
                })
    
    if not booking_options:
        return None
    
    msg = "🎫 BOOKING PAGE\n\n"
    if total_price:
        msg += f"💰 {total_price} round trip\n\n"
    msg += "✈️ Selected flights:\n"
    for fi in flights_info:
        msg += f"  • {fi}\n"
    msg += "\n📋 Fare options:\n"
    for bo in booking_options:
        msg += f"  • {bo['name']}: {bo['price']} → /click {bo['ref']}\n"
    msg += "\nTell me which fare to book (e.g. 'book Main Basic')\nor /click the ref to continue to Delta checkout."
    return msg


def _extract_flights(snap):
    """Parse flight listing from Google Flights snapshot."""
    flights = []
    for line in snap.split("\n"):
        s = line.strip()
        if 'link "From' not in s and 'link "from' not in s.lower():
            continue
        if 'Select flight' not in s:
            continue
        ref_match = re.search(r'\[ref=(\w+)\]', s)
        if not ref_match:
            continue
        ref = ref_match.group(1)
        # Extract key info
        price_match = re.search(r'(\d+) US dollars', s)
        price = f"${price_match.group(1)}" if price_match else "?"
        # Extract times
        time_match = re.search(r'at (\d+:\d+ [AP]M) on', s)
        arr_match = re.search(r'arrives at .+ at (\d+:\d+ [AP]M)', s)
        depart_time = time_match.group(1) if time_match else "?"
        arrive_time = arr_match.group(1) if arr_match else "?"
        # Airline
        airline = "?"
        for al in ["Delta", "United", "Frontier", "American", "Southwest", "JetBlue", "Alaska", "Spirit"]:
            if al in s:
                airline = al
                break
        # Duration + stops
        dur_match = re.search(r'Total duration (\d+ hr \d+ min)', s)
        duration = dur_match.group(1) if dur_match else "?"
        stops = "Nonstop" if "Nonstop" in s else re.search(r'(\d+ stop)', s)
        stops = stops.group(1) if hasattr(stops, 'group') else stops

        flights.append({
            "ref": ref,
            "price": price,
            "summary": f"{depart_time} → {arrive_time} | {airline} | {duration} | {stops}",
            "airline": airline,
            "depart_time": depart_time,
        })
    return flights


def handle_book(chat_id, args):
    """Trigger Apple Pay approval via book.py."""
    # Parse args: --merchant X --amount Y --description Z
    tg_send(chat_id, "💳 Triggering Apple Pay approval...")
    cmd = ["python3", BOOK_PY] + args.strip().split()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                cwd=str(Path(__file__).resolve().parent))
        output = result.stdout.strip()
        tg_send(chat_id, f"📱 book.py result:\n\n{output}")
    except subprocess.TimeoutExpired:
        tg_send(chat_id, "⏱️ book.py timed out (Apple Pay link may have expired)")
    except Exception as e:
        tg_send(chat_id, f"❌ book.py error: {e}")


def handle_pay(chat_id, args):
    """Fill payment form via fill_checkout.py."""
    tg_send(chat_id, "💳 Filling payment form with stored card...")
    extra = args.strip().split() if args.strip() else []
    cmd = ["python3", FILL_PY] + extra
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                cwd=str(Path(__file__).resolve().parent))
        output = result.stdout.strip()
        tg_send(chat_id, f"💳 fill_checkout.py result:\n\n{output}")
    except Exception as e:
        tg_send(chat_id, f"❌ fill_checkout.py error: {e}")


def handle_help(chat_id, args):
    tg_send(chat_id,
            "✈️ Flight Bot Commands:\n\n"
            "/go <url> — Navigate to URL\n"
            "/snap — Describe what's on the page\n"
            "/click <ref> — Click element\n"
            "/type <ref> <text> — Type into field\n"
            "/fill <ref> <value> — Fill field\n"
            "/screenshot — Send page screenshot\n"
            "/search — Open Google Flights (browser)\n"
            "/book --merchant X --amount Y --description Z — Apple Pay\n"
            "/pay [--submit] — Fill card into payment form\n"
            "/cvv <code> — Fill CVV on Delta checkout\n"
            "/help — This message\n\n"
            "Or just describe what you want in plain English!")


# ── Natural language fallback ───────────────────────────────────────────────

def handle_natural(chat_id, text):
    """Try to interpret natural language as a browser action."""
    lower = text.lower().strip()

    # Card info handler: user sends card details when in awaiting_card phase
    if chat_id in FLIGHT_RESULTS and FLIGHT_RESULTS[chat_id].get("phase") == "awaiting_card":
        cvv_match = re.match(r'^(\d{3,4})$', text.strip())
        if cvv_match:
            cvv = cvv_match.group(1)
            tg_send(chat_id, "💳 Filling saved-card CVV...")
            result = _fill_saved_card_cvv(chat_id, cvv)
            FLIGHT_RESULTS[chat_id]["phase"] = "checkout_ready"
            tg_send(chat_id, f"💳 Saved-card CVV filled: {result}\n\n✅ Review the page and complete booking when ready!")
            return
        else:
            tg_send(chat_id, "❌ Couldn't parse CVV. Send just `123` or `1234`.")
            return

    # Number-picker: user sends "1", "2", etc.
    num_match = re.match(r'^(\d{1,2})$', lower)
    if num_match and chat_id in FLIGHT_RESULTS:
        idx = int(num_match.group(1)) - 1
        state = FLIGHT_RESULTS[chat_id]
        phase = state.get("phase", "flight_selection")

        # ── Helper: finish checkout with seat selection ──
        def _finish_checkout():
            state["phase"] = "checkout"
            time.sleep(5)
            # Dismiss "Bundle & Save" / "No thanks" popups that appear after fare clicks
            _dismiss_popups()
            time.sleep(2)

            # Find the checkout tab: any tab NOT containing "Flights-Search" in URL
            # Expedia opens checkout in a new tab with a different URL pattern
            checkout_found = False
            for attempt in range(8):
                tabs = _list_tabs()
                for t in tabs:
                    url = t.get("url", "")
                    print(f"[DEBUG] Tab: {t['id'][:12]} | {url[:100]}")
                non_search = [t for t in tabs if "Flights-Search" not in t.get("url", "")
                              and t.get("url", "") not in ("", "about:blank")
                              and "chrome://" not in t.get("url", "")]
                if non_search:
                    ct = non_search[-1]
                    print(f"[DEBUG] Switching to checkout tab: {ct['id'][:12]} | {ct.get('url','')[:80]}")
                    browser_focus(ct["id"])
                    time.sleep(3)
                    checkout_found = True
                    break
                print(f"[DEBUG] No checkout tab yet, attempt {attempt+1}")
                time.sleep(3)
            if not checkout_found:
                print("[DEBUG] No separate checkout tab found, staying on current")
            tg_send(chat_id, "➡️ Processing Expedia post-fare steps...")
            selected = _handle_seat_selection(chat_id)
            transition_state = 'checkout'
            if selected:
                tg_send(chat_id, f"💺 Selected seats: {', '.join(selected)}")
                transition_state = _transition_after_selected_seats(chat_id)
            else:
                tg_send(chat_id, "➡️ No seat selection needed or Expedia went straight to bags/checkout.")
            # Auto-fill checkout form and prompt for card
            if transition_state == 'seats':
                return
            _handle_checkout_form(chat_id)

        # ── Helper: show return flights after outbound fare is selected ──
        def _show_return_flights(snap):
            is_roundtrip = state.get("return_date") is not None
            if is_roundtrip:
                for attempt in range(4):
                    return_flights = _parse_expedia_flights(snap)
                    if return_flights:
                        state["phase"] = "return_flight_selection"
                        state["return_flights"] = return_flights
                        msg = f"✈️ Return flights ({state['dest']} → {state['origin']}):\n\n"
                        for i, fl in enumerate(return_flights[:15], 1):
                            add = f" {fl['additional']}" if fl.get('additional') else ""
                            msg += f"{i}. {fl['airline']}  {fl['dep_time']}→{fl['arr_time']}  {fl['stops']}  {fl['price']}{add}\n"
                        msg += f"\n📋 Reply with a number (1-{min(15, len(return_flights))})."
                        tg_send(chat_id, msg)
                        return True

                    lo_snap = snap.lower()
                    if any(kw in lo_snap for kw in [
                        "return flight",
                        "choose your return",
                        "select your return",
                        f"{state['dest'].lower()} to {state['origin'].lower()}",
                    ]):
                        time.sleep(3)
                        snap = browser_snapshot(limit=1000)
                        continue

                    if any(kw in lo_snap for kw in [
                        "next: seats",
                        "choose seats",
                        "seat selection",
                        "review and book your trip",
                        "complete booking",
                        "how would you like to pay",
                    ]):
                        print("[DEBUG] Roundtrip flow reached seats/checkout before return flights were parsed")
                        return False

                    time.sleep(3)
                    snap = browser_snapshot(limit=1000)
            return False

        # ── Helper: show fares or auto-click single fare ──
        def _handle_fares(snap, label_prefix, fare_phase, on_fare_selected):
            fares = _parse_expedia_fares(snap)
            if fares and len(fares) == 1:
                # Auto-click single fare on current page (refs are fresh)
                tg_send(chat_id, f"✈️ {fares[0]['label']} {fares[0]['price']} — auto-selecting...")
                snap2 = _try_fast_click(fares[0]["ref"])
                if snap2:
                    return on_fare_selected(snap2, fares[0], 0)
                else:
                    return False, "Couldn't click fare"
            elif fares:
                state["phase"] = fare_phase
                state_key = "return_fares" if "return" in fare_phase else "fares"
                state[state_key] = fares
                msg = f"✈️ {label_prefix} — pick a fare:\n\n"
                for i, f in enumerate(fares, 1):
                    msg += f"{i}. {f['label']}  {f['price']}\n"
                msg += f"\nReply with a number (1-{len(fares)})."
                tg_send(chat_id, msg)
                return True, None
            return False, "Couldn't parse fares"

        # ── Return fare selection ──
        if phase == "return_fare_selection":
            fares = state.get("return_fares", [])
            if 0 <= idx < len(fares):
                state["selected_return_fare"] = fares[idx]
                state["selected_return_fare_idx"] = idx
                tg_send(chat_id, f"✈️ Booking {fares[idx]['label']} {fares[idx]['price']}...")
                # Fast path: try clicking stored ref
                snap = _try_fast_click(fares[idx]["ref"])
                if not snap:
                    tg_send(chat_id, "Page expired, replaying flow...")
                    snap, err = _replay_expedia(state, "return_fare_clicked")
                    if err:
                        tg_send(chat_id, f"⚠️ {err}")
                        return
                _finish_checkout()
            else:
                tg_send(chat_id, f"⚠️ Pick 1-{len(fares)}.")
            return

        # ── Return flight selection ──
        if phase == "return_flight_selection":
            flights = state.get("return_flights", [])
            if 0 <= idx < len(flights):
                rfl = flights[idx]
                state["selected_return_flight"] = rfl
                tg_send(chat_id, f"✅ Return: {rfl['airline']} {rfl['dep_time']} {rfl['price']}")
                # Fast path: try clicking stored ref
                snap = _try_fast_click(rfl["ref"])
                if not snap:
                    tg_send(chat_id, "Page expired, replaying flow...")
                    snap, err = _replay_expedia(state, "return_clicked")
                    if err:
                        tg_send(chat_id, f"⚠️ {err}")
                        return
                # Now handle fares
                fares = _parse_expedia_fares(snap)
                if fares and len(fares) == 1:
                    state["selected_return_fare"] = fares[0]
                    state["selected_return_fare_idx"] = 0
                    tg_send(chat_id, f"✈️ {fares[0]['label']} {fares[0]['price']} — auto-selecting...")
                    snap2 = _try_fast_click(fares[0]["ref"])
                    if not snap2:
                        snap2, err = _replay_expedia(state, "return_fare_clicked")
                        if err:
                            tg_send(chat_id, f"⚠️ {err}")
                            return
                    _finish_checkout()
                elif fares:
                    state["phase"] = "return_fare_selection"
                    state["return_fares"] = fares
                    msg = f"✈️ Return {rfl['airline']} {rfl['dep_time']} — pick a fare:\n\n"
                    for i, f in enumerate(fares, 1):
                        msg += f"{i}. {f['label']}  {f['price']}\n"
                    msg += f"\nReply with a number (1-{len(fares)})."
                    tg_send(chat_id, msg)
                else:
                    tg_send(chat_id, "⚠️ Couldn't parse return fares. Use /snap to see page.")
            else:
                tg_send(chat_id, f"⚠️ Pick 1-{len(flights)}.")
            return

        # ── Outbound fare selection ──
        if phase == "fare_selection":
            fares = state.get("fares", [])
            if 0 <= idx < len(fares):
                state["selected_fare"] = fares[idx]
                state["selected_fare_idx"] = idx
                tg_send(chat_id, f"✈️ {fares[idx]['label']} {fares[idx]['price']}")
                # Fast path: try clicking stored ref
                snap = _try_fast_click(fares[idx]["ref"])
                if not snap:
                    tg_send(chat_id, "Page expired, replaying flow...")
                    snap, err = _replay_expedia(state, "outbound_fare_clicked")
                    if err:
                        tg_send(chat_id, f"⚠️ {err}")
                        return
                if not _show_return_flights(snap):
                    _finish_checkout()
            else:
                tg_send(chat_id, f"⚠️ Pick 1-{len(fares)}.")
            return

        # ── Outbound flight selection (default phase) ──
        flights = state["flights"]
        if 0 <= idx < len(flights):
            fl = flights[idx]
            name = fl.get("airline", "?")
            dep_time = fl.get("dep_time", "?")
            price = fl.get("price", "?")
            state["selected_flight"] = fl

            tg_send(chat_id, f"✅ {name} {dep_time} {price}\nLoading Expedia...")

            snap, err = _replay_expedia(state, "outbound_clicked")
            if err:
                tg_send(chat_id, f"⚠️ {err}")
                return

            fares = _parse_expedia_fares(snap)
            if fares and len(fares) == 1:
                # Auto-click single fare (page is fresh, refs are good)
                state["selected_fare"] = fares[0]
                state["selected_fare_idx"] = 0
                tg_send(chat_id, f"✈️ {fares[0]['label']} {fares[0]['price']} — auto-selecting...")
                snap2 = _try_fast_click(fares[0]["ref"])
                if not snap2:
                    snap2, err2 = _replay_expedia(state, "outbound_fare_clicked")
                    if err2:
                        tg_send(chat_id, f"⚠️ {err2}")
                        return
                if not _show_return_flights(snap2):
                    _finish_checkout()
            elif fares:
                state["phase"] = "fare_selection"
                state["fares"] = fares
                msg = f"✈️ {name} {dep_time} — pick a fare:\n\n"
                for i, f in enumerate(fares, 1):
                    msg += f"{i}. {f['label']}  {f['price']}\n"
                msg += f"\nReply with a number (1-{len(fares)})."
                tg_send(chat_id, msg)
            else:
                tg_send(chat_id, "⚠️ Couldn't parse fares. Use /snap to see page.")
            return
        else:
            tg_send(chat_id, f"⚠️ Invalid number. Pick 1-{len(flights)}.")
            return

    # "more" — re-list flights from last search
    if lower == "more" and chat_id in FLIGHT_RESULTS:
        state = FLIGHT_RESULTS[chat_id]
        flights = state["flights"]
        msg = f"✈️ {state['origin']} → {state['dest']} flights:\n\n"
        for i, fl in enumerate(flights, 1):
            msg += f"{i}. {fl['airline']}  {fl['dep_time']}→{fl['arr_time']}  {fl['duration']}  {fl['stops']}  {fl['price']}\n"
        msg += f"\n📋 Reply with a number (1-{len(flights)}) to select."
        tg_send(chat_id, msg)
        return

    # Fare selection: "book Main Basic", "continue with Classic", "select First"
    fare_match = re.search(r'(main\s*basic|main\s*classic|first\s*classic|basic|classic|first)', lower)
    if fare_match and any(kw in lower for kw in ["book", "continue", "select", "pick", "choose"]):
        fare_name = fare_match.group(1).strip()
        tg_send(chat_id, f"🔍 Looking for {fare_name} fare option...")
        snap = browser_snapshot(limit=500)
        for line in snap.split("\n"):
            s = line.strip()
            if "Continue to book" in s and fare_name.replace(" ", "").lower() in s.replace(" ", "").lower():
                ref_match = re.search(r'\[ref=(\w+)\]', s)
                if ref_match:
                    tg_send(chat_id, f"✅ Clicking Continue with {fare_name}...")
                    try:
                        browser_click(ref_match.group(1))
                    except RuntimeError:
                        browser_js_click(ref_match.group(1))
                    # Wait for Delta tab to open
                    tg_send(chat_id, "✈️ Waiting for Delta checkout to open...")
                    tab_id = _switch_to_new_tab("delta", wait=12)
                    if tab_id:
                        # Poll until Delta page actually loads
                        tg_send(chat_id, "⏳ Delta page loading (this can take 30-60s)...")
                        snap2 = ""
                        for attempt in range(15):
                            time.sleep(4)
                            snap2 = browser_snapshot(limit=500)
                            if any(x in snap2 for x in ["Loading Trip Details", "Loading Passenger", "Loading Payment"]):
                                if attempt == 5:
                                    tg_send(chat_id, "⏳ Still loading...")
                                continue  # Still loading
                            if "delta" in snap2.lower() and len(snap2.strip().split("\n")) < 20:
                                continue  # Minimal content, still loading
                            break
                        # Check for restrictions dialog
                        if "Accept Restrictions" in snap2 or "Continue With Main" in snap2:
                            tg_send(chat_id, "📝 Accepting fare restrictions...")
                            _handle_delta_dialog()
                            time.sleep(5)
                            snap3 = browser_snapshot(limit=800)
                        else:
                            snap3 = snap2
                        # Check if we need to log in
                        if 'button "Log in"' in snap3 or "Log in" in snap3:
                            tg_send(chat_id, "🔑 Delta needs login. Clicking Log In...")
                            for line2 in snap3.split("\n"):
                                s2 = line2.strip()
                                if 'button "Log in"' in s2:
                                    ref2 = re.search(r'\[ref=(\w+)\]', s2)
                                    if ref2:
                                        browser_click(ref2.group(1))
                                        # Wait for login + page reload
                                        tg_send(chat_id, "⏳ Logging in & loading checkout (up to 60s)...")
                                        for i in range(15):
                                            time.sleep(4)
                                            snap3 = browser_snapshot(limit=800)
                                            if any(x in snap3 for x in ["Loading Trip Details", "Loading Passenger", "Loading Payment"]):
                                                if i == 5:
                                                    tg_send(chat_id, "⏳ Still loading...")
                                                continue
                                            if "Express Checkout" in snap3 or "Review and Pay" in snap3 or "Passenger Info" in snap3 or "CVV" in snap3:
                                                break
                                        break
                        # Show Delta checkout page
                        summary = summarize_snapshot(snap3, max_lines=50)
                        tg_send(chat_id, f"🎫 DELTA CHECKOUT\n\n{summary}")
                        # Look for actionable next steps
                        if "Trip Protection" in snap3:
                            tg_send(chat_id, "\n➡️ Say 'no protection' to decline trip insurance")
                        elif "Continue to Review" in snap3:
                            for line2 in snap3.split("\n"):
                                s2 = line2.strip()
                                if "Continue to Review" in s2 and "button" in s2.lower():
                                    ref2 = re.search(r'\[ref=(\w+)\]', s2)
                                    if ref2:
                                        tg_send(chat_id, f"\n➡️ Say 'continue to pay' or /click {ref2.group(1)}")
                    else:
                        tg_send(chat_id, "⚠️ Delta tab didn't open. Try /snap to see current page.")
                    return
        tg_send(chat_id, f"⚠️ Couldn't find {fare_name} fare. Try /snap to see booking options.")
        return

    # PayPal payment: "use paypal", "paypal", "pay with paypal"
    if any(phrase in lower for phrase in ["use paypal", "pay with paypal", "paypal", "pay pal"]):
        tg_send(chat_id, "💰 Selecting PayPal as payment...")
        try:
            # Step 1: Click "Use other forms of payment" to open the modal
            step1 = browser_evaluate("e1", """(el) => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (a.textContent.includes('Use other forms of payment')) {
                        a.scrollIntoView({block: 'center'});
                        a.click();
                        return 'opened modal';
                    }
                }
                return 'no link found';
            }""")
            if "no link" in step1:
                tg_send(chat_id, "⚠️ Can't find 'Use other forms of payment' link. Try /snap.")
                return
            tg_send(chat_id, "📋 Payment modal opened...")
            time.sleep(2)
            # Step 2: Select PayPal radio
            step2 = browser_evaluate("e1", """(el) => {
                const radios = document.querySelectorAll('input[type=radio]');
                for (const r of radios) {
                    const container = r.closest('label, div, li');
                    if (container && container.textContent.includes('PayPal')) {
                        r.click();
                        return 'selected PayPal';
                    }
                }
                return 'no PayPal radio';
            }""")
            if "no PayPal" in step2:
                tg_send(chat_id, "⚠️ Can't find PayPal option in modal. Try /snap.")
                return
            tg_send(chat_id, "✅ PayPal selected, clicking Continue...")
            time.sleep(1)
            # Step 3: Click Continue button in modal
            step3 = browser_evaluate("e1", """(el) => {
                const btn = document.querySelector('button.btn.btn-submit');
                if (btn) { btn.click(); return 'clicked Continue'; }
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.textContent.trim().toUpperCase() === 'CONTINUE') {
                        b.click(); return 'clicked Continue fallback';
                    }
                }
                return 'no Continue button';
            }""")
            if "clicked" in step3:
                tg_send(chat_id, "✅ PayPal payment confirmed!\n\n⏳ PayPal may redirect for authorization.\nOnce done, say 'complete purchase'.")
            else:
                tg_send(chat_id, f"⚠️ Continue button issue: {step3}")
        except RuntimeError as e:
            tg_send(chat_id, f"⚠️ Error: {e}")
        return

    # Skip trip protection: "no protection", "skip protection", "decline protection"
    if any(phrase in lower for phrase in ["no protection", "skip protection", "decline protection", "don't protect", "no trip protection", "no insurance"]):
        tg_send(chat_id, "🛡️ Declining trip protection...")
        snap = browser_snapshot(limit=800)
        for line in snap.split("\n"):
            s = line.strip()
            if "Trip Protection No" in s and "radio" in s.lower():
                ref_match = re.search(r'\[ref=(\w+)\]', s)
                if ref_match:
                    try:
                        browser_click(ref_match.group(1))
                    except RuntimeError:
                        browser_js_click(ref_match.group(1))
                    time.sleep(2)
                    tg_send(chat_id, "✅ Trip protection declined.\n\n� Pay with PayPal: say 'use paypal'\n🔒 Or pay with card: /cvv <your-3-digit-code>")
                    return
        tg_send(chat_id, "⚠️ Couldn't find trip protection option. Try /snap.")
        return

    # Confirm purchase (safety gate) — must be checked BEFORE "complete purchase"
    if "confirm purchase" in lower:
        tg_send(chat_id, "🔥 Completing purchase NOW...")
        result = _cdp_eval("""
        (() => {
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            }
            function hardClick(el) {
                if (!el) return false;
                el.scrollIntoView({block: 'center'});
                const rect = el.getBoundingClientRect();
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                const target = document.elementFromPoint(x, y) || el;
                for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    target.dispatchEvent(new MouseEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: x,
                        clientY: y
                    }));
                }
                target.focus?.();
                if (typeof el.click === 'function') el.click();
                return true;
            }
            window.scrollTo(0, document.body.scrollHeight);
            const btns = [...document.querySelectorAll('button')];
            for (const b of btns) {
                const txt = (b.textContent || '').replace(/\s+/g, ' ').trim();
                if (isVisible(b) && (txt.includes('Complete Booking') || txt.includes('Complete Purchase'))) {
                    hardClick(b);
                    return 'clicked: ' + txt;
                }
            }
            return 'not found';
        })()
        """)
        if "clicked" in str(result):
            time.sleep(8)
            try:
                snap2 = browser_snapshot(limit=300)
                summary = summarize_snapshot(snap2, max_lines=50)
                tg_send(chat_id, f"🎉 Purchase submitted!\n\n{summary}")
            except RuntimeError:
                tg_send(chat_id, f"🎉 Purchase button clicked ({result})! Page is loading...\nUse /wait to check status.")
            return
        tg_send(chat_id, f"⚠️ Couldn't find the purchase button via CDP: {result}")
        return

    # Complete purchase: "complete purchase", "buy now", "purchase", "submit order"
    if any(phrase in lower for phrase in ["complete purchase", "buy now", "purchase flight", "submit order", "finalize", "place order"]):
        tg_send(chat_id, "💳 Looking for final booking button...")
        result = _cdp_eval("""
        (() => {
            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            }
            window.scrollTo(0, document.body.scrollHeight);
            const btns = [...document.querySelectorAll('button')];
            for (const b of btns) {
                const txt = (b.textContent || '').replace(/\s+/g, ' ').trim();
                if (isVisible(b) && (txt.includes('Complete Booking') || txt.includes('Complete Purchase'))) {
                    b.scrollIntoView({block: 'center'});
                    return 'found: ' + txt;
                }
            }
            return 'not found';
        })()
        """)
        if "found" in str(result):
            tg_send(chat_id, f"⚠️ Found Purchase button ({result}). This will charge your card.\n\n⚡ Reply 'confirm purchase' to proceed or 'cancel' to stop.")
            return
        tg_send(chat_id, f"⚠️ No purchase button found via CDP. Make sure checkout is fully loaded.\nDetails: {result}")
        return

    # "continue to pay" / "review and pay"
    if any(phrase in lower for phrase in ["continue to pay", "review and pay", "continue to review", "proceed to pay"]):
        tg_send(chat_id, "💳 Continuing to Review & Pay...")
        snap = browser_snapshot(limit=500)
        for line in snap.split("\n"):
            s = line.strip()
            if ("Continue to Review" in s or "Review & Pay" in s) and "button" in s.lower():
                ref_match = re.search(r'\[ref=(\w+)\]', s)
                if ref_match:
                    try:
                        browser_click(ref_match.group(1))
                    except RuntimeError:
                        browser_js_click(ref_match.group(1))
                    time.sleep(5)
                    snap2 = browser_snapshot(limit=500)
                    summary = summarize_snapshot(snap2, max_lines=50)
                    tg_send(chat_id, f"💳 Payment page:\n\n{summary}")
                    return
        tg_send(chat_id, "⚠️ Couldn't find the Continue button. Try /snap.")
        return

    # Flight selection: "select/book the 1:25 PM Delta" or "book 1:25 pm"
    time_match = re.search(r'(\d{1,2}:\d{2}\s*[APap][Mm])', text)
    if time_match and any(kw in lower for kw in ["select", "book", "pick", "choose", "click"]):
        target_time = time_match.group(1).strip().upper()
        tg_send(chat_id, f"🔍 Looking for the {target_time} flight...")
        snap = browser_snapshot(limit=500)
        flights = _extract_flights(snap)
        found = False
        for f in flights:
            if target_time.replace(" ", "") in f["depart_time"].replace(" ", "").upper():
                # Use JS click — browser_click fails on Google Flights links
                try:
                    browser_js_click(f["ref"])
                    found = True
                    tg_send(chat_id, f"✅ Selected: {f['summary']} ({f['price']})")
                    time.sleep(4)
                    snap2 = browser_snapshot(limit=500)
                    # Check if we're on booking page or return flights
                    booking = _parse_booking_page(snap2)
                    if booking:
                        tg_send(chat_id, booking)
                    else:
                        flights2 = _extract_flights(snap2)
                        if flights2:
                            msg = "Now choose your return flight:\n\n"
                            for i, f2 in enumerate(flights2, 1):
                                msg += f"{i}. {f2['summary']}\n   💰 {f2['price']}  |  select the {f2['depart_time']}\n\n"
                            tg_send(chat_id, msg)
                        else:
                            summary = summarize_snapshot(snap2, max_lines=40)
                            tg_send(chat_id, f"📋 Page now shows:\n\n{summary}")
                except RuntimeError as e:
                    tg_send(chat_id, f"⚠️ Click failed: {e}")
                    # Retry with fresh snapshot
                    snap = browser_snapshot(limit=500)
                    flights2 = _extract_flights(snap)
                    for f2 in flights2:
                        if target_time.replace(" ", "") in f2["depart_time"].replace(" ", "").upper():
                            browser_js_click(f2["ref"])
                            found = True
                            tg_send(chat_id, f"✅ Selected: {f2['summary']} ({f2['price']})")
                            break
                break
        if not found:
            tg_send(chat_id, f"⚠️ Couldn't find a flight departing at {target_time}. Try /snap to see the page.")
        return

    # Flight search pattern: "SFO to SLC march 13 to march 16"
    fly_match = re.match(
        r'([A-Z]{3})\s+to\s+([A-Z]{3})\s+(.+)',
        text.strip(), re.IGNORECASE
    )
    if fly_match:
        origin = fly_match.group(1).upper()
        dest = fly_match.group(2).upper()
        date_str = fly_match.group(3).strip()
        # Normalize "march 13th to march 16th" → "Mar 13 Mar 16"
        date_str = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)  # remove ordinals
        date_str = re.sub(r'\s+to\s+', ' ', date_str)  # remove "to"
        date_str = re.sub(r'\s*-+\s*', ' ', date_str)  # remove dashes
        handle_fly(chat_id, f"{origin} {dest} {date_str}")
        return

    # "search SFO to SLC" pattern
    fly_match2 = re.match(
        r'(?:search|find|look for|book)\s+(?:flights?\s+)?(?:from\s+)?([A-Z]{3})\s+to\s+([A-Z]{3})(?:\s+(.+))?',
        text.strip(), re.IGNORECASE
    )
    if fly_match2:
        origin = fly_match2.group(1).upper()
        dest = fly_match2.group(2).upper()
        dates = fly_match2.group(3) or ""
        dates = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', dates)
        dates = re.sub(r'\s+to\s+', ' ', dates)
        handle_fly(chat_id, f"{origin} {dest} {dates}".strip())
        return

    # Navigation
    if lower.startswith("go to ") or lower.startswith("open ") or lower.startswith("navigate to "):
        url = re.sub(r'^(go to|open|navigate to)\s+', '', lower).strip()
        handle_go(chat_id, url)
        return

    # Snapshot requests
    if any(p in lower for p in ["what do you see", "what's on", "show me", "describe", "snapshot", "look at"]):
        handle_snap(chat_id, "")
        return

    # Screenshot
    if "screenshot" in lower or "picture" in lower or "photo" in lower:
        handle_screenshot(chat_id, "")
        return

    # Click
    m = re.match(r'click\s+(\w+)', lower)
    if m:
        handle_click(chat_id, m.group(1))
        return

    # Search flights
    if "search" in lower and ("flight" in lower or "google" in lower):
        handle_search(chat_id, "")
        return

    # Default
    tg_send(chat_id,
            f"🤔 Try something like:\n"
            f"• SFO to SLC March 13 to March 16\n"
            f"• /fly SFO SLC Mar 13 Mar 16\n"
            f"• /snap — see the current page\n"
            f"• /screenshot — get a picture\n"
            f"• /help — all commands")


# ── Main loop ───────────────────────────────────────────────────────────────

COMMANDS = {
    "/go": handle_go,
    "/nav": handle_go,
    "/navigate": handle_go,
    "/snap": handle_snap,
    "/snapshot": handle_snap,
    "/look": handle_snap,
    "/click": handle_click,
    "/type": handle_type,
    "/fill": handle_fill,
    "/screenshot": handle_screenshot,
    "/ss": handle_screenshot,
    "/cvv": handle_cvv,
    "/reload": handle_reload,
    "/refresh": handle_reload,
    "/wait": handle_wait,
    "/search": handle_search,
    "/fly": handle_fly,
    "/book": handle_book,
    "/pay": handle_pay,
    "/card": handle_pay,
    "/help": handle_help,
    "/start": handle_help,
    "/checkout": lambda chat_id, args: _handle_checkout_from_current(chat_id),
    "/seats": lambda chat_id, args: _handle_seats_from_current(chat_id),
}


def process_message(msg):
    chat_id = msg["chat"]["id"]

    # Security: only respond to allowed user
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        tg_send(chat_id, "⛔ Unauthorized. This bot only responds to its owner.")
        return

    text = msg.get("text", "").strip()
    if not text:
        return

    # Check for commands
    for cmd, handler in COMMANDS.items():
        if text.lower().startswith(cmd):
            args = text[len(cmd):].strip()
            try:
                handler(chat_id, args)
            except Exception as e:
                tg_send(chat_id, f"❌ Error: {e}\n\n{traceback.format_exc()[-500:]}")
            return

    # Natural language fallback
    try:
        handle_natural(chat_id, text)
    except Exception as e:
        tg_send(chat_id, f"❌ Error: {e}")


def main():
    if not BOT_TOKEN:
        print("ERROR: Set FLIGHT_BOT_TOKEN environment variable")
        print("  1. Message @BotFather on Telegram")
        print("  2. Send /newbot, pick a name and username")
        print("  3. Copy the token")
        print("  4. export FLIGHT_BOT_TOKEN='your-token-here'")
        sys.exit(1)

    print(f"✈️  Flight Bot starting...")
    print(f"   Gateway: {GW_URL}")
    print(f"   Token loaded: {bool(GW_TOKEN)}")
    if ALLOWED_CHAT_ID:
        print(f"   Allowed chat: {ALLOWED_CHAT_ID}")
    else:
        print(f"   ⚠️  No FLIGHT_BOT_CHAT_ID set — will respond to anyone!")
        print(f"   Set FLIGHT_BOT_CHAT_ID to your Telegram user ID for security.")

    # Ensure browser is running
    try:
        status = browser_cmd("status", "--json")
        print(f"   Browser status: {status[:100]}")
    except:
        print("   Starting browser...")
        browser_cmd("start")

    print(f"\n✈️  Bot is running! Send /help to your bot on Telegram.\n")

    while True:
        try:
            updates = tg_get_updates(timeout=30)
            for update in updates:
                msg = update.get("message")
                if msg:
                    process_message(msg)
        except KeyboardInterrupt:
            print("\n✈️  Bot stopped.")
            break
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
