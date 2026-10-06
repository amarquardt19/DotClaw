#!/usr/bin/env python3
"""
Uber Central Playwright Automation
Connects to existing Chrome session and automates ride booking with smart decision logic
"""

import sys
import json
import asyncio
from playwright.async_api import async_playwright
import re

# Uber Central configuration
UBER_CENTRAL_URL = "https://central.uber.com/v2/organization/84a13941-5856-546b-932a-0199992B6c27/new-ride"
CDP_PORT = 9222  # Chrome remote debugging port

# Default rider info
DEFAULT_RIDER = {
    "first_name": "Adam",
    "last_name": "Marquardt",
    "phone": "6508338934"
}

# Intake form rules
RULES = {
    "value_per_minute": 0.50,  # How much is 1 minute worth
    "min_time_savings": 3,  # Minimum minutes to pay extra
    "max_extra_for_faster": 2.00,  # Max extra $ for faster ride
    "nearby_threshold": 5,  # Minutes to consider "nearby"
    "nearby_max_extra": 3.00,  # Max extra $ for nearby ride
    "priority_min_savings": 3,  # Min minutes saved for priority
    "priority_max_extra": 1.50,  # Max extra $ for priority
    "ask_if_over": 25,  # Ask before booking if over this price
    "never_auto_over": 40,  # Never auto-book if over this price
    "ask_if_wait_over": 8  # Ask if car is this many minutes away
}


def log(message):
    """Print to stderr for logging"""
    print(f"[UBER] {message}", file=sys.stderr, flush=True)


