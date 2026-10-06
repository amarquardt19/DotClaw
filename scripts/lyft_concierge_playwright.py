#!/usr/bin/env python3
"""
Lyft Concierge Playwright Script
Automates ride requests through Lyft Concierge with smart auto-confirm logic
"""
import argparse
import json
import sys
import os
from pathlib import Path
from playwright.sync_api import sync_playwright
import requests

# Rider details
RIDER_FIRST_NAME = "Adam"
RIDER_LAST_NAME = "Marquardt"
RIDER_PHONE = "6508338934"
RIDER_LANGUAGE = "English"

# Google Places API
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

def log(msg):
    print(f"[lyft] {msg}", file=sys.stderr, flush=True)

def resolve_address_google_places(query):
    """Use Google Places API to resolve address to canonical form"""
    if not GOOGLE_PLACES_API_KEY:
        log(f"No Google Places API key, using query as-is: {query}")
        return {"query": query, "canonical": query}
    
    url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "formatted_address,name,geometry",
        "key": GOOGLE_PLACES_API_KEY
    }
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if data.get("status") == "OK" and data.get("candidates"):
            candidate = data["candidates"][0]
            canonical = candidate.get("formatted_address", query)
            name = candidate.get("name", "")
            log(f"Google Places resolved '{query}' -> '{canonical}'")
            return {
                "query": query,
                "canonical": canonical,
                "name": name,
                "lat": candidate.get("geometry", {}).get("location", {}).get("lat"),
                "lng": candidate.get("geometry", {}).get("location", {}).get("lng"),
            }
        else:
            log(f"Google Places API returned status: {data.get('status')}")
            return {"query": query, "canonical": query}
    except Exception as e:
        log(f"Google Places API error: {e}")
        return {"query": query, "canonical": query}

def fill_rider_details(page):
    """Fill in rider details form"""
    log("Filling rider details...")
    
    # Wait a bit for React to fully render
    page.wait_for_timeout(2000)
    
    # Fill using name attributes (more reliable than placeholders)
    filled = page.evaluate("""
        (data) => {
            const inputs = Array.from(document.querySelectorAll('input'));
            const results = {firstName: false, lastName: false, phone: false};
            
            inputs.forEach((input) => {
                const name = input.name || '';
                
                // Fill based on name attribute
                if (name === 'firstName') {
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(input, data.firstName);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.dispatchEvent(new Event('blur', { bubbles: true }));
                    results.firstName = true;
                } else if (name === 'lastName') {
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(input, data.lastName);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.dispatchEvent(new Event('blur', { bubbles: true }));
                    results.lastName = true;
                } else if (name === 'phoneNumber') {
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(input, data.phone);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.dispatchEvent(new Event('blur', { bubbles: true }));
                    results.phone = true;
                }
            });
            
            return results;
        }
    """, {"firstName": RIDER_FIRST_NAME, "lastName": RIDER_LAST_NAME, "phone": RIDER_PHONE})
    
    page.wait_for_timeout(500)
    log(f"✓ Rider details filled: {filled}")

