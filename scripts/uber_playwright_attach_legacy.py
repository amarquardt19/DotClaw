#!/usr/bin/env python3
import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


DEFAULT_CDP_URL = "http://127.0.0.1:18801"
GOOGLE_PLACES_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"


def log(message):
    print(f"[uber-attach] {message}", flush=True)


def load_dotenv(path):
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_google_place(query, api_key):
    if not query:
        return None
    params = urllib.parse.urlencode({
        "query": query,
        "key": api_key,
    })
    url = f"{GOOGLE_PLACES_TEXTSEARCH_URL}?{params}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    results = payload.get("results") or []
    if not results:
        status = payload.get("status") or "UNKNOWN"
        error_message = (payload.get("error_message") or "").strip()
        detail = f"google places returned no results: {status}"
        if error_message:
            detail = f"{detail} - {error_message}"
        raise RuntimeError(detail)
    top = results[0]
    name = (top.get("name") or "").strip()
    formatted = (top.get("formatted_address") or "").strip()
    geometry = top.get("geometry", {}).get("location", {}) or {}
    canonical = ", ".join([part for part in [name, formatted] if part])
    resolved = {
        "query": query,
        "canonical": canonical or formatted or name or query,
        "name": name,
        "formattedAddress": formatted,
        "lat": geometry.get("lat"),
        "lng": geometry.get("lng"),
        "placeId": top.get("place_id") or "",
    }
    log(f"google-resolve: query={query!r} canonical={resolved['canonical']!r} lat={resolved['lat']!r} lng={resolved['lng']!r}")
    return resolved


def build_uber_query(original_query, resolved):
    if not resolved:
        return original_query
    name = (resolved.get("name") or "").strip()
    formatted = (resolved.get("formattedAddress") or "").strip()
    original = (original_query or "").strip()
    if not formatted:
        return resolved.get("canonical") or original
    formatted_lower = formatted.lower()
    name_lower = name.lower()
    if name and name_lower in formatted_lower:
        return formatted
    address_like_tokens = [" st", " street", " ave", " avenue", " blvd", " boulevard", " rd", " road", " dr", " drive", " ct", " court", " ln", " lane"]
    if name and any(token in name_lower for token in address_like_tokens):
        return formatted
    parts = [part.strip() for part in formatted.split(",") if part.strip()]
    city = parts[1] if len(parts) > 1 else ""
    state = ""
    if len(parts) > 2:
        state = parts[2].split()[0]
    compact_location = ", ".join([part for part in [city, state] if part])
    compact = ", ".join([part for part in [name or original, compact_location] if part])
    return compact or formatted or resolved.get("canonical") or original


def destination_query_from_resolved(original_query, resolved):
    if not resolved:
        return original_query
    formatted = (resolved.get("formattedAddress") or "").strip()
    if formatted:
        parts = [part.strip() for part in formatted.split(",") if part.strip()]
        return ", ".join(parts[:3])
    return build_uber_query(original_query, resolved)


def query_tokens(*values):
    stopwords = {
        "the", "and", "san", "francisco", "ca", "usa", "street", "st", "avenue", "ave", "road", "rd",
        "drive", "dr", "boulevard", "blvd", "lane", "ln", "court", "ct", "place", "pl", "north", "south",
        "east", "west", "on", "of",
    }
    seen = []
    for value in values:
        for raw in (value or "").replace(",", " ").split():
            token = raw.strip().lower()
            if len(token) < 2 or token in stopwords:
                continue
            if token not in seen:
                seen.append(token)
    return seen[:8]


def destination_tokens(resolved, fallback_query=""):
    if resolved and resolved.get("formattedAddress"):
        return query_tokens(resolved.get("formattedAddress"))
    return query_tokens(fallback_query)


def safe_page_info(page):
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    return {"url": url, "title": title}


def uber_tab_score(page):
    info = safe_page_info(page)
    url = info["url"].lower()
    title = info["title"].lower()
    hay = f"{title} {url}"
    if "m.uber.com/go/product-selection" in url:
        return 100
    if "m.uber.com/go/pickup" in url:
        return 90
    if "m.uber.com/go/home" in url:
        return 80
    if "m.uber.com" in url:
        return 70
    if "uber" in hay:
        return 20
    return -1


