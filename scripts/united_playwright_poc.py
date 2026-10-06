#!/usr/bin/env python3
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://www.united.com/en/us"


def log(message):
    print(f"[united-poc] {message}", flush=True)


def pause(page, seconds, label):
    log(f"pause {seconds:.1f}s - {label}")
    page.wait_for_timeout(int(seconds * 1000))


def visible_state(page, label):
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
    type: el.getAttribute('type') || '',
    placeholder: el.getAttribute('placeholder') || '',
    ariaLabel: el.getAttribute('aria-label') || '',
    testId: el.getAttribute('data-testid') || '',
    name: el.getAttribute('name') || '',
    value: el.value || ''
  }));
  const buttons = [...document.querySelectorAll('button, [role="button"], a')].filter(visible).map((el) => ({
    text: clean(el.innerText || el.textContent || ''),
    ariaLabel: el.getAttribute('aria-label') || '',
    testId: el.getAttribute('data-testid') || ''
  })).filter((row) => row.text || row.ariaLabel || row.testId);
  const body = clean(document.body ? (document.body.innerText || document.body.textContent || '') : '');
  return {
    label,
    title: document.title,
    url: location.href,
    inputCount: fields.length,
    inputs: fields.slice(0, 20),
    buttons: buttons.slice(0, 20),
    bodyPreview: body.slice(0, 1200)
  };
}
""",
        {"label": label},
    )
    log(json.dumps(state, indent=2))
    return state


def click_first_matching(page, candidates, label, timeout_ms=1500):
    for selector in candidates:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=timeout_ms)
            text = ""
            try:
                text = (locator.inner_text(timeout=500) or "").strip()
            except Exception:
                pass
            log(f"{label}: clicking selector={selector!r} text={text!r}")
            locator.click(timeout=timeout_ms)
            return {"selector": selector, "text": text}
        except Exception:
            continue
    log(f"{label}: no matching visible selector found")
    return None


def dismiss_common_popups(page):
    selectors = [
        'button:has-text("Accept")',
        'button:has-text("I agree")',
        'button:has-text("Got it")',
        'button:has-text("No thanks")',
        'button[aria-label="Close"]',
        '[role="button"][aria-label="Close"]',
    ]
    clicks = []
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() < 1:
                continue
            locator.wait_for(state="visible", timeout=700)
            text = (locator.inner_text(timeout=500) or "").strip()
            locator.click(timeout=1000)
            clicks.append({"selector": selector, "text": text})
            page.wait_for_timeout(150)
        except Exception:
            continue
    return clicks


def open_booking_form(page):
    return click_first_matching(
        page,
        [
            'button:has-text("Book")',
            '[role="button"]:has-text("Book")',
            'a:has-text("Book")',
        ],
        "open-booking-form",
        timeout_ms=1200,
    )


def ensure_roundtrip(page):
    return click_first_matching(
        page,
        [
            'button:has-text("Roundtrip")',
            '[role="button"]:has-text("Roundtrip")',
            'label:has-text("Roundtrip")',
            'text="Roundtrip"',
        ],
        "ensure-roundtrip",
        timeout_ms=1200,
    )


def fill_field(page, selectors, value, label):
    last_error = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=2000)
            locator.click(timeout=1200)
            page.wait_for_timeout(100)
            try:
                locator.fill("")
                locator.fill(value)
            except Exception:
                locator.press("Meta+A")
                locator.press("Backspace")
                locator.type(value, delay=25)
            log(f"{label}: filled selector={selector!r} value={value!r}")
            return {"selector": selector, "value": value}
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise RuntimeError(f"{label}: no visible matching field")


def maybe_pick_suggestion(page, label, target_tokens, timeout_ms=3500):
    end_time = time.time() + (timeout_ms / 1000)
    target_tokens = [token.lower() for token in target_tokens if token]
    while time.time() < end_time:
        try:
            candidates = page.evaluate(
                """