def fill_addresses(page, pickup, destination):
    """Fill pickup and drop-off addresses"""
    log(f"Filling addresses: {pickup} -> {destination}")
    
    # Resolve addresses via Google Places
    pickup_resolved = resolve_address_google_places(pickup)
    destination_resolved = resolve_address_google_places(destination)
    
    # Fill pickup address by clicking and typing (triggers autocomplete)
    page.evaluate("""
        () => {
            const inputs = Array.from(document.querySelectorAll('input'));
            if (inputs[4]) {
                inputs[4].focus();
                inputs[4].click();
            }
        }
    """)
    page.wait_for_timeout(300)
    page.keyboard.type(pickup_resolved["canonical"], delay=30)
    
    # Wait for autocomplete dropdown to appear with the address text
    log("Waiting for pickup autocomplete dropdown...")
    page.wait_for_timeout(1000)
    
    # Wait for dropdown to contain the address (more specific than just [role="button"])
    dropdown_appeared = False
    for attempt in range(10):
        try:
            # Check if page content includes the address (dropdown is visible)
            has_address = page.evaluate("""
                (addr) => {
                    const text = document.body.textContent || '';
                    return text.includes(addr.substring(0, 15));
                }
            """, pickup_resolved["canonical"])
            
            if has_address:
                log(f"✓ Autocomplete dropdown appeared (attempt {attempt + 1})")
                dropdown_appeared = True
                break
        except:
            pass
        page.wait_for_timeout(500)
    
    if not dropdown_appeared:
        log("⚠ Autocomplete dropdown didn't appear, trying keyboard anyway")
    
    # Use keyboard to select
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    page.keyboard.press("Tab")
    page.wait_for_timeout(400)
    page.keyboard.press("Tab")
    page.wait_for_timeout(600)
    log("✓ Selected pickup autocomplete via keyboard")
    
    # Fill drop-off address - explicitly click it first
    page.evaluate("""
        () => {
            const inputs = Array.from(document.querySelectorAll('input'));
            if (inputs[5]) {
                inputs[5].focus();
                inputs[5].click();
            }
        }
    """)
    page.wait_for_timeout(500)
    page.keyboard.type(destination_resolved["canonical"], delay=30)
    
    # Wait for autocomplete dropdown to appear with the address text
    log("Waiting for dropoff autocomplete dropdown...")
    page.wait_for_timeout(1000)
    
    # Wait for dropdown to contain the address
    dropdown_appeared = False
    for attempt in range(10):
        try:
            has_address = page.evaluate("""
                (addr) => {
                    const text = document.body.textContent || '';
                    return text.includes(addr.substring(0, 15));
                }
            """, destination_resolved["canonical"])
            
            if has_address:
                log(f"✓ Dropoff autocomplete dropdown appeared (attempt {attempt + 1})")
                dropdown_appeared = True
                break
        except:
            pass
        page.wait_for_timeout(500)
    
    if not dropdown_appeared:
        log("⚠ Dropoff autocomplete dropdown didn't appear, trying keyboard anyway")
    
    # Use keyboard to select
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    log("✓ Selected dropoff autocomplete via keyboard")
    
    log(f"✓ Addresses filled and selected")
    return pickup_resolved, destination_resolved

def click_ride_details(page):
    """Click the 'Ride details' button"""
    log("Clicking 'Ride details' button...")
    
    button = page.locator('button:has-text("Ride details")').first
    button.click()
    page.wait_for_timeout(2000)
    
    log("✓ Clicked 'Ride details'")