def dump_state(page, label):
    state = page.evaluate(
        """
({ label }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const fields = [...document.querySelectorAll('input, textarea')].filter(visible).map((el) => ({
    tag: el.tagName,
    placeholder: el.getAttribute('placeholder') || '',
    ariaLabel: el.getAttribute('aria-label') || '',
    testId: el.getAttribute('data-testid') || '',
    value: el.value || ''
  }));
  const buttons = [...document.querySelectorAll('button, [role="button"], a')].filter(visible).map((el) => ({
    text: clean(el.innerText || el.textContent || ''),
    ariaLabel: el.getAttribute('aria-label') || '',
    testId: el.getAttribute('data-testid') || ''
  })).filter((row) => row.text || row.ariaLabel || row.testId);
  const body = clean(document.body ? document.body.innerText : '');
  return {
    label,
    title: document.title,
    url: location.href,
    inputCount: fields.length,
    inputs: fields.slice(0, 8),
    buttons: buttons.slice(0, 12),
    hasGetRide: /get a ride|plan your ride/i.test(body),
    hasChooseRide: /choose a ride|rides we think you'll like/i.test(body),
    bodyPreview: body.slice(0, 700)
  };
}
""",
        {"label": label},
    )
    log(json.dumps(state, indent=2))
    return state


def click_first_matching(page, candidates, label):
    for selector in candidates:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=1500)
            text = (locator.inner_text(timeout=1000) or "").strip()
            aria = locator.get_attribute("aria-label") or ""
            testid = locator.get_attribute("data-testid") or ""
            log(f"{label}: clicking selector={selector!r} text={text!r} aria={aria!r} testid={testid!r}")
            locator.click(timeout=2000)
            return {"selector": selector, "text": text, "ariaLabel": aria, "testId": testid}
        except Exception:
            continue
    log(f"{label}: no matching visible selector found")
    return None


def dismiss_choose_place_overlay(page):
    selectors = [
        'button[aria-label="Close"]',
        'button:has-text("Close")',
        '[role="button"][aria-label="Close"]',
    ]
    try:
        overlay_visible = page.locator('text="Choose a Place"').first.is_visible(timeout=500)
    except Exception:
        overlay_visible = False
    if not overlay_visible:
        return None
    closed = click_first_matching(page, selectors, "dismiss-choose-place")
    if closed:
        page.wait_for_timeout(150)
        return closed
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        log("dismiss-choose-place: sent Escape")
        return {"selector": "Escape", "mode": "keyboard"}
    except Exception:
        return None


def ensure_choose_place_overlay_closed(page, attempts=3):
    closed = None
    for _ in range(attempts):
        result = dismiss_choose_place_overlay(page)
        if not result:
            break
        closed = result
        page.wait_for_timeout(150)
    return closed


def click_search_button(page, label="search-button"):
    return click_first_matching(
        page,
        [
            'button:has-text("Search")',
            '[role="button"]:has-text("Search")',
            'button[aria-label="Search"]',
        ],
        label,
    )


def finalize_destination_sheet(page, label="destination-finalize"):
    clicked = click_search_button(page, f"{label}-search")
    if clicked:
        page.wait_for_timeout(200)
        return {"mode": "search-click", **clicked}
    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        log(f"{label}: pressed Enter")
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        log(f"{label}: pressed Escape")
        return {"mode": "keyboard-enter-escape"}
    except Exception:
        return None


def click_outside_sheet(page, label="outside-sheet"):
    try:
        page.mouse.click(700, 220)
        log(f"{label}: clicked map area at (700, 220)")
        page.wait_for_timeout(150)
        return {"x": 700, "y": 220}
    except Exception:
        return None


