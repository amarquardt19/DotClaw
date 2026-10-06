#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def log(message):
    print(f"[uber-poc] {message}", flush=True)


def pause(page, seconds, label):
    log(f"pause {seconds:.1f}s - {label}")
    page.wait_for_timeout(int(seconds * 1000))


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://m.uber.com/go/home")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--wait-seconds", type=float, default=5.0)
    parser.add_argument("--step-seconds", type=float, default=1.5)
    parser.add_argument("--warm-profile", action="store_true")
    parser.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    parser.add_argument("--channel", default="")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    profile_suffix = args.browser if not args.channel else f"{args.browser}-{args.channel}"
    profile_dir = root / ".playwright" / f"uber-poc-profile-{profile_suffix}"
    output_dir = root / ".playwright" / "artifacts"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    screenshot_path = output_dir / f"uber-poc-{timestamp}.png"

    with sync_playwright() as p:
        browser_type = getattr(p, args.browser)
        log(f"launching persistent browser profile at {profile_dir}")
        launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": args.headless,
            "viewport": {"width": 430, "height": 932},
        }
        if args.channel:
            launch_kwargs["channel"] = args.channel
        if args.browser in {"chromium", "webkit"}:
            launch_kwargs["user_agent"] = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            )
            launch_kwargs["is_mobile"] = True
            launch_kwargs["has_touch"] = True
        context = browser_type.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        log(f"navigating to {args.url}")
        page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        pause(page, args.wait_seconds, "initial page settle")

        if args.warm_profile:
            warm_state = dump_state(page, "warm-profile")
            print(
                json.dumps(
                    {
                        "mode": "warm-profile",
                        "browser": args.browser,
                        "channel": args.channel,
                        "profile": str(profile_dir),
                        "state": warm_state,
                    },
                    indent=2,
                )
            )
            print("Manual warm-up mode active. Complete Uber login/captcha in this browser, then press Ctrl+C.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            context.close()
            return

        initial_state = dump_state(page, "initial")

        cookie_click = click_first_matching(
            page,
            [
                'button:has-text("Got it")',
                'button:has-text("Opt out")',
                '[role="button"]:has-text("Got it")',
                '[role="button"]:has-text("Opt out")',
            ],
            "cookie-banner",
        )
        if cookie_click:
            pause(page, args.step_seconds, "after cookie/banner click")

        post_cookie_state = dump_state(page, "post-cookie")

        pickup_click = click_first_matching(
            page,
            [
                '[data-testid="pickup-setting"]',
                '[data-testid="pickup-location-input"]',
                'button:has-text("Pickup location")',
                '[role="button"]:has-text("Pickup location")',
                'text="Pickup location"',
            ],
            "pickup-control",
        )
        if pickup_click:
            pause(page, args.step_seconds, "after pickup control click")

        pickup_state = dump_state(page, "after-pickup-click")

        dropoff_click = click_first_matching(
            page,
            [
                '[data-testid="dropoff-location-input"]',
                'button:has-text("Dropoff location")',
                '[role="button"]:has-text("Dropoff location")',
                'text="Dropoff location"',
            ],
            "dropoff-control",
        )
        if dropoff_click:
            pause(page, args.step_seconds, "after dropoff control click")

        final_state = dump_state(page, "final")

        page.screenshot(path=str(screenshot_path), full_page=True)
        print(
            json.dumps(
                {
                    "initialState": initial_state,
                    "postCookieState": post_cookie_state,
                    "pickupClick": pickup_click,
                    "pickupState": pickup_state,
                    "dropoffClick": dropoff_click,
                    "finalState": final_state,
                    "screenshot": str(screenshot_path),
                },
                indent=2,
            )
        )
        if not args.headless:
            print("Press Ctrl+C to close the browser.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        context.close()


if __name__ == "__main__":
    main()