def extract_ride_options(page):
    """Extract all available ride options from the page"""
    log("Extracting ride options...")
    
    # Wait for ride options to load
    page.wait_for_timeout(3000)
    
    # Take screenshot for debugging
    screenshot_path = Path(__file__).parent.parent / ".playwright" / "artifacts" / f"lyft-rides-{page.url.split('/')[-1]}.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_path))
    log(f"Screenshot saved: {screenshot_path}")
    
    # Extract ride options via JavaScript
    rides = page.evaluate("""
        () => {
            const rides = [];
            const pageText = document.body.innerText || '';
            
            // Debug: log page text
            console.log('=== PAGE TEXT ===');
            console.log(pageText);
            console.log('=== END PAGE TEXT ===');
            
            // Extract all "in X min" patterns with prices
            const lines = pageText.split('\\n');
            
            const standardOptions = [];
            const xlOptions = [];
            const easyStepInOptions = [];
            const waitAndSaveOptions = [];
            const priorityOptions = [];
            
            for (let i = 0; i < lines.length; i++) {
                const line = lines[i].trim();
                
                // Look for ride type first
                if (line === 'Standard' || line === 'XL' || line === 'Easy Step-In' || 
                    line === 'Wait & Save' || line === 'Priority Pickup') {
                    const rideType = line;
                    let eta = null;
                    let price = null;
                    
                    // Look ahead for ETA and price
                    // Pattern: "Standard" -> "4" -> "seats" -> "in 4 min" -> "$6.97"
                    for (let j = i + 1; j < Math.min(i + 10, lines.length); j++) {
                        const nextLine = lines[j].trim();
                        
                        // Check for ETA pattern
                        const etaMatch = nextLine.match(/^(?:in|within)\s+(\d+)\s+min$/i);
                        if (etaMatch && !eta) {
                            eta = parseInt(etaMatch[1]);
                        }
                        
                        // Check for price
                        const priceMatch = nextLine.match(/^\$([0-9.]+)$/);
                        if (priceMatch && !price) {
                            price = parseFloat(priceMatch[1]);
                        }
                        
                        // Stop if we found both
                        if (eta && price) break;
                    }
                    
                    if (eta && price) {
                        const option = {
                            type: rideType,
                            price: price,
                            eta_min: eta,
                            text: `${rideType} in ${eta} min - $${price}`
                        };
                        
                        if (rideType === 'Standard') {
                            standardOptions.push(option);
                        } else if (rideType === 'XL') {
                            xlOptions.push(option);
                        } else if (rideType === 'Easy Step-In') {
                            easyStepInOptions.push(option);
                        } else if (rideType === 'Wait & Save') {
                            waitAndSaveOptions.push(option);
                        } else if (rideType === 'Priority Pickup') {
                            priorityOptions.push(option);
                        }
                    }
                }
            }
            
            // Pick fastest Standard option
            if (standardOptions.length > 0) {
                standardOptions.sort((a, b) => a.eta_min - b.eta_min);
                rides.push(standardOptions[0]);
            }
            
            // Pick fastest XL option
            if (xlOptions.length > 0) {
                xlOptions.sort((a, b) => a.eta_min - b.eta_min);
                rides.push(xlOptions[0]);
            }
            
            // Pick fastest Wait & Save option
            if (waitAndSaveOptions.length > 0) {
                waitAndSaveOptions.sort((a, b) => a.eta_min - b.eta_min);
                rides.push(waitAndSaveOptions[0]);
            }
            
            // Pick fastest Priority Pickup option
            if (priorityOptions.length > 0) {
                priorityOptions.sort((a, b) => a.eta_min - b.eta_min);
                rides.push(priorityOptions[0]);
            }
            
            return rides;
        }
    """)
    
    log(f"Found {len(rides)} ride options: {json.dumps(rides, indent=2)}")
    return rides