def ensure_mobile_field_open(page, kind):
    selectors = {
        "pickup": [
            '[data-testid="pudo-button-pickup"]',
            'button:has-text("Pickup location")',
            '[role="button"]:has-text("Pickup location")',
            'button:has-text("Enter location")',
        ],
        "destination": [
            '[data-testid="pudo-button-drop0"]',
            'button:has-text("Dropoff location")',
            '[role="button"]:has-text("Dropoff location")',
            'button:has-text("Enter destination")',
        ],
    }
    opened = click_first_matching(page, selectors.get(kind, []), f"open-{kind}")
    if opened or kind != "destination":
        return opened
    try:
        locator = page.get_by_text("Dropoff location", exact=False).first
        locator.wait_for(state="visible", timeout=1200)
        log("open-destination-text-fallback: clicking visible Dropoff location text")
        locator.click(timeout=1500)
        return {"selector": "text=Dropoff location", "text": "Dropoff location"}
    except Exception:
        return None


def find_active_combobox(page, expected_value=""):
    expected = (expected_value or "").strip().lower()
    combos = page.locator('input[role="combobox"]')
    try:
        count = combos.count()
    except Exception:
        count = 0
    best_index = None
    best_score = -1
    for idx in range(count):
        combo = combos.nth(idx)
        try:
            if not combo.is_visible():
                continue
            value = (combo.input_value() or "").strip().lower()
            aria = (combo.get_attribute("aria-label") or "").strip().lower()
            score = 0
            if expected and expected in value:
                score += 2
            if expected and expected in aria:
                score += 1
            if combo == page.locator(':focus'):
                score += 1
            if score > best_score:
                best_score = score
                best_index = idx
        except Exception:
            continue
    if best_index is None:
        return None
    return combos.nth(best_index)


def visible_suggestions(page, label):
    suggestions = page.evaluate(
        """
({ label }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const rows = [...document.querySelectorAll('li, [role="option"], button, a, div')]
    .filter(visible)
    .map((el) => ({
      text: clean(el.innerText || el.textContent || ''),
      ariaLabel: el.getAttribute('aria-label') || '',
      testId: el.getAttribute('data-testid') || '',
      role: el.getAttribute('role') || ''
    }))
    .filter((row) => row.text && row.text.length > 2)
    .filter((row) => /park tavern|clay|larkin|broadway|san francisco|home|location|airport|terminal|street|ave|st\b/i.test(row.text));
  return { label, suggestions: rows.slice(0, 12) };
}
""",
        {"label": label},
    )
    log(json.dumps(suggestions, indent=2))
    return suggestions