async def main(pickup, destination, dry_run=False, force_ride_type=None):
    """
    Main automation function
    
    Args:
        pickup: Pickup address
        destination: Destination address
        dry_run: If True, fetch options but don't book
        force_ride_type: If set, book this specific ride type (e.g. 'UberX', 'Priority')
    """
    log(f"Starting Uber Central automation: {pickup} → {destination}")
    
    async with async_playwright() as p:
        try:
            # Connect to existing Chrome instance
            log(f"Connecting to Chrome on port {CDP_PORT}...")
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
            
            # Get the default context and page
            contexts = browser.contexts
            if not contexts:
                raise Exception("No browser contexts found. Make sure Chrome is running with remote debugging.")
            
            context = contexts[0]
            pages = context.pages
            
            # Find or create Uber Central page
            uber_page = None
            for page in pages:
                if "central.uber.com" in page.url:
                    uber_page = page
                    break
            
            if not uber_page:
                log("Opening new Uber Central tab...")
                uber_page = await context.new_page()
            
            # Navigate to new ride page
            log(f"Navigating to: {UBER_CENTRAL_URL}")
            try:
                await uber_page.goto(UBER_CENTRAL_URL, wait_until="domcontentloaded", timeout=15000)
            except:
                log("Page load timeout, but continuing anyway...")
            await uber_page.wait_for_timeout(3000)
            
            # Take debug screenshot
            await uber_page.screenshot(path="/tmp/uber_debug.png")
            log("Screenshot saved to /tmp/uber_debug.png")
            
            # Check if we need to select organization
            try:
                org_selector = await uber_page.wait_for_selector('text="Select an organization"', timeout=3000)
                log("Need to select organization...")
                
                # Click the dropdown
                await uber_page.click('select, [role="combobox"], button:has-text("Select an organization")')
                await uber_page.wait_for_timeout(1000)
                
                # Click "Entrepreneur"
                await uber_page.click('text="Entrepreneur"')
                await uber_page.wait_for_timeout(3000)
                log("Selected Entrepreneur organization")
            except:
                log("No organization selection needed, continuing...")
            
            # Click "Single ride" from the sidebar menu
            try:
                log("Looking for 'Single ride' option...")
                # Try to click Single ride directly (it might already be visible)
                await uber_page.click('text="Single ride"', timeout=5000)
                await uber_page.wait_for_timeout(3000)
                log("Clicked Single ride")
            except:
                # If not visible, try clicking "Create new" first
                try:
                    log("Clicking 'Create new' button...")
                    await uber_page.click('button:has-text("Create new")', timeout=3000)
                    await uber_page.wait_for_timeout(1000)
                    
                    log("Now clicking 'Single ride'...")
                    await uber_page.click('text="Single ride"')
                    await uber_page.wait_for_timeout(3000)
                    log("Clicked Single ride")
                except Exception as e:
                    log(f"Could not find Single ride option: {e}")
            
            # Take screenshot after navigation
            await uber_page.screenshot(path="/tmp/uber_after_org.png")
            log("Screenshot saved to /tmp/uber_after_org.png")
            
            # Fill rider information
            log("Filling rider information...")
            try:
                # First name
                first_name_input = await uber_page.wait_for_selector('input[placeholder*="First name"], input[name*="firstName"]', timeout=5000)
                await first_name_input.fill(DEFAULT_RIDER["first_name"])
                log(f"Filled first name: {DEFAULT_RIDER['first_name']}")
                
                # Last name
                last_name_input = await uber_page.wait_for_selector('input[placeholder*="Last name"], input[name*="lastName"]', timeout=5000)
                await last_name_input.fill(DEFAULT_RIDER["last_name"])
                log(f"Filled last name: {DEFAULT_RIDER['last_name']}")
                
                # Phone number
                phone_input = await uber_page.wait_for_selector('input[placeholder*="000-0000"], input[type="tel"]', timeout=5000)
                await phone_input.fill(DEFAULT_RIDER["phone"])
                log(f"Filled phone: {DEFAULT_RIDER['phone']}")
            except Exception as e:
                log(f"Could not fill rider info (might be pre-filled): {e}")
            
            await uber_page.wait_for_timeout(1000)
            
            # Fill pickup address - try multiple selectors
            log(f"Filling pickup: {pickup}")
            pickup_input = None
            selectors = [
                'input[placeholder*="Where from"]',
                'input[placeholder*="where from"]',
                'input[name*="pickup"]',
                'input[aria-label*="Pickup"]',
                'input[aria-label*="pickup"]',
                'input[type="text"]'
            ]
            
            for selector in selectors:
                try:
                    pickup_input = await uber_page.wait_for_selector(selector, timeout=2000)
                    log(f"Found pickup input with selector: {selector}")
                    break
                except:
                    continue
            
            if not pickup_input:
                raise Exception("Could not find pickup address input field")
            await pickup_input.click()
            await pickup_input.fill("")
            await pickup_input.type(pickup, delay=50)
            await uber_page.wait_for_timeout(1500)
            
            # Wait for and select autocomplete suggestion for pickup
            try:
                await uber_page.wait_for_selector('[role="option"]', timeout=5000)
                # Just press Enter (don't use arrow keys for pickup)
                await uber_page.keyboard.press("Enter")
                await uber_page.wait_for_timeout(1000)
                log("Selected autocomplete option for pickup")
            except:
                log("No autocomplete for pickup, continuing...")
            
            # Fill dropoff address - try multiple selectors
            log(f"Filling dropoff: {destination}")
            dropoff_input = None
            dropoff_selectors = [
                'input[placeholder*="Where to"]',
                'input[placeholder*="where to"]',
                'input[name*="dropoff"]',
                'input[name*="destination"]',
                'input[aria-label*="Dropoff"]',
                'input[aria-label*="dropoff"]'
            ]
            
            for selector in dropoff_selectors:
                try:
                    dropoff_input = await uber_page.wait_for_selector(selector, timeout=2000)
                    log(f"Found dropoff input with selector: {selector}")
                    break
                except:
                    continue
            
            if not dropoff_input:
                raise Exception("Could not find dropoff address input field")
            await dropoff_input.click()
            await dropoff_input.fill("")
            await dropoff_input.type(destination, delay=50)
            await uber_page.wait_for_timeout(1500)
            
            # Wait for and select autocomplete suggestion for dropoff
            try:
                await uber_page.wait_for_selector('[role="option"]', timeout=5000)
                # Press UP arrow 5 times to get to the right option, then Enter
                for i in range(5):
                    await uber_page.keyboard.press("ArrowUp")
                    await uber_page.wait_for_timeout(200)
                await uber_page.keyboard.press("Enter")
                await uber_page.wait_for_timeout(2000)
                log("Selected autocomplete option for dropoff (5 up arrows)")
            except:
                log("No autocomplete for dropoff, continuing...")
            
            # Scroll down to "Vehicle type" section and click to open modal
            log("Scrolling to Vehicle type section...")
            try:
                # Wait for "Vehicle type" text to appear
                vehicle_type_section = await uber_page.wait_for_selector('text="Vehicle type"', timeout=10000)
                
                # Scroll it into view
                await vehicle_type_section.scroll_into_view_if_needed()
                await uber_page.wait_for_timeout(2000)
                log("Vehicle type section is visible")
                
                # First, make sure Vehicle type section is EXPANDED (not collapsed)
                log("Ensuring Vehicle type section is expanded...")
                try:
                    # Check if section is collapsed by looking for the down arrow
                    section_collapsed = await uber_page.query_selector('text="Vehicle type" >> .. >> [class*="down"], text="Vehicle type" >> .. >> [class*="chevron-down"]')
                    
                    if section_collapsed:
                        log("Section is collapsed, expanding it...")
                        await vehicle_type_section.click()
                        await uber_page.wait_for_timeout(1000)
                except:
                    log("Could not determine if section is collapsed")
                
                # Now click on the ride card to open the modal
                log("Looking for ride card to open modal...")
                try:
                    await uber_page.wait_for_timeout(1000)
                    
                    # Find the ride card that shows price and ETA
                    ride_cards = await uber_page.query_selector_all('div:has-text("$"), div:has-text("min")')
                    
                    clicked = False
                    for card in ride_cards:
                        text = await card.inner_text()
                        # Must have BOTH price and time, and be a reasonable length (not the whole page)
                        if '$' in text and 'min' in text and len(text) < 200:
                            # Check if it's one of our ride types
                            if any(ride_type in text for ride_type in ['Priority', 'UberX', 'Wait & Save', 'Pet']):
                                log(f"Clicking ride card to open modal...")
                                await card.click()
                                await uber_page.wait_for_timeout(3000)
                                clicked = True
                                break
                    
                    if clicked:
                        log("Clicked ride card to open modal")
                    else:
                        log("Could not find suitable ride card to click")
                        
                except Exception as e:
                    log(f"Error clicking ride card: {e}")
                    
            except Exception as e:
                log(f"Could not find Vehicle type section: {e}")
            
            # Wait for ride options to load
            log("Waiting for ride options to load...")
            await uber_page.wait_for_timeout(3000)
            
            # Take screenshot to see ride options
            await uber_page.screenshot(path="/tmp/uber_rides.png")
            log("Screenshot saved to /tmp/uber_rides.png")
            
            # Extract ride options
            rides = await extract_ride_options(uber_page)
            
            if not rides:
                return {
                    "success": False,
                    "error": "No ride options found"
                }
            
            log(f"Found {len(rides)} ride options")
            for ride in rides:
                log(f"  {ride['type']}: ${ride['price']:.2f}, {ride['eta_min']} min")
            
            # If dry_run, just return the rides without booking
            if dry_run:
                rides_data = [{k: v for k, v in r.items() if k != 'element'} for r in rides]
                log("Dry run mode - returning options without booking")
                return {
                    "success": True,
                    "rides": rides_data,
                    "pickup": pickup,
                    "destination": destination
                }
            
            # If force_ride_type is set, select that specific ride
            if force_ride_type:
                selected_ride = next((r for r in rides if r['type'] == force_ride_type), None)
                if not selected_ride:
                    return {
                        "success": False,
                        "error": f"Requested ride type '{force_ride_type}' not available"
                    }
                log(f"Force selecting {force_ride_type}...")
            else:
                # Apply decision logic
                decision = apply_decision_logic(rides)
                log(f"Decision: {decision['action']} - {decision['reason']}")
                
                if decision['action'] != 'auto_confirm':
                    # Ask for confirmation - remove elements from rides
                    rides_data = [{k: v for k, v in r.items() if k != 'element'} for r in rides]
                    return {
                        "success": True,
                        "action": "ask_confirmation",
                        "rides": rides_data,
                        "reason": decision['reason'],
                        "pickup": pickup,
                        "destination": destination
                    }
                
                selected_ride = decision['selected_ride']
            
            # Book the selected ride
            log(f"Auto-confirming {selected_ride['type']}...")
            
            # Click the selected ride option
            await select_ride_option(uber_page, selected_ride)
            
            # Click Request now button
            await uber_page.click('button:has-text("Request now")')
            await uber_page.wait_for_timeout(2000)
            
            log("✓ Ride requested successfully!")
            
            # Remove element from ride data before returning
            ride_data = {k: v for k, v in selected_ride.items() if k != 'element'}
            
            return {
                "success": True,
                "action": "confirmed",
                "ride": ride_data,
                "reason": f"Booked {selected_ride['type']}",
                "pickup": pickup,
                "destination": destination
            }
            
        except Exception as e:
            log(f"Error: {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "error": str(e)
            }


