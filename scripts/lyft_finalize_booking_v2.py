#!/usr/bin/env python3
import os
import sys
from playwright.sync_api import sync_playwright

CDP_URL = os.getenv("EMMYCAR_LYFT_CDP_URL", "http://127.0.0.1:9333")


def log(msg):
    print(f"[lyft-finalize-v2] {msg}", file=sys.stderr, flush=True)


def main():
    if len(sys.argv) < 2:
        print("Usage: python lyft_finalize_booking_v2.py <ride_type>")
        sys.exit(1)

    ride_type = sys.argv[1]

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]

        page = None
        for pg in context.pages:
            if "lyft.com/concierge" in pg.url:
                page = pg
                break

        if not page:
            log("Could not find Lyft Concierge page")
            sys.exit(1)

        selected = False
        fare_cards = page.locator('[role="treeitem"]')
        fare_count = fare_cards.count()
        for i in range(fare_count):
            card = fare_cards.nth(i)
            try:
                card_text = card.inner_text(timeout=1500)
            except Exception:
                continue
            if ride_type.lower() not in card_text.lower():
                continue
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            card.click()
            page.wait_for_timeout(800)
            selected = True
            break

        if not selected:
            log(f"Could not select ride type: {ride_type}")
            sys.exit(1)

        for selector in [
            'button:has-text("Request ride")',
            'button:has-text("Confirm")',
            'button:has-text("Request")',
        ]:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    page.wait_for_timeout(3000)
                    print("ok")
                    return
            except Exception:
                continue

        log("Could not find confirm button")
        sys.exit(1)


if __name__ == "__main__":
    main()
