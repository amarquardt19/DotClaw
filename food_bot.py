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


BOT_TOKEN = os.environ.get("FOOD_BOT_TOKEN", "")
ALLOWED_CHAT_ID = _env_int("FOOD_BOT_CHAT_ID", 0)
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET = 0
BROWSER_PROFILE = os.environ.get("FOOD_BROWSER_PROFILE", "openclaw-food")
BROWSER_CDP_PORT = _env_int("FOOD_BROWSER_CDP_PORT", 18802)
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN") or shutil.which("openclaw") or "/opt/homebrew/bin/openclaw"
FOOD_STATE = {}
TARGET_URL = "https://www.grubhub.com/lets-eat"
DEFAULT_RESTAURANT = "Proposition Chicken"
DEFAULT_ITEMS = ["Small Kale Salad", "Fried Chicken Entree"]
YES_WORDS = {"yes", "y", "ok", "okay", "confirm", "do it", "place it", "order it", "order"}
NO_WORDS = {"cancel", "stop", "no", "n", "never mind", "nevermind"}
ACTIVE_BROWSER_PROFILE = BROWSER_PROFILE
ACTIVE_CDP_PORT = BROWSER_CDP_PORT


def _pick_fallback_profile(err_text):
    lowered = (err_text or "").lower()
    if "profile" not in lowered or "not found" not in lowered:
        return None
    candidates = [
        profile
        for profile in ["openclaw", "openclaw-cars", "chrome"]
        if profile != ACTIVE_BROWSER_PROFILE
    ]
    for profile in candidates:
        if profile in lowered:
            return profile
    return candidates[0] if candidates else None


def _normalize_items(raw_items):
    items = []
    for item in raw_items or []:
        text = (item or "").strip()
        if text:
            items.append(text)
    return items


def _parse_food_request(args):
    text = (args or "").strip()
    if not text:
        return {"restaurant": DEFAULT_RESTAURANT, "items": list(DEFAULT_ITEMS)}
    restaurant_part = text
    items_part = ""
    for separator in ["|", " items:", " item:", " with "]:
        if separator in text.lower():
            idx = text.lower().find(separator)
            restaurant_part = text[:idx].strip(" ,;:-")
            items_part = text[idx + len(separator):].strip()
            break
    restaurant = restaurant_part.strip(" ,;:-") or DEFAULT_RESTAURANT
    raw_items = []
    if items_part:
        raw_items = re.split(r"\s*(?:;|,|\n|\band\b)\s*", items_part)
    items = _normalize_items(raw_items) or list(DEFAULT_ITEMS)
    return {"restaurant": restaurant, "items": items}