async def extract_ride_options(page):
    """Extract all available ride options from the page"""
    rides = []
    
    try:
        # Wait for ride options to appear - just wait for "Priority" which should always be there
        await page.wait_for_selector('text="Priority"', timeout=15000)
        log("Found Priority text on page")
        
        # Get all text content from the page
        page_text = await page.inner_text('body')
        log(f"Page text preview: {page_text[:500]}...")
        
        # Wait a bit more for modal to fully load
        await page.wait_for_timeout(2000)
        
        # Look specifically in the modal for ride options
        # The modal should have a "Select when to ride" heading
        modal_visible = await page.query_selector('text="Select when to ride"')
        
        if modal_visible:
            log("Modal is open, extracting rides from modal...")
            # Get all ride cards in the modal - they should be in a scrollable list
            ride_containers = await page.query_selector_all('div:has-text("UberX"), div:has-text("Priority"), div:has-text("Wait & Save"), div:has-text("Pet")')
        else:
            log("Modal not visible, extracting from main page...")
            ride_containers = await page.query_selector_all('div:has-text("UberX"), div:has-text("Priority"), div:has-text("Wait & Save"), div:has-text("Pet")')
        
        log(f"Found {len(ride_containers)} potential ride containers")
        
        rides_dict = {}  # Use dict to keep only the best match per type
        
        for container in ride_containers:
            try:
                text = await container.inner_text()
                
                # Skip if too short (not a real ride card)
                if len(text) < 20:
                    continue
                
                # Must have both price and ETA to be valid
                if '$' not in text or 'min' not in text:
                    continue
                
                # Extract ride type
                ride_type = None
                if "Priority" in text:
                    ride_type = "Priority"
                elif "UberX" in text:
                    ride_type = "UberX"
                elif "Wait & Save" in text:
                    ride_type = "Wait & Save"
                elif "Pet" in text:
                    ride_type = "Pet"
                
                if not ride_type:
                    continue
                
                # Extract price - get the FIRST price in the text (the actual price, not estimated total)
                price_matches = re.findall(r'\$(\d+\.?\d*)', text)
                if not price_matches:
                    continue
                price = float(price_matches[0])  # First price is the ride price
                
                # Extract ETA (look for "In X mins" or "X min" pattern)
                eta_match = re.search(r'In (\d+) min', text) or re.search(r'(\d+)\s*min', text)
                eta_min = int(eta_match.group(1)) if eta_match else 999
                
                # Only keep if we haven't seen this type yet, or if this has valid price
                if ride_type not in rides_dict:
                    log(f"Found ride: {ride_type} - ${price} - {eta_min} min")
                    rides_dict[ride_type] = {
                        "type": ride_type,
                        "price": price,
                        "eta_min": eta_min,
                        "element": container
                    }
                
            except Exception as e:
                log(f"Error extracting ride option: {e}")
                continue
        
        rides = list(rides_dict.values())
        
        return rides
        
    except Exception as e:
        log(f"Error extracting ride options: {e}")
        return []