def should_auto_confirm(rides):
    """
    Determine if we should auto-confirm a ride
    
    Rules:
    1. Auto-confirm Standard if ETA ≤ 5 mins OR fare < 65th percentile
    2. Auto-confirm XL if 3+ mins faster than Standard AND ≤ $1.50 more expensive AND Standard would be auto-confirmed
    3. Ask for confirmation if ETA > 5 mins AND fare >= 65th percentile
    
    Returns: (should_confirm, ride_type, reason)
    """
    if not rides:
        return False, None, "No rides available"
    
    standard = next((r for r in rides if r['type'].lower() == 'standard'), None)
    xl = next((r for r in rides if r['type'].lower() == 'xl'), None)
    
    if not standard:
        log("No Standard ride found, cannot auto-confirm")
        return False, None, "No Standard ride found"
    
    standard_eta = standard.get('eta_min')
    standard_price = standard.get('price')
    
    if standard_eta is None:
        log("⚠ Standard ETA not found, asking for confirmation")
        return False, None, "Standard ETA unknown"
    
    log(f"Standard: ${standard_price:.2f}, ETA: {standard_eta} min")
    
    # Calculate 65th percentile of all ride prices
    all_prices = sorted([r['price'] for r in rides])
    percentile_65_idx = int(len(all_prices) * 0.65)
    percentile_65 = all_prices[percentile_65_idx] if percentile_65_idx < len(all_prices) else all_prices[-1]
    log(f"Price range: ${min(all_prices):.2f} - ${max(all_prices):.2f}, 65th percentile: ${percentile_65:.2f}")
    
    # Check if Standard qualifies for auto-confirm
    eta_ok = standard_eta <= 5
    price_ok = standard_price <= percentile_65
    
    if eta_ok or price_ok:
        # Check if XL is better (Rule 2)
        if xl and xl.get('eta_min') is not None:
            time_diff = standard_eta - xl['eta_min']
            price_diff = xl['price'] - standard['price']
            
            log(f"XL comparison: {time_diff} min faster, ${price_diff:.2f} more expensive")
            
            if time_diff >= 3 and price_diff <= 1.50:
                log(f"✓ Auto-confirm XL: {time_diff} min faster, only ${price_diff:.2f} more")
                return True, 'XL', f"{time_diff} min faster for ${price_diff:.2f} more"
        
        # Auto-confirm Standard
        if eta_ok and price_ok:
            reason = f"{standard_eta} min ETA ≤ 5 min AND ${standard_price:.2f} ≤ ${percentile_65:.2f} (65th percentile)"
        elif eta_ok:
            reason = f"{standard_eta} min ETA ≤ 5 min"
        else:
            reason = f"${standard_price:.2f} ≤ ${percentile_65:.2f} (65th percentile)"
        
        log(f"✓ Auto-confirm Standard: {reason}")
        return True, 'Standard', reason
    
    # No auto-confirm
    log(f"✗ No auto-confirm: ETA {standard_eta} min > 5 min AND ${standard_price:.2f} > ${percentile_65:.2f} (65th percentile)")
    return False, None, f"ETA {standard_eta} min > 5 min AND ${standard_price:.2f} > ${percentile_65:.2f} (65th percentile)"

def select_and_confirm_ride(page, ride_type):
    """Select a ride type and confirm the booking"""
    log(f"Selecting and confirming {ride_type}...")
    
    # Try to click the ride card (might already be selected)
    try:
        ride_card = page.locator(f'text="{ride_type}"').first
        if ride_card.is_visible(timeout=2000):
            ride_card.click()
            page.wait_for_timeout(1000)
            log(f"✓ Clicked {ride_type} card")
    except:
        log(f"⚠ Could not click {ride_type} card, might already be selected")
    
    # Look for confirm button
    confirm_selectors = [
        'button:has-text("Request ride")',
        'button:has-text("Confirm")',
        'button:has-text("Request")',
    ]
    
    for selector in confirm_selectors:
        try:
            confirm_btn = page.locator(selector).first
            if confirm_btn.is_visible(timeout=2000):
                log(f"Found confirm button: {selector}")
                confirm_btn.click()
                page.wait_for_timeout(3000)
                log(f"✓ Ride confirmed: {ride_type}")
                return True
        except Exception as e:
            log(f"Selector {selector} failed: {e}")
            continue
    
    log(f"✗ Could not find confirm button")
    return False

