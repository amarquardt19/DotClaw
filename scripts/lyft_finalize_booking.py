#!/usr/bin/env python3
"""
Simple script to finalize a Lyft booking that's already showing ride options.
Just clicks the ride type and confirms - no re-entering addresses.
"""
import sys
from playwright.sync_api import sync_playwright

def log(msg):
    print(f"[lyft-finalize] {msg}", file=sys.stderr, flush=True)

def main():
    if len(sys.argv) < 2:
        print("Usage: python lyft_finalize_booking.py <ride_type>")
        sys.exit(1)
    
    ride_type = sys.argv[1]
    
    log(f"Finalizing Lyft booking for: {ride_type}")
    log(f"Ride type (lowercase): {ride_type.lower()}")
    
    with sync_playwright() as p:
        # Connect to existing Chrome
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        
        # Find the Lyft Concierge page
        page = None
        for pg in context.pages:
            if "lyft.com/concierge" in pg.url:
                page = pg
                break
        
        if not page:
            log("❌ Could not find Lyft Concierge page")
            sys.exit(1)
        
        log(f"Found Lyft page: {page.url}")

        selected = False
        try:
            fare_cards = page.locator('[role="treeitem"]')
            fare_count = fare_cards.count()
            log(f"Found {fare_count} fare cards")
            
            for i in range(fare_count):
                card = fare_cards.nth(i)
                try:
                    card_text = card.inner_text(timeout=1500)
                except Exception:
                    continue
                current_selected = card.get_attribute("aria-selected") or "false"
                preview = " ".join(card_text.split())[:120]
                log(f"Fare card {i}: selected={current_selected} text={preview}")
                
                if ride_type.lower() not in card_text.lower():
                    continue
                
                aria_selected = card.get_attribute("aria-selected") or "false"
                log(f"Matched fare card '{ride_type}' with aria-selected={aria_selected}")
                
                if aria_selected == "true":
                    selected = True
                    log(f"✓ {ride_type} is already selected")
                    break
                
                log(f"Clicking fare card for {ride_type}")
                card.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                card.click()
                page.wait_for_timeout(800)
                
                verified_selected = card.get_attribute("aria-selected") or "false"
                if verified_selected == "true":
                    selected = True
                    log(f"✓ Selected {ride_type} via fare card click")
                    break
                
                try:
                    text_locator = card.locator(f'text="{ride_type}"').first
                    text_locator.scroll_into_view_if_needed()
                    page.wait_for_timeout(200)
                    text_locator.click()
                    page.wait_for_timeout(800)
                    verified_selected = card.get_attribute("aria-selected") or "false"
                    if verified_selected == "true":
                        selected = True
                        log(f"✓ Selected {ride_type} via inner text click")
                        break
                except Exception:
                    pass
                
                try:
                    img_locator = card.locator('img').first
                    if img_locator.is_visible(timeout=500):
                        img_locator.scroll_into_view_if_needed()
                        page.wait_for_timeout(200)
                        img_locator.click()
                        page.wait_for_timeout(800)
                        verified_selected = card.get_attribute("aria-selected") or "false"
                        if verified_selected == "true":
                            selected = True
                            log(f"✓ Selected {ride_type} via image click")
                            break
                except Exception:
                    pass
        except Exception as e:
            log(f"DOM-based fare selection failed: {e}")

        if not selected:
            log("Direct ride selection failed, falling back to keyboard navigation...")
            for i in range(9):
                page.keyboard.press("Tab")
                page.wait_for_timeout(100)

            log("Pressed Tab 9 times")
            log(f"Checking if ride_type '{ride_type}' == 'standard': {ride_type.lower() == 'standard'}")
            if ride_type.lower() == "standard":
                log("Pressing Up Arrow to select Standard...")
                page.keyboard.press("ArrowUp")
                page.wait_for_timeout(500)
                log("Up Arrow pressed")
            else:
                log(f"NOT pressing Up Arrow - ride type is '{ride_type}'")

            log("Pressing Enter to confirm selection...")
            page.keyboard.press("Enter")
            page.wait_for_timeout(1000)
            log("Enter pressed")
        
        # Now click Request ride button
        log("Looking for Request ride button...")
        confirm_selectors = [
            'button:has-text("Request ride")',
            'button:has-text("Confirm")',
            'button:has-text("Request")',
        ]
        
        confirmed = False
        for selector in confirm_selectors:
            try:
                confirm_btn = page.locator(selector).first
                if confirm_btn.is_visible(timeout=2000):
                    log(f"Clicking: {selector}")
                    confirm_btn.click()
                    page.wait_for_timeout(3000)
                    log(f"✓ Ride confirmed!")
                    confirmed = True
                    break
            except:
                continue
        
        if not confirmed:
            log("❌ Could not find confirm button")
            sys.exit(1)
        
        log("✅ Booking complete!")

if __name__ == "__main__":
    main()