def _parse_food_natural_request(text):
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    if "order again" not in lowered and "grubhub" not in lowered and "food" not in lowered:
        return None
    match = re.search(r"(?:from|at)\s+(.+?)(?:\s+(?:with|items?\:?|and get)\s+(.+))?$", text.strip(), flags=re.IGNORECASE)
    if match:
        restaurant = match.group(1).strip(" .,:;-")
        item_text = (match.group(2) or "").strip()
        raw_items = re.split(r"\s*(?:;|,|\n|\band\b)\s*", item_text) if item_text else []
        return {
            "restaurant": restaurant or DEFAULT_RESTAURANT,
            "items": _normalize_items(raw_items) or list(DEFAULT_ITEMS),
        }
    return {"restaurant": DEFAULT_RESTAURANT, "items": list(DEFAULT_ITEMS)}


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
    global ACTIVE_BROWSER_PROFILE
    last_err = None
    total_attempts = max(retries, 1) + 1
    for attempt in range(total_attempts):
        cmd = [OPENCLAW_BIN, "browser"] + list(args) + ["--browser-profile", ACTIVE_BROWSER_PROFILE]
        if GW_TOKEN:
            cmd.extend(["--token", GW_TOKEN])
        if GW_URL:
            cmd.extend(["--url", GW_URL])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip()
                fallback_profile = _pick_fallback_profile(err)
                if fallback_profile and fallback_profile != ACTIVE_BROWSER_PROFILE:
                    ACTIVE_BROWSER_PROFILE = fallback_profile
                    last_err = err
                    continue
                if "timeout" in err.lower() and attempt < total_attempts - 1:
                    last_err = err
                    time.sleep(2)
                    continue
                raise RuntimeError(f"browser {args[0]} failed: {err}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            last_err = f"subprocess timeout after {timeout}s"
            if attempt < total_attempts - 1:
                time.sleep(2)
                continue
            raise RuntimeError(f"browser {args[0]} failed: {last_err}")
    if last_err:
        raise RuntimeError(f"browser {args[0]} failed after {retries} retries: {last_err}")


def _refresh_browser_status():
    global ACTIVE_CDP_PORT
    try:
        raw = browser_cmd("status", "--json", timeout=20, retries=1)
        data = json.loads(raw)
        port = data.get("cdpPort")
        if isinstance(port, int) and port > 0:
            ACTIVE_CDP_PORT = port
        return data
    except Exception:
        return {}


def _list_tabs():
    import urllib.request

    try:
        data = urllib.request.urlopen(f"http://127.0.0.1:{ACTIVE_CDP_PORT}/json/list", timeout=5).read()
        tabs = json.loads(data)
        return [t for t in tabs if t.get("type") == "page"]
    except Exception:
        return []


def _open_blank_tab():
    import urllib.request

    for url in [
        f"http://127.0.0.1:{ACTIVE_CDP_PORT}/json/new?about:blank",
        f"http://127.0.0.1:{ACTIVE_CDP_PORT}/json/new",
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


def browser_focus(target_id):
    return browser_cmd("focus", target_id)


def _ensure_browser_page(timeout_seconds=10):
    _refresh_browser_status()
    tabs = _list_tabs()
    if tabs:
        try:
            browser_focus(tabs[0]["id"])
        except Exception:
            pass
        return True
    try:
        browser_cmd("start")
    except RuntimeError:
        pass
    _refresh_browser_status()
    end_time = time.time() + timeout_seconds
    while time.time() < end_time:
        _refresh_browser_status()
        tabs = _list_tabs()
        if tabs:
            try:
                browser_focus(tabs[0]["id"])
            except Exception:
                pass
            return True
        _open_blank_tab()
        time.sleep(1)
    return False


def browser_navigate(url):
    if not _ensure_browser_page():
        raise RuntimeError("browser navigate failed: no pages available in the connected browser")
    try:
        return browser_cmd("navigate", url)
    except RuntimeError as err:
        if "frame was detached" not in str(err).lower():
            raise
        time.sleep(1)
        if not _ensure_browser_page():
            raise
        return browser_cmd("navigate", url)


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


def _grubhub_tab_score(tab):
    url = (tab.get("url") or "").lower()
    title = (tab.get("title") or "").lower()
    hay = f"{title} {url}"
    if "grubhub.com" in url and DEFAULT_RESTAURANT.lower().replace(" ", "-") in url:
        return 100
    if "grubhub.com" in url and "proposition" in hay:
        return 80
    if "grubhub.com" in url and "checkout" in url:
        return 70
    if "grubhub.com" in hay:
        return 20
    return -1


def _cdp_eval(js_code, timeout=30):
    try:
        tabs = _list_tabs()
        if not tabs:
            return "error: no browser tabs found"
        target = max(tabs, key=_grubhub_tab_score)
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


def _parse_jsonish(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {"raw": value}
    try:
        return json.loads(value)
    except Exception:
        return {"raw": value}


def _food_flow_js(restaurant_name, items, place_order):
    restaurant_json = json.dumps(restaurant_name)
    items_json = json.dumps(items)
    place_json = "true" if place_order else "false"
    return _cdp_eval(
        f"""
(async () => {{
  const TARGET_RESTAURANT = {restaurant_json};
  const TARGET_ITEMS = {items_json};
  const PLACE_ORDER = {place_json};
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
  const bodyText = () => clean(document.body.innerText || document.body.textContent || '');
  const visible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const all = (sel) => [...document.querySelectorAll(sel)];
  const textOf = (el) => norm(el?.innerText || el?.getAttribute?.('aria-label') || el?.textContent || '');
  const clickish = (el) => {{
    if (!el) return false;
    el.scrollIntoView?.({{ block: 'center' }});
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
      el.dispatchEvent(new MouseEvent(type, {{ bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window }}));
    }}
    el.click?.();
    return true;
  }};
  const setValue = (el, value) => {{
    if (!el) return false;
    const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    el.focus?.();
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return true;
  }};
  const pressEnter = (el) => {{
    if (!el) return false;
    for (const type of ['keydown', 'keypress', 'keyup']) {{
      el.dispatchEvent(new KeyboardEvent(type, {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }}));
    }}
    return true;
  }};
  const findByText = (texts, selectors='button, [role="button"], a, article, div, span, h1, h2, h3, label') => {{
    const wanted = texts.map(t => t.toLowerCase());
    return all(selectors).filter(visible).find(el => wanted.some(w => textOf(el).includes(w))) || null;
  }};
  const findManyByText = (texts, selectors='button, [role="button"], a, article, div, span, h1, h2, h3, label') => {{
    const wanted = texts.map(t => t.toLowerCase());
    return all(selectors).filter(visible).filter(el => wanted.some(w => textOf(el).includes(w)));
  }};
  const firstRestaurantCandidate = () => {{
    const anchors = all('a').filter(visible);
    const anchorHit = anchors.find(el => {{
      const text = textOf(el);
      const href = norm(el.getAttribute('href') || '');
      return text.includes(norm(TARGET_RESTAURANT)) && (href.includes('/restaurant/') || href.includes('/delivery/'));
    }});
    if (anchorHit) return anchorHit;
    const cards = all('article, [data-testid], div').filter(visible);
    return cards.find(el => {{
      const text = textOf(el);
      if (!text.includes(norm(TARGET_RESTAURANT))) return false;
      const hasMeta = text.includes('min') || text.includes('mile') || text.includes('rating') || text.includes('offers available');
      if (!hasMeta) return false;
      return !!(el.querySelector('a[href*="/restaurant/"]') || el.querySelector('a[href*="/delivery/"]'));
    }}) || null;
  }};
  const waitFor = async (fn, timeout=8000) => {{
    const end = Date.now() + timeout;
    while (Date.now() < end) {{
      const val = fn();
      if (val) return val;
      await sleep(250);
    }}
    return null;
  }};
  const scrollPage = (amount=500) => {{
    window.scrollBy({{ top: amount, left: 0, behavior: 'instant' }});
  }};
  const findOrderAgainControl = () => {{
    const categoryRail = all('aside, nav, div, section, ul').filter(visible).find(el => {{
      const text = textOf(el);
      return text.includes('best sellers') && text.includes('order again') && text.includes('sandwiches') && text.includes('salads');
    }});
    if (categoryRail) {{
      const exact = [...categoryRail.querySelectorAll('a, button, [role="button"], div, span, li')]
        .filter(visible)
        .find(el => textOf(el) === 'order again' && !(el.closest('header') || el.closest('[aria-label*="review" i]')));
      if (exact) return exact;
    }}
    const categoriesMarker = all('a, button, [role="button"], div, span').filter(visible).find(el => textOf(el) === 'categories');
    if (categoriesMarker) {{
      const container = categoriesMarker.parentElement?.parentElement || categoriesMarker.parentElement || categoriesMarker;
      const scoped = [...container.parentElement?.querySelectorAll?.('a, button, [role="button"], div, span, li') || []]
        .filter(visible)
        .find(el => textOf(el) === 'order again');
      if (scoped) return scoped;
    }}
    return null;
  }};
  const handleScheduleModal = async () => {{
    const modalText = textOf(document.body);
    if (!modalText.includes('schedule my order')) return false;
    const modalShell = all('div, section, aside').filter(visible).find(el => textOf(el).includes('schedule my order'));
    if (modalShell) {{
      const backArrow = [...modalShell.querySelectorAll('button, [role="button"], div, span, svg')]
        .filter(visible)
        .find(el => {{
          const label = textOf(el);
          const aria = norm(el.getAttribute?.('aria-label') || '');
          const cls = norm((el.className || '').toString());
          return aria.includes('back') || aria.includes('previous') || label === '' || cls.includes('back');
        }});
      if (backArrow) {{
        clickish(backArrow.closest('button, [role="button"], div, span') || backArrow);
        result.steps.push('schedule-back-clicked');
        await sleep(1500);
        return true;
      }}
    }}
    const closeBtn = findByText(['close', 'x'], 'button, [role="button"], div, span');
    if (closeBtn) {{
      clickish(closeBtn.closest('button, [role="button"], div, span') || closeBtn);
      result.steps.push('schedule-close-clicked');
      await sleep(1200);
      return true;
    }}
    return false;
  }};
  const result = {{ steps: [], itemsAdded: [], restaurantFound: false, onCheckout: false, placed: false }};
  let restaurantAlreadyOpenedFromResults = false;

  if (!/grubhub\.com/.test(location.hostname)) {{
    location.href = 'https://www.grubhub.com/lets-eat';
    return JSON.stringify({{ redirecting: true }});
  }}

  const searchInput = all('input, textarea').filter(visible).find(el => {{
    const label = norm(el.getAttribute('placeholder') || el.getAttribute('aria-label') || '');
    return label.includes('search') || label.includes('food');
  }});
  if (searchInput) {{
    setValue(searchInput, TARGET_RESTAURANT);
    result.steps.push('search-filled');
    await sleep(700);
    pressEnter(searchInput);
    result.steps.push('search-enter');
    await sleep(3500);
    const firstResult = await waitFor(() => firstRestaurantCandidate(), 6000);
    if (firstResult) {{
      const target = firstResult.tagName === 'A'
        ? firstResult
        : (firstResult.querySelector('a[href*="/restaurant/"]') || firstResult.querySelector('a[href*="/delivery/"]') || firstResult.closest('a, button, [role="button"], article, div') || firstResult);
      clickish(target);
      result.steps.push('restaurant-result-clicked');
      restaurantAlreadyOpenedFromResults = true;
      await sleep(4000);
    }} else {{
      result.steps.push('restaurant-result-missing');
    }}
  }} else {{
    result.steps.push('search-missing');
  }}

  const restaurantCard = await waitFor(() => {{
    return firstRestaurantCandidate() || findByText([TARGET_RESTAURANT], 'a, button, [role="button"], article, div, span, h1, h2, h3');
  }}, 9000);
  if (!restaurantCard) {{
    return JSON.stringify({{ ...result, error: 'restaurant-not-found', url: location.href, body: bodyText().slice(0, 600) }});
  }}
  if (!restaurantAlreadyOpenedFromResults) {{
    const restaurantTarget = restaurantCard.tagName === 'A'
      ? restaurantCard
      : (restaurantCard.querySelector('a[href*="/restaurant/"]') || restaurantCard.querySelector('a[href*="/delivery/"]') || restaurantCard.closest('a, button, [role="button"], article, div') || restaurantCard);
    clickish(restaurantTarget);
    result.steps.push('restaurant-opened');
    await sleep(5000);
  }} else {{
    result.steps.push('restaurant-already-open');
  }}
  result.restaurantFound = true;

  await handleScheduleModal();

  let orderAgain = await waitFor(() => findOrderAgainControl(), 3000);
  if (!orderAgain) {{
    for (let i = 0; i < 5; i++) {{
      scrollPage(350);
      await sleep(500);
      orderAgain = findOrderAgainControl();
      if (orderAgain) break;
    }}
  }}
  if (orderAgain) {{
    clickish(orderAgain.closest('button, [role="button"], a, div, span') || orderAgain);
    result.steps.push('order-again-opened');
    await sleep(1800);
  }} else {{
    result.steps.push('order-again-missing');
  }}

  for (const itemName of TARGET_ITEMS) {{
    const itemCard = await waitFor(() => findByText([itemName], 'button, [role="button"], article, div, span, h1, h2, h3, p'), 5000);
    if (!itemCard) {{
      result.steps.push(`missing-item:${{itemName}}`);
      continue;
    }}
    clickish(itemCard.closest('button, [role="button"], article, div') || itemCard);
    result.steps.push(`opened-item:${{itemName}}`);
    await sleep(1200);
    const addBtn = await waitFor(() => findByText(['add to bag', 'add to cart'], 'button, [role="button"], div, span'), 5000);
    if (!addBtn) {{
      result.steps.push(`missing-add:${{itemName}}`);
      continue;
    }}
    clickish(addBtn.closest('button, [role="button"], div, span') || addBtn);
    result.itemsAdded.push(itemName);
    result.steps.push(`added:${{itemName}}`);
    await sleep(1800);
  }}

  const bagButton = await waitFor(() => {{
    return findByText(['view bag', 'modify your order', 'bag', 'cart'], 'button, [role="button"], a, div, span')
      || all('[aria-label*="bag" i], [aria-label*="cart" i], [data-testid*="bag" i], [data-testid*="cart" i]').filter(visible)[0]
      || null;
  }}, 5000);
  if (bagButton) {{
    clickish(bagButton.closest('button, [role="button"], a, div, span') || bagButton);
    result.steps.push('bag-opened');
    await sleep(2000);
  }} else {{
    result.steps.push('bag-missing');
  }}

  const standardDelivery = findByText(['standard delivery'], 'button, [role="button"], div, span, label');
  if (standardDelivery) {{
    clickish(standardDelivery.closest('button, [role="button"], label, div, span') || standardDelivery);
    result.steps.push('standard-delivery-selected');
    await sleep(1000);
  }}

  for (let i = 0; i < 2; i++) {{
    const checkoutBtn = findManyByText(['checkout', 'continue to checkout', 'proceed to checkout'], 'button, [role="button"], a, div, span')[0];
    if (!checkoutBtn) break;
    clickish(checkoutBtn.closest('button, [role="button"], a, div, span') || checkoutBtn);
    result.steps.push(`checkout-clicked:${{i + 1}}`);
    await sleep(2500);
  }}

  const paymentMethod = findByText(['payment method', 'change', 'edit payment'], 'button, [role="button"], a, div, span');
  if (paymentMethod) {{
    clickish(paymentMethod.closest('button, [role="button"], a, div, span') || paymentMethod);
    result.steps.push('payment-opened');
    await sleep(1800);
  }}
  const venmo = await waitFor(() => findByText(['venmo'], 'button, [role="button"], a, div, span, label'), 5000);
  if (venmo) {{
    clickish(venmo.closest('button, [role="button"], a, div, span, label') || venmo);
    result.steps.push('venmo-selected');
    await sleep(1800);
  }} else {{
    result.steps.push('venmo-missing');
  }}

  result.onCheckout = /payment method|venmo|place your delivery order|review order|driver tip/i.test(bodyText());

  if (PLACE_ORDER) {{
    const placeBtn = findByText(['place your delivery order', 'review order', 'continue with venmo', 'pay with venmo'], 'button, [role="button"], a, div, span');
    if (placeBtn) {{
      clickish(placeBtn.closest('button, [role="button"], a, div, span') || placeBtn);
      result.steps.push('place-order-clicked');
      result.placed = true;
      await sleep(2500);
    }} else {{
      result.steps.push('place-order-missing');
    }}
  }}

  result.url = location.href;
  result.body = bodyText().slice(0, 700);
  return JSON.stringify(result);
}})()
""",
        timeout=60,
    )


def _run_async_for_chat(chat_id, label, fn):
    def runner():
        try:
            fn()
        except Exception as e:
            tg_send(chat_id, f"❌ Error during {label}: {e}\n\n{traceback.format_exc()[-500:]}")

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


def _send_status(chat_id, title, parsed):
    lines = [title]
    if parsed.get("error"):
        lines.append(f"Error: {parsed['error']}")
    if parsed.get("itemsAdded"):
        lines.append("Items added: " + ", ".join(parsed["itemsAdded"]))
    if parsed.get("steps"):
        lines.append("Steps: " + " | ".join(parsed["steps"][:12]))
    if parsed.get("url"):
        lines.append(f"URL: {parsed['url']}")
    if parsed.get("placed"):
        lines.append("Order placement click was sent.")
    elif parsed.get("onCheckout"):
        lines.append("Checkout page is ready.")
    tg_send(chat_id, "\n".join(lines))
    shot = browser_screenshot()
    if shot:
        tg_send_photo(chat_id, shot, caption="Food")


def _start_food_flow(chat_id, request, place_order):
    FOOD_STATE[chat_id] = {
        "phase": "ordering" if place_order else "reviewing",
        "restaurant": request["restaurant"],
        "items": list(request["items"]),
    }
    tg_send(
        chat_id,
        f"🍗 Food\nOpening Grubhub, searching {request['restaurant']}, and adding: "
        + ", ".join(request["items"]) + "."
        + (" Then I’ll click 'Place your delivery order'." if place_order else " I’ll stop at checkout for review."),
    )

    def run_flow():
        browser_navigate(TARGET_URL)
        time.sleep(4)
        parsed = _parse_jsonish(_food_flow_js(request["restaurant"], request["items"], place_order=place_order))
        _send_status(chat_id, "Food flow result:", parsed)
        FOOD_STATE[chat_id]["phase"] = "done"

    _run_async_for_chat(chat_id, "food order", run_flow)


def handle_help(chat_id, _args=""):
    tg_send(
        chat_id,
        "🍗 Food\n\n"
        "Commands:\n"
        "- /foodreview : use the default Proposition Chicken order and stop at checkout\n"
        "- /food : use the default Proposition Chicken order and place it\n"
        "- /foodreview <restaurant>\n"
        "- /foodreview <restaurant> | <item1>; <item2>\n"
        "- /food <restaurant> | <item1>; <item2>\n"
        "- /go <url> : navigate manually\n"
        "- /snap : inspect page text\n"
        "- /screenshot : get a screenshot\n"
        "- /cancel : clear local Food state\n\n"
        "Examples:\n"
        "- /foodreview Proposition Chicken\n"
        "- /food Proposition Chicken | Small Kale Salad; Fried Chicken Entree\n"
        "- order again from Proposition Chicken\n\n"
        "If you omit items, Food uses the default two-item order."
    )


def handle_food_review(chat_id, args=""):
    _start_food_flow(chat_id, _parse_food_request(args), place_order=False)


def handle_food(chat_id, args=""):
    _start_food_flow(chat_id, _parse_food_request(args), place_order=True)


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


def handle_snap(chat_id, _args=""):
    snap = browser_snapshot(limit=250)
    useful = [line.strip() for line in snap.splitlines() if line.strip()][:60]
    tg_send(chat_id, "📋 Page snapshot:\n\n" + "\n".join(useful))


def handle_screenshot(chat_id, _args=""):
    shot = browser_screenshot()
    if not shot:
        tg_send(chat_id, "⚠️ Couldn't get a screenshot.")
        return
    tg_send_photo(chat_id, shot, caption="Food")


def handle_cancel(chat_id, _args=""):
    FOOD_STATE.pop(chat_id, None)
    tg_send(chat_id, "🛑 Cleared Food state.")


def handle_natural(chat_id, text):
    lower = text.lower().strip()
    if lower in NO_WORDS:
        handle_cancel(chat_id)
        return
    if any(token in lower for token in ["food", "grubhub", "proposition chicken"]):
        request = _parse_food_natural_request(text) or {"restaurant": DEFAULT_RESTAURANT, "items": list(DEFAULT_ITEMS)}
        _start_food_flow(chat_id, request, place_order=any(token in lower for token in ["place", "submit"]))
        return
    tg_send(chat_id, "🤔 Try /foodreview first, or /food to place the order.")


COMMANDS = {
    "/help": handle_help,
    "/start": handle_help,
    "/foodreview": handle_food_review,
    "/food": handle_food,
    "/go": handle_go,
    "/snap": handle_snap,
    "/screenshot": handle_screenshot,
    "/cancel": handle_cancel,
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
        print("ERROR: Set FOOD_BOT_TOKEN environment variable")
        sys.exit(1)
    print("🍗 Food bot starting...")
    print(f"   Gateway: {GW_URL}")
    print(f"   Token loaded: {bool(GW_TOKEN)}")
    print(f"   Requested browser profile: {BROWSER_PROFILE}")
    if ALLOWED_CHAT_ID:
        print(f"   Allowed chat: {ALLOWED_CHAT_ID}")
    else:
        print("   ⚠️ No FOOD_BOT_CHAT_ID set — will respond to anyone!")
    try:
        status = _refresh_browser_status()
        print(f"   Browser status: {json.dumps(status)[:100]}")
    except Exception:
        print("   Starting browser...")
        browser_cmd("start")
        status = _refresh_browser_status()
    if ACTIVE_BROWSER_PROFILE != BROWSER_PROFILE:
        print(f"   Using fallback browser profile: {ACTIVE_BROWSER_PROFILE}")
    print(f"   Active CDP port: {ACTIVE_CDP_PORT}")
    print("\n🍗 Food is running. Send /help to your bot.\n")
    while True:
        try:
            updates = tg_get_updates(timeout=30)
            for update in updates:
                msg = update.get("message")
                if msg:
                    process_message(msg)
        except KeyboardInterrupt:
            print("\n🍗 Food stopped.")
            break
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()