def main():
    parser = argparse.ArgumentParser(description="Lyft Concierge automation")
    parser.add_argument("--pickup", required=True, help="Pickup address")
    parser.add_argument("--destination", required=True, help="Destination address")
    parser.add_argument("--auto-confirm", action="store_true", help="Auto-confirm if rules match")
    parser.add_argument("--dry-run", action="store_true", help="Fetch options but don't book")
    parser.add_argument("--force-ride-type", help="Force selection of specific ride type (e.g., 'Standard', 'XL')")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:18802", help="Chrome DevTools Protocol URL")
    args = parser.parse_args()
    
    result = {
        "pickup": args.pickup,
        "destination": args.destination,
        "rides": [],
        "auto_confirmed": False,
        "confirmed_ride": None,
    }
    
    with sync_playwright() as p:
        # Connect to existing Chrome via CDP
        log(f"Connecting to Chrome via CDP: {args.cdp_url}")
        browser = p.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0]
        
        # Get existing page or create new one
        pages = context.pages
        
        # Find the Lyft Concierge page (not DevTools)
        page = None
        for p in pages:
            if "lyft.com/concierge" in p.url:
                page = p
                log(f"Found Lyft Concierge page: {page.url}")
                break
        
        if not page:
            # Use first non-devtools page
            for p in pages:
                if not p.url.startswith("devtools://"):
                    page = p
                    log(f"Using page: {page.url}")
                    break
        
        if not page:
            page = context.new_page()
            log("Created new page")
        
        try:
            # Always navigate to new request page to start fresh
            log("Navigating to Lyft Concierge new request page...")
            page.goto("https://www.lyft.com/concierge/organization/marqhomecom__lyftagent-1/new", wait_until="networkidle")
            page.wait_for_timeout(2000)
            
            # Take screenshot to see what's on the page
            screenshot_path = Path(__file__).parent.parent / ".playwright" / "artifacts" / "lyft-initial.png"
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path))
            log(f"Initial screenshot: {screenshot_path}")
            
            # Debug: check for iframes
            frames = page.frames
            log(f"Found {len(frames)} frames on page")
            for i, frame in enumerate(frames):
                log(f"Frame {i}: {frame.url}")
            
            # Debug: try to find the input in page content
            page_content = page.content()
            if 'First name' in page_content:
                log("✓ 'First name' text found in page HTML")
            else:
                log("✗ 'First name' text NOT found in page HTML")
            
            # Fill rider details
            fill_rider_details(page)
            
            # Fill addresses
            pickup_resolved, destination_resolved = fill_addresses(page, args.pickup, args.destination)
            result["pickup_resolved"] = pickup_resolved
            result["destination_resolved"] = destination_resolved
            
            # Click ride details
            click_ride_details(page)
            
            # Extract ride options
            rides = extract_ride_options(page)
            result["rides"] = rides
            
            # If dry-run, just return the rides without booking
            if args.dry_run:
                log("Dry-run mode: returning options without booking")
                result["success"] = True
                print(json.dumps(result))
                return
            
            # Force ride type selection (overrides auto-confirm logic)
            if args.force_ride_type:
                log(f"Force selecting ride type: {args.force_ride_type}")
                confirmed = select_and_confirm_ride(page, args.force_ride_type)
                result["auto_confirmed"] = confirmed
                result["confirmed_ride"] = args.force_ride_type
                
                if confirmed:
                    log(f"✓ Successfully booked {args.force_ride_type}")
                    page.wait_for_timeout(3000)
                else:
                    log(f"✗ Failed to book {args.force_ride_type}")
            
            # Auto-confirm logic
            elif args.auto_confirm and rides:
                should_confirm, ride_type, reason = should_auto_confirm(rides)
                result["auto_confirm_reason"] = reason
                
                if should_confirm:
                    confirmed = select_and_confirm_ride(page, ride_type)
                    result["auto_confirmed"] = confirmed
                    result["confirmed_ride"] = ride_type
                    
                    if confirmed:
                        # Wait for confirmation to process
                        page.wait_for_timeout(3000)
                        
                        # Reload to new request page
                        log("Reloading to new request page...")
                        page.goto("https://www.lyft.com/concierge/organization/marqhomecom__lyftagent-1/new", wait_until="networkidle")
                        page.wait_for_timeout(2000)
                        log("✓ Ready for next request")
                else:
                    log(f"No auto-confirm: {reason}")
                    log("Returning options for manual selection")
            
            # Final screenshot
            screenshot_path = Path(__file__).parent.parent / ".playwright" / "artifacts" / "lyft-final.png"
            page.screenshot(path=str(screenshot_path))
            result["screenshot"] = str(screenshot_path)
            
        except Exception as e:
            log(f"Error: {e}")
            result["error"] = str(e)
    
    # Output JSON result
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