async def select_ride_option(page, ride):
    """Select ride option using keyboard navigation in the modal"""
    try:
        ride_type = ride['type']
        log(f"Selecting {ride_type} using keyboard navigation...")
        
        # The modal should be open with Wait & Save selected by default (at bottom)
        # Use keyboard to navigate up to the desired ride type
        
        if ride_type == "UberX":
            # UberX is one up from Wait & Save
            log("Pressing Up arrow to select UberX")
            await page.keyboard.press("ArrowUp")
            await page.wait_for_timeout(500)
        elif ride_type == "Priority":
            # Priority is two up from Wait & Save
            log("Pressing Up arrow twice to select Priority")
            await page.keyboard.press("ArrowUp")
            await page.wait_for_timeout(300)
            await page.keyboard.press("ArrowUp")
            await page.wait_for_timeout(500)
        
        # Press Enter to confirm selection and close modal
        log(f"Pressing Enter to confirm {ride_type}")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2000)
            
    except Exception as e:
        log(f"Error selecting ride: {e}")


def apply_decision_logic(rides):
    """Apply intake form rules to decide which ride to book"""
    
    # Filter out Wait & Save (never use it) and only consider UberX and Priority
    standard_rides = [r for r in rides if r['type'] in ['UberX', 'Priority']]
    
    if not standard_rides:
        return {
            "action": "ask_confirmation",
            "reason": "No UberX or Priority rides available"
        }
    
    # Find cheapest and fastest
    cheapest = min(standard_rides, key=lambda r: r['price'])
    fastest = min(standard_rides, key=lambda r: r['eta_min'])
    
    # Filter rides that meet the ETA threshold (≤ 8 min)
    acceptable_rides = [r for r in standard_rides if r['eta_min'] <= RULES['ask_if_wait_over']]
    
    # If we have rides within the ETA threshold, pick the cheapest of those
    if acceptable_rides:
        best_ride = min(acceptable_rides, key=lambda r: r['price'])
        
        # Rule 1: Never auto-book if over $40
        if best_ride['price'] > RULES['never_auto_over']:
            return {
                "action": "ask_confirmation",
                "reason": f"Price ${best_ride['price']:.2f} exceeds auto-book limit of ${RULES['never_auto_over']}"
            }
        
        # Rule 2: Ask if over $25
        if best_ride['price'] > RULES['ask_if_over']:
            return {
                "action": "ask_confirmation",
                "reason": f"Price ${best_ride['price']:.2f} exceeds ask threshold of ${RULES['ask_if_over']}"
            }
        
        # Auto-confirm the best ride within ETA threshold
        return {
            "action": "auto_confirm",
            "selected_ride": best_ride,
            "reason": f"Best option within {RULES['ask_if_wait_over']} min threshold: ${best_ride['price']:.2f}, {best_ride['eta_min']} min"
        }
    
    # No rides within ETA threshold - ask for confirmation
    return {
        "action": "ask_confirmation",
        "reason": f"All rides exceed {RULES['ask_if_wait_over']} min wait threshold (cheapest: {cheapest['type']} ${cheapest['price']:.2f}, {cheapest['eta_min']} min)"
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({
            "success": False,
            "error": "Usage: python uber_central_playwright.py <pickup> <destination> [--dry-run]"
        }))
        sys.exit(1)
    
    pickup = sys.argv[1]
    destination = sys.argv[2]
    dry_run = '--dry-run' in sys.argv
    
    # Check for --force-ride-type argument
    force_ride_type = None
    if '--force-ride-type' in sys.argv:
        idx = sys.argv.index('--force-ride-type')
        if idx + 1 < len(sys.argv):
            force_ride_type = sys.argv[idx + 1]
    
    result = asyncio.run(main(pickup, destination, dry_run=dry_run, force_ride_type=force_ride_type))
    print(json.dumps(result))
