#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import requests
import websocket


def _load_gateway():
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    if not cfg_path.exists():
        return "", ""
    cfg = json.loads(cfg_path.read_text())
    token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
    port = cfg.get("gateway", {}).get("port", 18789)
    return token, f"ws://127.0.0.1:{port}"


def _load_local_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()
GW_TOKEN, GW_URL = _load_gateway()


def _env_int(name, default=0):
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        print(f"WARNING: {name} must be numeric, got {raw!r}. Using {default}.")
        return default


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name, default=0):
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = raw.strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


BOT_TOKEN = os.environ.get("EMMYCAR_BOT_TOKEN", "")
ALLOWED_CHAT_ID = _env_int("EMMYCAR_BOT_CHAT_ID", 0)
HOME_ADDRESS = os.environ.get("EMMYCAR_HOME_ADDRESS", "2835 Clay Street, San Francisco, CA 94115")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET = 0
BROWSER_PROFILE = os.environ.get("EMMYCAR_BROWSER_PROFILE", "openclaw-cars")
BROWSER_CDP_PORT = _env_int("EMMYCAR_BROWSER_CDP_PORT", 18801)
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw") or "/opt/homebrew/bin/openclaw"
RIDE_STATE = {}
AIRPORTS = {
    "SFO": "San Francisco International Airport",
    "OAK": "Oakland International Airport",
    "SJC": "San Jose Mineta International Airport",
}
AIRPORT_SYNONYMS = {
    "sfo airport": AIRPORTS["SFO"],
    "san francisco airport": AIRPORTS["SFO"],
    "san francisco international": AIRPORTS["SFO"],
    "san francisco international airport": AIRPORTS["SFO"],
    "oak airport": AIRPORTS["OAK"],
    "oakland airport": AIRPORTS["OAK"],
    "oakland international": AIRPORTS["OAK"],
    "oakland international airport": AIRPORTS["OAK"],
    "sjc airport": AIRPORTS["SJC"],
    "san jose airport": AIRPORTS["SJC"],
    "san jose international": AIRPORTS["SJC"],
    "san jose mineta international airport": AIRPORTS["SJC"],
}
PRODUCT_ALIASES = {
    "uberx": "UberX",
    "uber x": "UberX",
    "x": "UberX",
    "comfort": "Comfort",
    "uber comfort": "Comfort",
    "black": "Black",
    "uber black": "Black",
    "xl": "UberXL",
    "uberxl": "UberXL",
    "uber xl": "UberXL",
    "green": "Uber Green",
    "uber green": "Uber Green",
}
NO_WORDS = {"cancel", "stop", "no", "n", "never mind", "nevermind"}
YES_WORDS = {"yes", "y", "ok", "okay", "confirm", "do it", "send it", "request it"}


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
        requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text[i:i + 4000]}, timeout=15)


def tg_send_photo(chat_id, photo_path, caption=""):
    with open(photo_path, "rb") as f:
        requests.post(
            f"{API}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"photo": f},
            timeout=30,
        )


def browser_cmd(*args, timeout=60, retries=1):
    cmd = [OPENCLAW_BIN, "browser"] + list(args) + ["--browser-profile", BROWSER_PROFILE]
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


def _list_tabs():
    import urllib.request

    try:
        data = urllib.request.urlopen(f"http://127.0.0.1:{BROWSER_CDP_PORT}/json/list", timeout=5).read()
        tabs = json.loads(data)
        return [t for t in tabs if t.get("type") == "page"]
    except Exception:
        return []


def _open_blank_tab():
    import urllib.request

    for url in [
        f"http://127.0.0.1:{BROWSER_CDP_PORT}/json/new?about:blank",
        f"http://127.0.0.1:{BROWSER_CDP_PORT}/json/new",
    ]:
        try:
            req = urllib.request.Request(url, method="PUT")
            with urllib.request.urlopen(req, timeout=5) as response:
                payload = response.read().decode("utf-8", errors="ignore")
            if payload:
                return True
        except Exception:
            continue
    return False


def _ensure_browser_page(timeout_seconds=10):
    if _list_tabs():
        return True
    try:
        browser_cmd("start")
    except RuntimeError:
        pass
    try:
        _open_blank_tab()
    except Exception:
        pass
    end_time = time.time() + timeout_seconds
    while time.time() < end_time:
        if _list_tabs():
            return True
        try:
            _open_blank_tab()
        except Exception:
            pass
        time.sleep(1)
    return False


def browser_navigate(url):
    if not _ensure_browser_page():
        raise RuntimeError("browser navigate failed: Error: No pages available in the connected browser")
    try:
        return browser_cmd("navigate", url)
    except RuntimeError as err:
        if "frame was detached" not in str(err).lower():
            raise
        time.sleep(1)
        if not _ensure_browser_page():
            raise
        return browser_cmd("navigate", url)