({ targetTokens }) => {
  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  return [...document.querySelectorAll('[role="option"], li, button, [role="button"], div')]
    .filter(visible)
    .map((el) => {
      const text = clean(el.innerText || el.textContent || '');
      const hay = text.toLowerCase();
      const score = targetTokens.filter((token) => hay.includes(token)).length;
      return { text, score, top: el.getBoundingClientRect().top };
    })
    .filter((row) => row.text)
    .sort((a, b) => (b.score - a.score) || (a.top - b.top))
    .slice(0, 8);
}
""",
                {"targetTokens": target_tokens},
            )
        except Exception:
            candidates = []
        for row in candidates:
            if row.get("score", 0) < 1:
                continue
            text = row.get("text", "")
            try:
                page.get_by_text(text[:120], exact=False).first.click(timeout=1200)
                log(f"{label}: selected suggestion text={text!r}")
                return {"text": text, "mode": "text-match"}
            except Exception:
                continue
        page.wait_for_timeout(100)
    return None


def open_date_picker(page):
    return click_first_matching(
        page,
        [
            'input[aria-label*="Depart"]',
            'input[placeholder*="Depart"]',
            'button[aria-label*="Depart"]',
            '[role="button"]:has-text("Depart")',
            '[data-testid*="date"]',
        ],
        "open-date-picker",
        timeout_ms=2000,
    )


def calendar_labels(target_date):
    variants = []
    for fmt in [
        "%A, %B %d, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ]:
        variants.append(target_date.strftime(fmt).replace(" 0", " "))
    seen = []
    for item in variants:
        if item not in seen:
            seen.append(item)
    return seen


def click_calendar_day(page, target_date):
    labels = calendar_labels(target_date)
    for _ in range(12):
        for label in labels:
            selectors = [
                f'button[aria-label*="{label}"]',
                f'[role="button"][aria-label*="{label}"]',
                f'td[aria-label*="{label}"]',
            ]
            for selector in selectors:
                locator = page.locator(selector).first
                try:
                    locator.wait_for(state="visible", timeout=600)
                    locator.click(timeout=1000)
                    log(f"calendar: clicked {label!r} via {selector!r}")
                    return {"label": label, "selector": selector}
                except Exception:
                    continue
        next_month = click_first_matching(
            page,
            [
                'button[aria-label*="Next month"]',
                'button[aria-label*="Next Month"]',
                '[role="button"][aria-label*="Next month"]',
                'button:has-text("Next")',
            ],
            "calendar-next-month",
            timeout_ms=800,
        )
        if not next_month:
            break
        page.wait_for_timeout(200)
    raise RuntimeError(f"Could not click calendar date {target_date.date().isoformat()}")


def confirm_dates(page):
    return click_first_matching(
        page,
        [
            'button:has-text("Done")',
            'button:has-text("Apply")',
            '[role="button"]:has-text("Done")',
            '[role="button"]:has-text("Apply")',
        ],
        "confirm-dates",
        timeout_ms=1000,
    )


def search_flights(page):
    clicked = click_first_matching(
        page,
        [
            'button:has-text("Search")',
            'button:has-text("Find flights")',
            '[role="button"]:has-text("Search")',
            '[role="button"]:has-text("Find flights")',
        ],
        "search-flights",
        timeout_ms=2000,
    )
    if clicked:
        return clicked
    try:
        page.keyboard.press("Enter")
        log("search-flights: pressed Enter")
        return {"mode": "keyboard-enter"}
    except Exception:
        return None


def navigate_with_fallback(page, url, timeout_ms):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        return {"mode": "domcontentloaded"}
    except Exception:
        try:
            page.goto(url, wait_until="commit", timeout=timeout_ms)
            return {"mode": "commit"}
        except Exception:
            page.goto(url, timeout=timeout_ms)
            return {"mode": "direct"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--wait-seconds", type=float, default=5.0)
    parser.add_argument("--step-seconds", type=float, default=0.9)
    parser.add_argument("--warm-profile", action="store_true")
    parser.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    parser.add_argument("--channel", default="")
    parser.add_argument("--launch-arg", action="append", default=[])
    parser.add_argument("--origin", default="BOS")
    parser.add_argument("--destination", default="PUJ")
    parser.add_argument("--depart", default="2026-07-17")
    parser.add_argument("--return-date", default="2026-07-20")
    parser.add_argument("--stay-open", action="store_true")
    args = parser.parse_args()

    depart_date = datetime.strptime(args.depart, "%Y-%m-%d")
    return_date = datetime.strptime(args.return_date, "%Y-%m-%d")

    root = Path(__file__).resolve().parents[1]
    profile_suffix = args.browser if not args.channel else f"{args.browser}-{args.channel}"
    profile_dir = root / ".playwright" / f"united-poc-profile-{profile_suffix}"
    output_dir = root / ".playwright" / "artifacts"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    screenshot_path = output_dir / f"united-poc-{timestamp}.png"

    with sync_playwright() as p:
        browser_type = getattr(p, args.browser)
        log(f"launching persistent browser profile at {profile_dir}")
        launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": args.headless,
            "viewport": {"width": 1440, "height": 1100},
        }
        if args.channel:
            launch_kwargs["channel"] = args.channel
        if args.launch_arg:
            launch_kwargs["args"] = args.launch_arg
        context = browser_type.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        log(f"navigating to {args.url}")
        navigation = navigate_with_fallback(page, args.url, args.timeout_ms)
        pause(page, args.wait_seconds, "initial page settle")

        if args.warm_profile:
            warm_state = visible_state(page, "warm-profile")
            print(json.dumps({
                "mode": "warm-profile",
                "browser": args.browser,
                "channel": args.channel,
                "navigation": navigation,
                "profile": str(profile_dir),
                "state": warm_state,
            }, indent=2))
            print("Manual warm-up mode active. Sign into United in this Playwright browser, then press Ctrl+C.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            context.close()
            return

        dismissed = dismiss_common_popups(page)
        open_booking_form(page)
        ensure_roundtrip(page)
        pause(page, args.step_seconds, "booking form settle")

        initial_state = visible_state(page, "initial")

        origin_fill = fill_field(
            page,
            [
                'input[aria-label*="Origin"]',
                'input[placeholder*="Origin"]',
                'input[name*="origin"]',
                'input[id*="origin"]',
            ],
            args.origin,
            "origin-fill",
        )
        origin_suggestion = maybe_pick_suggestion(page, "origin-suggestion", [args.origin.lower()])
        pause(page, args.step_seconds, "after origin")

        destination_fill = fill_field(
            page,
            [
                'input[aria-label*="Destination"]',
                'input[placeholder*="Destination"]',
                'input[name*="destination"]',
                'input[id*="destination"]',
            ],
            args.destination,
            "destination-fill",
        )
        destination_suggestion = maybe_pick_suggestion(page, "destination-suggestion", [args.destination.lower()])
        pause(page, args.step_seconds, "after destination")

        date_picker = open_date_picker(page)
        pause(page, args.step_seconds, "after date picker open")
        depart_pick = click_calendar_day(page, depart_date)
        pause(page, 0.3, "after depart click")
        return_pick = click_calendar_day(page, return_date)
        date_confirm = confirm_dates(page)
        pause(page, args.step_seconds, "after date selection")

        search_click = search_flights(page)
        pause(page, 4.0, "search results settle")
        final_state = visible_state(page, "final")

        page.screenshot(path=str(screenshot_path), full_page=True)
        print(json.dumps({
            "profile": str(profile_dir),
            "dismissed": dismissed,
            "originFill": origin_fill,
            "originSuggestion": origin_suggestion,
            "destinationFill": destination_fill,
            "destinationSuggestion": destination_suggestion,
            "datePicker": date_picker,
            "departPick": depart_pick,
            "returnPick": return_pick,
            "dateConfirm": date_confirm,
            "searchClick": search_click,
            "initialState": initial_state,
            "finalState": final_state,
            "screenshot": str(screenshot_path),
        }, indent=2))

        if args.stay_open and not args.headless:
            print("Press Ctrl+C to close the browser.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        context.close()


if __name__ == "__main__":
    main()