def select_suggestion(page, expected_text, label):
    expected = (expected_text or "").strip().lower()
    selectors = [
        '[role="option"]',
        'li',
        '[data-testid*="option"]',
        '[data-testid*="result"]',
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            count = 0
        if count < 1:
            continue
        for idx in range(min(count, 12)):
            row = locator.nth(idx)
            try:
                row.wait_for(state="visible", timeout=1000)
                text = (row.inner_text(timeout=1000) or "").strip()
                if not text:
                    continue
                hay = text.lower()
                if expected and expected not in hay and not hay.startswith(expected.split(" ")[0]):
                    continue
                log(f"{label}: selecting matched suggestion selector={selector!r} index={idx} text={text!r}")
                row.click(timeout=2000)
                return {"selector": selector, "index": idx, "text": text, "matched": True}
            except Exception:
                continue
        try:
            first = locator.first
            first.wait_for(state="visible", timeout=1000)
            text = (first.inner_text(timeout=1000) or "").strip()
            if text:
                log(f"{label}: selecting fallback first suggestion selector={selector!r} text={text!r}")
                first.click(timeout=2000)
                return {"selector": selector, "index": 0, "text": text, "matched": False}
        except Exception:
            continue
    log(f"{label}: no selectable suggestion found")
    return None


def select_top_option(page, label, target_tokens=None, timeout_ms=2500):
    end_time = time.time() + (timeout_ms / 1000)
    blocked_tokens = ["saved places", "edit", "home", "work", "current location", "add home", "add work", "set location on map", "see more results"]
    target_tokens = [token.lower() for token in (target_tokens or []) if token]
    while time.time() < end_time:
        try:
            candidates = page.evaluate(
                """
({ blocked, targets }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const rows = [...document.querySelectorAll('[role="option"]')]
    .filter(visible)
    .map((el, idx) => ({
      idx,
      text: clean(el.innerText || el.textContent || ''),
      score: targets.filter((token) => clean(el.innerText || el.textContent || '').toLowerCase().includes(token)).length,
      top: el.getBoundingClientRect().top,
      left: el.getBoundingClientRect().left,
      width: el.getBoundingClientRect().width
    }))
    .filter((row) => row.text)
    .filter((row) => !blocked.some((token) => row.text.toLowerCase().includes(token)))
    .sort((a, b) => (b.score - a.score) || (a.top - b.top))
    .slice(0, 6);
  return rows;
}
""",
                {"blocked": blocked_tokens, "targets": target_tokens},
            )
        except Exception:
            candidates = []
        for row in candidates:
            if target_tokens and row.get("score", 0) < 1:
                continue
            try:
                locator = page.locator('[role="option"]').nth(row["idx"])
                log(f"{label}: selecting top option index={row['idx']} score={row.get('score', 0)} text={row['text']!r}")
                locator.click(timeout=1500)
                return {"selector": '[role="option"]', "index": row["idx"], "text": row["text"], "matched": True, "mode": "top-option"}
            except Exception:
                continue
        page.wait_for_timeout(100)
    raise TimeoutError(f"{label}: no visible top option within {timeout_ms}ms")


def wait_for_matching_option(page, target_tokens, timeout_ms=2500):
    end_time = time.time() + (timeout_ms / 1000)
    blocked_tokens = ["saved places", "edit", "home", "work", "current location", "add home", "add work", "set location on map", "see more results"]
    target_tokens = [token.lower() for token in (target_tokens or []) if token]
    while time.time() < end_time:
        try:
            candidates = page.evaluate(
                """
({ blocked, targets }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  return [...document.querySelectorAll('[role="option"]')]
    .filter(visible)
    .map((el) => clean(el.innerText || el.textContent || ''))
    .filter((text) => text)
    .filter((text) => !blocked.some((token) => text.toLowerCase().includes(token)));
}
""",
                {"blocked": blocked_tokens, "targets": target_tokens},
            )
        except Exception:
            candidates = []
        if not target_tokens:
            return candidates
        for text in candidates:
            hay = text.lower()
            score = sum(1 for token in target_tokens if token in hay)
            if score >= min(2, len(target_tokens)):
                return candidates
        page.wait_for_timeout(100)
    raise TimeoutError(f"no matching visible options for tokens={target_tokens}")


def click_matching_option(page, label, target_tokens, timeout_ms=2500):
    end_time = time.time() + (timeout_ms / 1000)
    blocked_tokens = ["saved places", "edit", "home", "work", "current location", "add home", "add work", "set location on map", "see more results"]
    target_tokens = [token.lower() for token in (target_tokens or []) if token]
    required_score = max(1, min(3, len(target_tokens)))
    require_dropoff_bound = "1652" in target_tokens and "stockton" in target_tokens
    while time.time() < end_time:
        try:
            candidates = page.evaluate(
                """
({ blocked, targets }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const nodes = [...document.querySelectorAll('[role="option"], button, [role="button"], li, [data-testid], [tabindex]')];
  return nodes
    .filter(visible)
    .map((el, idx) => {
      const text = clean(el.innerText || el.textContent || '');
      const hay = text.toLowerCase();
      const score = targets.filter((token) => hay.includes(token)).length;
      return {
        idx,
        text,
        score,
        top: el.getBoundingClientRect().top,
        tag: el.tagName,
        role: el.getAttribute('role') || '',
        testId: el.getAttribute('data-testid') || ''
      };
    })
    .filter((row) => row.text)
    .filter((row) => !blocked.some((token) => row.text.toLowerCase().includes(token)))
    .sort((a, b) => (b.score - a.score) || (a.top - b.top));
}
""",
                {"blocked": blocked_tokens, "targets": target_tokens},
            )
        except Exception:
            candidates = []
        for row in candidates:
            if row.get("score", 0) < required_score:
                continue
            row_text = row.get("text", "")
            row_text_lower = row_text.lower()
            if require_dropoff_bound and row.get("role") != "option" and "dropoff" not in row_text_lower:
                continue
            try:
                if row.get("role") == "option":
                    locator = page.locator('[role="option"]').nth(row["idx"])
                else:
                    text_snippet = row_text[:120]
                    locator = page.get_by_text(text_snippet, exact=False).first
                log(f"{label}: clicking matching option index={row['idx']} score={row.get('score', 0)} role={row.get('role', '')!r} tag={row.get('tag', '')!r} text={row_text!r}")
                locator.click(timeout=1500)
                return {"selector": row.get("role") == "option" and '[role="option"]' or 'text-match', "index": row["idx"], "text": row_text, "matched": True, "mode": "matched-click"}
            except Exception:
                continue
        if target_tokens:
            snippets = []
            if "1652" in target_tokens and "stockton" in target_tokens:
                snippets.extend([
                    "Dropoff1652 Stockton St",
                    "Dropoff 1652 Stockton St",
                ])
            for snippet in snippets:
                try:
                    locator = page.get_by_text(snippet, exact=False).first
                    if locator.is_visible(timeout=300):
                        log(f"{label}: clicking text fallback snippet={snippet!r}")
                        locator.click(timeout=1500)
                        return {"selector": "text-fallback", "index": -1, "text": snippet, "matched": True, "mode": "matched-click"}
                except Exception:
                    continue
        page.wait_for_timeout(100)
    raise TimeoutError(f"{label}: no clickable matching option for tokens={target_tokens}")


def commit_with_keyboard(page, selector, label):
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=2000)
    locator.focus()
    page.keyboard.press("Enter")
    log(f"{label}: committed with keyboard Enter")
    return {"selector": selector, "mode": "keyboard", "matched": True}