def browser_screenshot():
    raw = browser_cmd("screenshot", "--json", timeout=20)
    for line in raw.splitlines():
        match = re.search(r"(/.+\.(?:png|jpg|jpeg))", line)
        if match:
            return match.group(1)
    try:
        data = json.loads(raw)
        return data.get("path", data.get("file", ""))
    except Exception:
        return ""


def browser_snapshot(limit=250):
    raw = browser_cmd("snapshot", "--labels", "--limit", str(limit), timeout=90, retries=2)
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("- RootWebArea") or s.startswith("- root"):
            return "\n".join(lines[i:])
    clean = []
    for line in lines:
        s = line.strip()
        if any(c in s for c in "│├╮╯◇─") or "Doctor warnings" in s or s == "":
            continue
        clean.append(line)
    return "\n".join(clean)


def browser_focus(target_id):
    return browser_cmd("focus", target_id)


def browser_click(ref):
    return browser_cmd("click", ref)


def _uber_tab_score(tab):
    url = (tab.get("url") or "").lower()
    title = (tab.get("title") or "").lower()
    hay = f"{title} {url}"
    if "m.uber.com/go/" in url:
        return 100
    if "m.uber.com" in url:
        return 80
    if "uber.com" in url and any(token in url for token in ["/go/", "/ride", "/product-selection"]):
        return 60
    if "uber" in title and "docs.google.com" not in url:
        return 20
    if "uber" in hay and "docs.google.com" not in url:
        return 10
    return -1


def _cdp_eval(js_code, timeout=20):
    try:
        tabs = _list_tabs()
        if not tabs:
            return "error: no browser tabs found"
        target = max(tabs, key=_uber_tab_score)
        ws_url = target.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return "error: no webSocketDebuggerUrl"
        ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": js_code, "returnByValue": True, "awaitPromise": True},
        }))
        raw = ws.recv()
        ws.close()
        resp = json.loads(raw)
        result = resp.get("result", {}).get("result", {})
        if result.get("type") == "string":
            return result["value"]
        if result.get("type") == "undefined":
            return "undefined"
        if "value" in result:
            return str(result["value"])
        return json.dumps(result)
    except Exception as e:
        return f"error: {e}"


def _switch_to_uber_tab(wait=8):
    for _ in range(wait):
        tabs = _list_tabs()
        uber_tabs = [tab for tab in tabs if _uber_tab_score(tab) >= 0]
        if uber_tabs:
            target = max(uber_tabs, key=_uber_tab_score)
            browser_focus(target["id"])
            return target["id"]
        time.sleep(1)
    return None


def _build_uber_url(origin, destination):
    return "https://m.uber.com/go/home"


def _normalize_place(value):
    text = value.strip()
    upper = text.upper()
    if upper in AIRPORTS:
        return AIRPORTS[upper]
    lower = text.lower()
    if lower in {"home", "my house", "house"}:
        return HOME_ADDRESS
    lowered = re.sub(r"[,.]", "", lower)
    if any(token in lowered for token in ["2835 clay", "clay street san francisco", "2835 clay street san francisco", "2835 clay st san francisco"]):
        return HOME_ADDRESS
    if lowered in AIRPORT_SYNONYMS:
        return AIRPORT_SYNONYMS[lowered]
    return text