def commit_with_guarded_enter(page, selector, label, target_tokens=None, timeout_ms=2500):
    candidates = wait_for_matching_option(page, target_tokens or [], timeout_ms=timeout_ms)
    log(f"{label}: matching options before Enter = {json.dumps(candidates[:6])}")
    return commit_with_keyboard(page, selector, label)


def fill_uber_input(page, selector, value, label):
    ensure_choose_place_overlay_closed(page)
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=4000)
    try:
        locator.focus()
    except Exception:
        try:
            locator.click(timeout=2000, force=True)
        except Exception:
            pass
    locator.fill("")
    locator.fill(value)
    log(f"{label}: filled selector={selector!r} value={value!r}")
    return {"selector": selector, "value": value}


def fill_uber_field(page, kind, value, label):
    ensure_choose_place_overlay_closed(page)
    field_selectors = {
        "pickup": [
            '[data-testid="dotcom-ui.pickup-destination.input.pickup"]',
            'input[aria-label="pickup location input"]',
            'input[placeholder*="Pickup"]',
            'input[placeholder*="location"]',
            'input[role="combobox"]',
        ],
        "destination": [
            '[data-testid="dotcom-ui.pickup-destination.input.destination.drop0"]',
            'input[aria-label="destination location input"]',
            'input[placeholder*="Dropoff"]',
            'input[placeholder*="destination"]',
        ],
    }
    for selector in field_selectors.get(kind, []):
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=1200)
            return fill_uber_input(page, selector, value, label)
        except Exception:
            continue
    opened = ensure_mobile_field_open(page, kind)
    if opened:
        for selector in field_selectors.get(kind, []):
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=2000)
                return fill_uber_input(page, selector, value, label)
            except Exception:
                continue
    raise TimeoutError(f"{label}: no visible {kind} input found")


def fill_destination_with_retries(page, value, label, attempts=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        ensure_choose_place_overlay_closed(page)
        ensure_mobile_field_open(page, "destination")
        page.wait_for_timeout(150)
        try:
            return fill_uber_field(page, "destination", value, f"{label}-attempt-{attempt}")
        except TimeoutError as exc:
            last_error = exc
            log(f"{label}: attempt {attempt} failed to find destination-bound input")
            page.wait_for_timeout(200)
    raise last_error or TimeoutError(f"{label}: no visible destination input found after retries")


def type_into_focused_target(page, value, label):
    ensure_choose_place_overlay_closed(page)
    try:
        page.keyboard.press("Meta+A")
        page.wait_for_timeout(50)
    except Exception:
        pass
    try:
        page.keyboard.press("Backspace")
        page.wait_for_timeout(50)
    except Exception:
        pass
    page.keyboard.type(value, delay=35)
    log(f"{label}: typed into focused target value={value!r}")
    return {"selector": ":focus", "value": value, "mode": "keyboard-type"}


def refill_active_mobile_combobox(page, value, label):
    ensure_choose_place_overlay_closed(page)
    combo = find_active_combobox(page, value)
    if combo is None:
        raise TimeoutError(f"{label}: no active combobox matched")
    combo.focus()
    combo.fill("")
    combo.fill(value)
    log(f"{label}: refilled active combobox value={value!r}")
    return {"selector": 'input[role="combobox"]', "value": value}


def pause(page, seconds, label):
    log(f"pause {seconds:.1f}s - {label}")
    page.wait_for_timeout(int(seconds * 1000))


def pick_uber_page(browser):
    pages = []
    for context in browser.contexts:
        pages.extend(context.pages)
    if not pages:
        return None
    live_pages = []
    for page in pages:
        try:
            info = safe_page_info(page)
            if not info["url"] and not info["title"]:
                continue
            live_pages.append(page)
        except Exception:
            continue
    if not live_pages:
        return None
    return max(live_pages, key=uber_tab_score)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument("--step-seconds", type=float, default=0.35)
    parser.add_argument("--stay-open", action="store_true")
    parser.add_argument("--click-demo", action="store_true")
    parser.add_argument("--pickup", default="")
    parser.add_argument("--destination", default="")
    parser.add_argument("--fast-top-option", action="store_true")
    parser.add_argument("--debug-dumps", action="store_true")
    parser.add_argument("--google-places", action="store_true")
    parser.add_argument("--google-only", action="store_true")
    parser.add_argument("--press-enter", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    output_dir = root / ".playwright" / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = output_dir / f"uber-attach-{time.strftime('%Y%m%d-%H%M%S')}.png"
    places_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    pickup_resolved = None
    destination_resolved = None
    pickup_is_saved_home = args.pickup.strip().lower() == "home"
    pickup_query = args.pickup
    destination_query = args.destination

    if args.google_places or args.google_only:
        if not places_key:
            raise RuntimeError("Google Places mode requested but GOOGLE_MAPS_API_KEY is not set")
        if pickup_query and not pickup_is_saved_home:
            pickup_resolved = resolve_google_place(pickup_query, places_key)
            pickup_query = build_uber_query(pickup_query, pickup_resolved)
        if destination_query:
            destination_resolved = resolve_google_place(destination_query, places_key)
            destination_query = destination_query_from_resolved(destination_query, destination_resolved)

    if args.google_only:
        print(json.dumps({
            "pickupResolved": pickup_resolved,
            "pickupQuery": pickup_query,
            "destinationResolved": destination_resolved,
            "destinationQuery": destination_query,
        }, indent=2))
        return

    with sync_playwright() as p:
        log(f"connecting over CDP to {args.cdp_url}")
        browser = p.chromium.connect_over_cdp(args.cdp_url)
        page = pick_uber_page(browser)
        if not page:
            raise RuntimeError("No pages found in attached browser")
        info = safe_page_info(page)
        log(f"attached to page title={info['title']!r} url={info['url']!r}")

        initial_state = dump_state(page, "attached-initial") if args.debug_dumps else None
        click_result = None
        pickup_fill = None
        pickup_suggestions = None
        pickup_selection = None
        pickup_modal_recovered = False
        destination_fill = None
        destination_suggestions = None
        destination_selection = None

        if args.click_demo:
            click_result = click_first_matching(
                page,
                [
                    'button:has-text("Got it")',
                    '[role="button"]:has-text("Got it")',
                    'button:has-text("Pickup location")',
                    '[role="button"]:has-text("Pickup location")',
                    'button:has-text("Dropoff location")',
                    '[role="button"]:has-text("Dropoff location")',
                ],
                "attach-demo",
            )
            if click_result:
                pause(page, args.step_seconds, "after attached click demo")

        if args.pickup:
            pickup_fill = fill_uber_field(page, "pickup", pickup_query, "pickup-fill")
            if args.press_enter:
                pickup_selection = commit_with_guarded_enter(
                    page,
                    pickup_fill["selector"],
                    "pickup-selection",
                    target_tokens=query_tokens(
                        pickup_query,
                        pickup_resolved.get("name") if pickup_resolved else "",
                        pickup_resolved.get("formattedAddress") if pickup_resolved else "",
                    ),
                )
            elif args.fast_top_option:
                pickup_selection = select_top_option(
                    page,
                    "pickup-selection",
                    target_tokens=query_tokens(
                        pickup_query,
                        pickup_resolved.get("name") if pickup_resolved else "",
                        pickup_resolved.get("formattedAddress") if pickup_resolved else "",
                    ),
                )
            else:
                pause(page, args.step_seconds, "after pickup fill")
                if args.debug_dumps:
                    pickup_suggestions = visible_suggestions(page, "pickup-suggestions")
                pickup_selection = select_suggestion(page, pickup_query, "pickup-selection")
            if pickup_selection:
                pause(page, args.step_seconds, "after pickup selection")
                modal_closed = ensure_choose_place_overlay_closed(page)
                if modal_closed and not pickup_modal_recovered:
                    pickup_modal_recovered = True
                    pickup_fill = fill_uber_field(page, "pickup", pickup_query, "pickup-refill-after-modal")
                    if args.press_enter:
                        pickup_selection = commit_with_guarded_enter(
                            page,
                            pickup_fill["selector"],
                            "pickup-reselection-after-modal",
                            target_tokens=query_tokens(
                                pickup_query,
                                pickup_resolved.get("name") if pickup_resolved else "",
                                pickup_resolved.get("formattedAddress") if pickup_resolved else "",
                            ),
                        )
                    elif args.fast_top_option:
                        pickup_selection = select_top_option(
                            page,
                            "pickup-reselection-after-modal",
                            target_tokens=query_tokens(
                                pickup_query,
                                pickup_resolved.get("name") if pickup_resolved else "",
                                pickup_resolved.get("formattedAddress") if pickup_resolved else "",
                            ),
                        )
                    else:
                        pickup_selection = select_suggestion(page, pickup_query, "pickup-reselection-after-modal")
                    pause(page, args.step_seconds, "after pickup reselection after modal")
                click_outside_sheet(page, "after-pickup-outside-click")

        if args.destination:
            ensure_choose_place_overlay_closed(page)
            ensure_mobile_field_open(page, "destination")
            destination_fill = type_into_focused_target(page, destination_query, "destination-type")
            if args.press_enter:
                page.wait_for_timeout(900)
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(80)
                page.keyboard.press("Enter")
                log("destination-enter-after-type: pressed ArrowDown, Enter")
                destination_selection = {"selector": ":focus", "mode": "keyboard-arrowdown-enter", "matched": True}
            elif args.fast_top_option:
                destination_selection = select_top_option(
                    page,
                    "destination-selection",
                    target_tokens=destination_tokens(destination_resolved, destination_query),
                )
            else:
                pause(page, args.step_seconds, "after destination fill")
                if args.debug_dumps:
                    destination_suggestions = visible_suggestions(page, "destination-suggestions")
                destination_selection = select_suggestion(page, destination_query, "destination-selection")
            if destination_selection:
                pause(page, args.step_seconds, "after destination selection")

        final_state = dump_state(page, "attached-final")
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(json.dumps({
            "cdpUrl": args.cdp_url,
            "attachedPage": safe_page_info(page),
            "initialState": initial_state,
            "clickResult": click_result,
            "pickupResolved": pickup_resolved,
            "pickupFill": pickup_fill,
            "pickupSuggestions": pickup_suggestions,
            "pickupSelection": pickup_selection,
            "destinationResolved": destination_resolved,
            "destinationFill": destination_fill,
            "destinationSuggestions": destination_suggestions,
            "destinationSelection": destination_selection,
            "finalState": final_state,
            "screenshot": str(screenshot_path),
        }, indent=2))

        if args.stay_open:
            print("Attached mode active. Press Ctrl+C to disconnect.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        browser.close()


if __name__ == "__main__":
    main()