def _parse_request(text):
    raw = text.strip()
    lowered_raw = raw.lower()
    product = None
    if re.search(r"\b(?:uber\s*xl|uberxl|xl)\b", lowered_raw):
        product = "UberXL"
    elif re.search(r"\b(?:uber\s*x|uberx|x)\b", lowered_raw):
        product = "UberX"
    raw = re.sub(r"^[\s,;:.-]+|[\s,;:.-]+$", "", raw)
    raw = re.sub(
        r"^(please\s+)?(?:call|get|book|order|request)\s+(me\s+)?(an?\s+)?(?:uber(?:\s+(?:x|xl|comfort|black|green))?|uberx|uberxl)\s+",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    raw = re.sub(r"^(please\s+)?call\s+(me\s+)?a\s+ride\s+", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^(please\s+)?(?:uber(?:\s+(?:x|xl|comfort|black|green))?|uberx|uberxl)\s+", "", raw, flags=re.IGNORECASE)
    explicit_match = re.match(r"^from\s+(.+?)\s+(?:to|->)\s+(.+)$", raw, flags=re.IGNORECASE)
    if explicit_match:
        origin = _normalize_place(explicit_match.group(1))
        destination = _normalize_place(explicit_match.group(2))
        return {"origin": origin, "destination": destination, "product": product or "UberX"}
    match = re.match(r"(.+?)\s+(?:to|->)\s+(.+)$", raw, flags=re.IGNORECASE)
    if not match:
        return None
    origin = _normalize_place(match.group(1))
    destination = _normalize_place(match.group(2))
    return {"origin": origin, "destination": destination, "product": product or "UberX"}


def _drive_uber_location_flow_js(origin, destination):
    origin_json = json.dumps(origin)
    destination_json = json.dumps(destination)
    return _cdp_eval(
        f"""
(async () => {{
  const PICKUP = {origin_json};
  const DEST = {destination_json};
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();

  const visible = (el) => {{
    if (!el) return false;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  }};

  const textInputs = () =>
    [...document.querySelectorAll('input, textarea')].filter(el => {{
      if (!visible(el)) return false;
      const type = (el.getAttribute('type') || '').toLowerCase();
      return !['hidden', 'checkbox', 'radio', 'submit', 'button'].includes(type);
    }});

  const findLabeledActivator = (labelText) => {{
    const label = labelText.toLowerCase();
    const nodes = [...document.querySelectorAll('button, [role="button"], div, a, label')].filter(visible);
    return nodes.find(el => norm(el.innerText).includes(label)) || null;
  }};

  const findActionButton = (labelText) => {{
    const label = labelText.toLowerCase();
    const nodes = [...document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')].filter(visible);
    return nodes.find(el => {{
      const text = norm(el.innerText || el.value || el.getAttribute('aria-label') || '');
      return text === label || text.includes(label);
    }}) || null;
  }};

  const clickish = (el) => {{
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    el.scrollIntoView?.({{ block: 'center' }});
    el.focus?.();
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {{
      el.dispatchEvent(new MouseEvent(type, {{
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX: x,
        clientY: y,
        view: window
      }}));
    }});
    el.click?.();
    return true;
  }};

  const setValue = (el, value) => {{
    if (!el) return false;
    const proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    el.focus?.();
    if (setter) setter.call(el, value);
    else el.value = value;
    el.setSelectionRange?.(el.value.length, el.value.length);
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return true;
  }};

  const pressEnter = (el) => {{
    if (!el) return false;
    el.focus?.();
    ['keydown', 'keypress', 'keyup'].forEach(type => {{
      el.dispatchEvent(new KeyboardEvent(type, {{
        key: 'Enter',
        code: 'Enter',
        keyCode: 13,
        which: 13,
        bubbles: true,
        cancelable: true,
      }}));
    }});
    return true;
  }};

  const neutralClick = () => {{
    const target =
      document.querySelector('h1') ||
      document.querySelector('[role="heading"]') ||
      document.querySelector('header') ||
      document.querySelector('main') ||
      document.body;
    return clickish(target);
  }};

  const topVisibleOption = () => {{
    const options = [...document.querySelectorAll('li[role="option"]')].filter(visible);
    return options.find(el => {{
      const text = norm(el.innerText);
      return text && !text.includes('see more results');
    }}) || null;
  }};

  const matchingVisibleOption = (query) => {{
    const q = norm(query);
    const tokens = q.split(/\s+/).filter(token => token.length >= 3);
    const options = [...document.querySelectorAll('li[role="option"]')].filter(visible);
    for (const el of options) {{
      const text = norm(el.innerText);
      if (!text || text.includes('see more results')) continue;
      if (q && (text.includes(q) || q.includes(text))) return el;
      const hits = tokens.filter(token => text.includes(token)).length;
      if (tokens.length && hits >= Math.max(2, Math.ceil(tokens.length * 0.6))) return el;
    }}
    return null;
  }};

  const pickupActivator =
    document.querySelector('[data-testid="pudo-button-pickup"]') ||
    document.querySelector('[data-testid="enhancer-container-pickup"]') ||
    document.querySelectorAll('[data-testid="pudo-input-v2"]')[0] ||
    findLabeledActivator('pickup location') ||
    document.querySelector('input[type="search"]') ||
    textInputs()[0] ||
    null;

  const dropoffActivator =
    document.querySelector('[data-testid="pudo-button-drop0"]') ||
    document.querySelector('[data-testid="enhancer-container-drop0"]') ||
    document.querySelectorAll('[data-testid="pudo-input-v2"]')[1] ||
    findLabeledActivator('dropoff location') ||
    null;

  if (!pickupActivator) return {{ error: 'no-pickup-activator' }};

  const steps = [];

  if (dropoffActivator) {{
    clickish(dropoffActivator);
    steps.push('dropoff-preclicked');
    await sleep(300);
  }} else {{
    steps.push('dropoff-preclick-missing');
  }}

  clickish(pickupActivator);
  steps.push('pickup-activated');
  await sleep(700);

  let inputs = textInputs();
  if (!inputs.length) {{
    return {{ error: 'no-inputs-after-pickup-activate', steps }};
  }}

  const pickupInput =
    inputs.find(el => norm(el.getAttribute('placeholder')).includes('pickup')) ||
    inputs.find(el => norm(el.getAttribute('aria-label')).includes('pickup')) ||
    inputs[0];

  setValue(pickupInput, PICKUP);
  steps.push('pickup-filled');
  await sleep(700);
  let pickupOption = matchingVisibleOption(PICKUP) || topVisibleOption();
  if (pickupOption) {{
    clickish(pickupOption);
    steps.push('pickup-option-first-click');
    await sleep(350);
    pickupOption = matchingVisibleOption(PICKUP) || topVisibleOption() || pickupOption;
    clickish(pickupOption);
    steps.push('pickup-option-second-click');
    await sleep(1200);
  }} else {{
    pressEnter(pickupInput);
    steps.push('pickup-enter-fallback');
    await sleep(1200);
  }}
  neutralClick();
  steps.push('pickup-committed-after-selection');
  await sleep(400);

  inputs = textInputs();

  if (inputs.length < 2 && dropoffActivator) {{
    clickish(dropoffActivator);
    steps.push('dropoff-materialized-click');
    await sleep(700);
    inputs = textInputs();
  }}

  if (inputs.length < 2) {{
    return {{ error: 'still-need-two-inputs', steps, inputCount: inputs.length }};
  }}

  let dropoffInput =
    inputs.find(el => norm(el.getAttribute('placeholder')).includes('dropoff')) ||
    inputs.find(el => norm(el.getAttribute('aria-label')).includes('dropoff')) ||
    inputs[1];
  clickish(dropoffInput);
  steps.push('dropoff-selected-before-fill');
  await sleep(120);

  setValue(dropoffInput, DEST);
  steps.push('dropoff-filled');
  await sleep(700);

  neutralClick();
  steps.push('neutral-clicked');
  await sleep(250);

  inputs = textInputs();
  dropoffInput =
    inputs.find(el => norm(el.getAttribute('placeholder')).includes('dropoff')) ||
    inputs.find(el => norm(el.getAttribute('aria-label')).includes('dropoff')) ||
    inputs[1] ||
    dropoffInput;

  clickish(dropoffInput);
  await sleep(150);
  dropoffInput.focus?.();
  dropoffInput.setSelectionRange?.(0, dropoffInput.value.length);
  steps.push('dropoff-reselected');
  await sleep(700);

  let option = matchingVisibleOption(DEST) || topVisibleOption();
  if (!option) {{
    return {{
      error: 'no-top-option-before-double-click',
      steps,
      pickupValue: pickupInput.value,
      dropoffValue: dropoffInput.value
    }};
  }}

  const optionText = clean(option.innerText);
  clickish(option);
  steps.push('top-option-first-click');
  await sleep(350);

  option = matchingVisibleOption(DEST) || topVisibleOption() || option;
  clickish(option);
  steps.push('top-option-second-click');
  await sleep(1800);

  const searchButton =
    document.querySelector('[data-testid="confirm-button"]') ||
    document.querySelector('[data-testid="search-button"]') ||
    findActionButton('search');
  if (searchButton) {{
    clickish(searchButton);
    steps.push('search-clicked');
    await sleep(1800);
  }} else {{
    steps.push('search-button-missing');
  }}

  const body = clean(document.body.innerText);

  return {{
    steps,
    optionText,
    pickupValue: pickupInput.value,
    dropoffValue: dropoffInput.value,
    state: {{
      url: location.href,
      hasAirline: /which airline|search by airline name/i.test(body),
      hasChooseRide: /choose a ride|rides we think you'll like/i.test(body),
      hasGetRide: /get a ride|plan your ride/i.test(body),
      body: body.slice(0, 400)
    }}
  }};
}})()
""",
        timeout=20,
    )


def _open_uber_page_for_js(origin, destination):
    url = _build_uber_url(origin, destination)
    browser_navigate(url)
    _switch_to_uber_tab(wait=5)
    time.sleep(6)
    return url


def _run_exact_uber_page_js(origin, destination):
    result = _drive_uber_location_flow_js(origin, destination)
    time.sleep(2)
    shot = browser_screenshot()
    return result, shot


def _read_ride_options_js():
    return _cdp_eval(
        """
(() => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const rows = [...document.querySelectorAll('button, [role="button"], li, article, div')].filter(visible);
  const options = [];
  for (const row of rows) {
    const text = clean(row.innerText);
    if (!text) continue;
    const priceMatch = text.match(/\$\s?\d+(?:\.\d{2})?/);
    if (!priceMatch) continue;
    const lines = text.split('\n').map(clean).filter(Boolean);
    const product = lines.find(line => /^[A-Za-z][A-Za-z0-9+\-\s]{1,30}$/.test(line) && !line.startsWith('$')) || lines[0];
    const price = priceMatch[0].replace(/\s+/g, '');
    if (!product || !price) continue;
    if (norm(product).includes('leave now')) continue;
    const key = norm(product) + '|' + price;
    if (options.some(option => option.key === key)) continue;
    options.push({
      key,
      product: clean(product),
      price,
      text,
      selected: /selected|faster|recommended/i.test(text),
    });
  }
  const requestButton = [...document.querySelectorAll('button, [role="button"], a')].filter(visible).map(el => clean(el.innerText)).find(text => /^(request|confirm|reserve)\b/i.test(text)) || '';
  const body = clean(document.body.innerText);
  return JSON.stringify({
    options: options.slice(0, 12),
    requestButton,
    hasChooseRide: /choose a ride|rides we think you'll like/i.test(body),
    body: body.slice(0, 400),
  });
})()
""",
        timeout=15,
    )


def _select_product_and_confirm_js(product_name):
    product_json = json.dumps(product_name)
    return _cdp_eval(
        f"""
(() => {{
  const TARGET = {product_json};
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
  const visible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const clickish = (el) => {{
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    el.scrollIntoView?.({{ block: 'center' }});
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
      el.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window }}));
    }}
    el.click?.();
    return true;
  }};
  const rows = [...document.querySelectorAll('button, [role="button"], li, article, div')].filter(visible);
  let chosenText = '';
  let rowTarget = null;
  for (const row of rows) {{
    const text = clean(row.innerText);
    if (!text || !/\$\s?\d+(?:\.\d{{2}})?/.test(text)) continue;
    if (norm(text).includes(norm(TARGET))) {{
      rowTarget = row.closest('button, [role="button"], li, article, div') || row;
      chosenText = text;
      break;
    }}
  }}
  if (rowTarget) {{
    clickish(rowTarget);
  }}
  const buttons = [...document.querySelectorAll('button, [role="button"], a')].filter(visible);
  const request = buttons.find(el => {{
    const text = norm(el.innerText || el.getAttribute('aria-label') || '');
    return (text.startsWith('request') || text.startsWith('confirm') || text.startsWith('reserve')) && (text.includes(norm(TARGET)) || text.includes('uber'));
  }}) || buttons.find(el => {{
    const text = norm(el.innerText || el.getAttribute('aria-label') || '');
    return text.startsWith('request') || text.startsWith('confirm') || text.startsWith('reserve');
  }});
  if (!request) return JSON.stringify({{ error: 'request-button-not-found', chosenText }});
  const requestText = clean(request.innerText || request.getAttribute('aria-label') || '');
  clickish(request);
  return JSON.stringify({{ chosenText, requestText, clicked: true }});
}})()
""",
        timeout=15,
    )


def _parse_jsonish(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {"raw": value}
    try:
        return json.loads(value)
    except Exception:
        return {"raw": value}


def _parse_snapshot_ride_options(snapshot):
    options = []
    for line in snapshot.splitlines():
        s = line.strip()
        if not s.startswith("- option "):
            continue
        ref_match = re.search(r"\[ref=(\w+)\]", s)
        price_matches = re.findall(r"\$\d+(?:\.\d{2})?", s)
        if not ref_match or not price_matches:
            continue
        label_match = re.search(r'- option "([^"]+)"', s)
        if not label_match:
            continue
        label = label_match.group(1)
        product_match = re.match(r'([A-Za-z][A-Za-z0-9+\- ]{1,30}?)Person\d', label)
        if not product_match:
            product_match = re.match(r'([A-Za-z][A-Za-z0-9+\- ]{1,30}?)\s+\d+\s+mins?\s+away', label)
        product = (product_match.group(1) if product_match else label.split("$")[0]).strip()
        if not product or product.lower() in {"popular", "economy", "premium", "more"}:
            continue
        options.append({
            "ref": ref_match.group(1),
            "product": product,
            "price": price_matches[0],
            "text": label,
            "selected": False,
        })
    deduped = []
    seen = set()
    for option in options:
        key = (option["product"].lower(), option["price"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(option)
    return deduped


def _find_snapshot_request_button(snapshot, product_name):
    target = (product_name or "").strip().lower()
    fallback = None
    for line in snapshot.splitlines():
        s = line.strip()
        if not s.startswith("- button "):
            continue
        ref_match = re.search(r"\[ref=(\w+)\]", s)
        label_match = re.search(r'- button "([^"]+)"', s)
        if not ref_match or not label_match:
            continue
        label = label_match.group(1).strip()
        lowered = label.lower()
        if not re.match(r"^(request|confirm|reserve)\b", lowered):
            continue
        if target and target in lowered:
            return ref_match.group(1), label
        fallback = (ref_match.group(1), label)
    return fallback or (None, None)


def _find_snapshot_button_ref(snapshot, button_text):
    target = (button_text or "").strip().lower()
    for line in snapshot.splitlines():
        s = line.strip()
        if not s.startswith("- button "):
            continue
        ref_match = re.search(r"\[ref=(\w+)\]", s)
        label_match = re.search(r'- button "([^"]+)"', s)
        if not ref_match or not label_match:
            continue
        label = label_match.group(1).strip().lower()
        if label == target or label.startswith(target + " ") or target in label:
            return ref_match.group(1)
    return None


def _request_button_matches_product(snapshot, product_name):
    _ref, label = _find_snapshot_request_button(snapshot, product_name)
    if not label:
        return False
    return (product_name or "").strip().lower() in label.lower()


def _find_option_ref_for_text(snapshot, product_name):
    target = (product_name or "").strip().lower()
    exact_ref = None
    fallback_ref = None
    for line in snapshot.splitlines():
        s = line.strip()
        if not s.startswith("- option "):
            continue
        ref_match = re.search(r"\[ref=(\w+)\]", s)
        label_match = re.search(r'- option "([^"]+)"', s)
        if not ref_match or not label_match:
            continue
        ref = ref_match.group(1)
        label = label_match.group(1).strip().lower()
        if target == "uberx" and ("uberxperson" in label or label.startswith("uberx ") or label == "uberx"):
            exact_ref = ref
            break
        if target and target in label:
            fallback_ref = fallback_ref or ref
    return exact_ref or fallback_ref


def _select_product(product_name):
    snap = browser_snapshot(limit=320)
    option_ref = _find_option_ref_for_text(snap, product_name)
    result = None
    if option_ref:
        try:
            browser_click(option_ref)
            result = f"clicked-ref:{option_ref}"
        except RuntimeError:
            result = None
    if not result:
        result = str(_select_product_and_confirm_js(product_name))
    for _ in range(8):
        time.sleep(1)
        snap = browser_snapshot(limit=320)
        lowered = snap.lower()
        if "price breakdown" in lowered:
            close_ref = _find_snapshot_button_ref(snap, "Close")
            if close_ref:
                try:
                    browser_click(close_ref)
                    time.sleep(1)
                    snap = browser_snapshot(limit=320)
                    option_ref = _find_option_ref_for_text(snap, product_name)
                    if option_ref:
                        try:
                            browser_click(option_ref)
                            result = f"clicked-ref:{option_ref}:after-close"
                        except RuntimeError:
                            pass
                except RuntimeError:
                    pass
        if _request_button_matches_product(snap, product_name):
            return result, snap
    return result, snap


def _prepare_confirmation_state(chat_id):
    state = RIDE_STATE[chat_id]
    selected_product = state.get("product") or "UberX"
    selection_result, snapshot = _select_product(selected_product)
    options = _parse_snapshot_ride_options(snapshot)
    request_ref, request_label = _find_snapshot_request_button(snapshot, selected_product)
    state["phase"] = "awaiting_confirmation"
    state["fares"] = options
    state["request_ref"] = request_ref
    state["request_label"] = request_label
    state["ready_to_submit"] = bool(request_ref)
    return {
        "selected_product": selected_product,
        "selection": selection_result,
        "options": options,
        "request_ref": request_ref,
        "request_label": request_label,
        "snapshot": snapshot,
    }


def _send_confirmation_prompt(chat_id, parsed_result, url):
    prepared = _prepare_confirmation_state(chat_id)
    selected_product = prepared["selected_product"]
    options = prepared["options"]
    request_label = prepared.get("request_label") or ""
    fare_text = ""
    for option in options:
        if option.get("product", "").lower() == selected_product.lower():
            fare_text = option.get("price", "")
            break
    tg_send(
        chat_id,
        f"Ride options:\n{_format_options_message(options, selected_product)}\n\n"
        f"Current ride: {selected_product}\n"
        f"Fare: {fare_text or 'unknown'}\n"
        f"Current request button: {request_label or 'not found'}\n"
        "Is this okay? Reply with 'yes' or 'okay' to request it, or send '/product uberx', '/product comfort', '/product black', or '/product xl'.\n\n"
        f"JS result:\n{json.dumps(parsed_result, indent=2)}\n\nURL: {url}",
    )


def _format_options_message(options, selected_product=None):
    lines = []
    for option in options:
        product = option.get("product", "Ride")
        price = option.get("price", "")
        marker = " <- selected" if selected_product and product.lower() == selected_product.lower() else ""
        lines.append(f"- {product}: {price}{marker}")
    return "\n".join(lines)


def _confirm_ride(chat_id):
    state = RIDE_STATE.get(chat_id)
    if not state:
        tg_send(chat_id, "Start with /ride first.")
        return
    product_name = state.get("product") or "UberX"
    cached_request_ref = state.get("request_ref")
    cached_request_label = state.get("request_label")
    if state.get("ready_to_submit") and cached_request_ref:
        try:
            browser_click(cached_request_ref)
            shot = browser_screenshot()
            state["phase"] = "requested"
            tg_send(chat_id, f"Ride request result:\n{json.dumps({'mode': 'cached-fast-path', 'requestRef': cached_request_ref, 'requestLabel': cached_request_label, 'clicked': True}, indent=2)}")
            if shot:
                tg_send_photo(chat_id, shot, caption="EmmyCar request result")
            return
        except RuntimeError:
            pass
    selection_result, snapshot = _select_product(product_name)
    lowered = snapshot.lower()
    if "price breakdown" in lowered:
        close_ref = _find_snapshot_button_ref(snapshot, "Close")
        if close_ref:
            try:
                browser_click(close_ref)
                time.sleep(1)
                selection_result, snapshot = _select_product(product_name)
            except RuntimeError:
                pass
    request_ref, request_label = _find_snapshot_request_button(snapshot, product_name)
    if not request_ref:
        time.sleep(1)
        snapshot = browser_snapshot(limit=320)
        lowered = snapshot.lower()
        if "price breakdown" in lowered:
            close_ref = _find_snapshot_button_ref(snapshot, "Close")
            if close_ref:
                try:
                    browser_click(close_ref)
                    time.sleep(1)
                    snapshot = browser_snapshot(limit=320)
                except RuntimeError:
                    pass
        request_ref, request_label = _find_snapshot_request_button(snapshot, product_name)
    if request_ref:
        try:
            browser_click(request_ref)
            result = {"mode": "snapshot", "selectionResult": selection_result, "requestRef": request_ref, "requestLabel": request_label, "clicked": True}
        except RuntimeError:
            snapshot = browser_snapshot(limit=320)
            request_ref, request_label = _find_snapshot_request_button(snapshot, product_name)
            if request_ref:
                browser_click(request_ref)
                result = {"mode": "snapshot", "selectionResult": selection_result, "requestRef": request_ref, "requestLabel": request_label, "clicked": True, "retried": True}
            else:
                result = {"mode": "js-fallback", **_parse_jsonish(_confirm_ride_js())}
    else:
        result = {"mode": "js-fallback", **_parse_jsonish(_confirm_ride_js())}
    shot = browser_screenshot()
    state["phase"] = "requested"
    tg_send(chat_id, f"Ride request result:\n{json.dumps(result, indent=2)}")
    if shot:
        tg_send_photo(chat_id, shot, caption="EmmyCar request result")


def _run_async_for_chat(chat_id, label, fn):
    def runner():
        try:
            fn()
        except Exception as e:
            tg_send(chat_id, f"❌ Error during {label}: {e}\n\n{traceback.format_exc()[-500:]}")

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


def _show_price_confirmation_js_debug(chat_id):
    state = RIDE_STATE[chat_id]
    url = _open_uber_page_for_js(state["origin"], state["destination"])
    result, shot = _run_exact_uber_page_js(state["origin"], state["destination"])
    parsed_result = _parse_jsonish(result)
    snapshot = browser_snapshot(limit=320)
    snapshot_options = _parse_snapshot_ride_options(snapshot)
    fares = _parse_jsonish(_read_ride_options_js())
    effective_options = snapshot_options or fares.get("options", [])
    if ("choose a ride" in snapshot.lower() or fares.get("hasChooseRide")) and effective_options:
        _send_confirmation_prompt(chat_id, parsed_result, url)
    else:
        tg_send(chat_id, f"JS debug result:\n{result}\n\nURL: {url}")
    if shot:
        tg_send_photo(chat_id, shot, caption="EmmyCar JS debug")


def _show_price_confirmation(chat_id):
    state = RIDE_STATE[chat_id]
    url = _open_uber_page_for_js(state["origin"], state["destination"])
    result, shot = _run_exact_uber_page_js(state["origin"], state["destination"])
    parsed_result = _parse_jsonish(result)
    snapshot = browser_snapshot(limit=320)
    snapshot_options = _parse_snapshot_ride_options(snapshot)
    fares = _parse_jsonish(_read_ride_options_js())
    effective_options = snapshot_options or fares.get("options", [])
    if ("choose a ride" in snapshot.lower() or fares.get("hasChooseRide")) and effective_options:
        _send_confirmation_prompt(chat_id, parsed_result, url)
    else:
        tg_send(chat_id, f"⚠️ I couldn't get to the fare-selection page cleanly yet.\n\nResult:\n{result}\n\nURL: {url}")
    if shot:
        tg_send_photo(chat_id, shot, caption="EmmyCar")


def _start_request_js_debug(chat_id, origin, destination, product="UberX"):
    state = {
        "origin": origin,
        "destination": destination,
        "phase": "js_debug",
        "product": product or "UberX",
    }
    RIDE_STATE[chat_id] = state
    tg_send(chat_id, f"🚗 JS debug ride\nRide: {state['product']}\nFrom: {state['origin']}\nTo: {state['destination']}\n\nOpening Uber and running page JS now.")
    _run_async_for_chat(chat_id, "js debug", lambda: _show_price_confirmation_js_debug(chat_id))


def _start_request(chat_id, origin, destination, product="UberX"):
    state = {
        "origin": origin,
        "destination": destination,
        "phase": "opening_ride",
        "product": product or "UberX",
    }
    RIDE_STATE[chat_id] = state
    tg_send(chat_id, f"🚗 Ride request\nRide: {state['product']}\nFrom: {state['origin']}\nTo: {state['destination']}\n\nOpening Uber and getting fares now.")
    _run_async_for_chat(chat_id, "fare lookup", lambda: _show_price_confirmation(chat_id))


def handle_help(chat_id, _args=""):
    tg_send(
        chat_id,
        "🚗 EmmyCar JS Only\n\n"
        "Examples:\n"
        "- /ride home to SFO\n"
        "- /ride 2835 Clay Street, San Francisco to American Bull Burlingame\n"
        "- /ridejs home to 1 Ferry Building, San Francisco\n\n"
        f"- 'home' maps to {HOME_ADDRESS}"
    )


def handle_call(chat_id, args):
    request = _parse_request(args)
    if not request:
        tg_send(chat_id, "Try: '/ride home to American Bull Burlingame'")
        return
    _start_request(chat_id, request["origin"], request["destination"], request.get("product") or "UberX")


def handle_call_js(chat_id, args):
    request = _parse_request(args)
    if not request:
        tg_send(chat_id, "Try: '/ridejs home to American Bull Burlingame'")
        return
    _start_request_js_debug(chat_id, request["origin"], request["destination"], request.get("product") or "UberX")


def handle_product(chat_id, args):
    state = RIDE_STATE.get(chat_id)
    if not state:
        tg_send(chat_id, "Start with /ride or a natural request first.")
        return
    choice = PRODUCT_ALIASES.get(args.strip().lower())
    if not choice:
        tg_send(chat_id, "Try one of: UberX, Comfort, Black, XL")
        return
    state["product"] = choice
    tg_send(chat_id, f"🚗 Ride type set to {choice}")
    if state.get("phase") == "awaiting_confirmation":
        def switch_product():
            _select_product(choice)
            _send_confirmation_prompt(chat_id, {"mode": "product-switch"}, _list_tabs()[0].get("url", "") if _list_tabs() else "")
        _run_async_for_chat(chat_id, "fare refresh", switch_product)


def handle_screenshot(chat_id, _args=""):
    shot = browser_screenshot()
    if not shot:
        tg_send(chat_id, "⚠️ Couldn't get a screenshot.")
        return
    tg_send_photo(chat_id, shot, caption="EmmyCar")


def handle_cancel(chat_id, _args=""):
    RIDE_STATE.pop(chat_id, None)
    tg_send(chat_id, "🛑 Canceled EmmyCar ride flow.")


def handle_go(chat_id, args):
    url = args.strip()
    if not url:
        tg_send(chat_id, "Usage: /go <url>")
        return
    if not url.startswith("http"):
        url = "https://" + url
    browser_navigate(url)
    time.sleep(2)
    tg_send(chat_id, f"✅ Navigated to {url}")


def handle_natural(chat_id, text):
    lower = text.lower().strip()
    state = RIDE_STATE.get(chat_id)
    if lower in NO_WORDS:
        handle_cancel(chat_id)
        return
    if state and state.get("phase") == "awaiting_confirmation" and lower in YES_WORDS:
        _run_async_for_chat(chat_id, "ride confirmation", lambda: _confirm_ride(chat_id))
        return
    if state and state.get("phase") == "awaiting_confirmation" and lower.startswith("uber"):
        choice = PRODUCT_ALIASES.get(lower)
        if choice:
            state["product"] = choice
            tg_send(chat_id, f"🚗 Switching to {choice}. Refreshing fares now.")
            _run_async_for_chat(chat_id, "fare refresh", lambda: _show_price_confirmation(chat_id))
            return
    parsed = _parse_request(text)
    if parsed:
        _start_request(chat_id, parsed["origin"], parsed["destination"], parsed.get("product") or "UberX")
        return
    tg_send(chat_id, "🤔 Try: 'call an uber home to SFO' or use /help.")


COMMANDS = {
    "/help": handle_help,
    "/start": handle_help,
    "/ride": handle_call,
    "/ridejs": handle_call_js,
    "/uberjs": handle_call_js,
    "/uber": handle_call,
    "/call": handle_call,
    "/product": handle_product,
    "/screenshot": handle_screenshot,
    "/cancel": handle_cancel,
    "/go": handle_go,
}


def process_message(msg):
    chat_id = msg["chat"]["id"]
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        tg_send(chat_id, "⛔ Unauthorized. This bot only responds to its owner.")
        return
    text = msg.get("text", "").strip()
    if not text:
        return
    for cmd, handler in COMMANDS.items():
        if text.lower().startswith(cmd):
            args = text[len(cmd):].strip()
            try:
                handler(chat_id, args)
            except Exception as e:
                tg_send(chat_id, f"❌ Error: {e}\n\n{traceback.format_exc()[-500:]}")
            return
    try:
        handle_natural(chat_id, text)
    except Exception as e:
        tg_send(chat_id, f"❌ Error: {e}\n\n{traceback.format_exc()[-500:]}")


def main():
    if not BOT_TOKEN:
        print("ERROR: Set EMMYCAR_BOT_TOKEN environment variable")
        sys.exit(1)
    print("🚗 EmmyCar JS-only bot starting...")
    print(f"   Gateway: {GW_URL}")
    print(f"   Token loaded: {bool(GW_TOKEN)}")
    if ALLOWED_CHAT_ID:
        print(f"   Allowed chat: {ALLOWED_CHAT_ID}")
    else:
        print("   ⚠️ No EMMYCAR_BOT_CHAT_ID set — will respond to anyone!")
    try:
        status = browser_cmd("status", "--json")
        print(f"   Browser status: {status[:100]}")
    except Exception:
        print("   Starting browser...")
        browser_cmd("start")
    print("\n🚗 EmmyCar is running. Send /help to your bot.\n")
    while True:
        try:
            updates = tg_get_updates(timeout=30)
            for update in updates:
                msg = update.get("message")
                if msg:
                    process_message(msg)
        except KeyboardInterrupt:
            print("\n🚗 EmmyCar stopped.")
            break
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
