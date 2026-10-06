#!/usr/bin/env python3
"""
Unified Uber + Lyft ride comparison and booking automation.
Compares ride options from both platforms and selects the best one based on intake form rules.
"""

import asyncio
import sys
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

# Import the Uber script
import uber_central_playwright

# Decision rules from intake form
RULES = {
    'never_auto_over': 40,      # Never auto-book if over $40
    'ask_if_over': 25,          # Ask for confirmation if over $25
    'ask_if_wait_over': 8,      # Ask if wait time is over 8 minutes
    'nearby_threshold': 5,       # Consider "nearby" if ≤ 5 min
    'nearby_max_extra': 3,      # Max extra $ for nearby car
    'min_time_savings': 3,      # Min minutes saved to pay more
    'max_extra_for_faster': 5,  # Max extra $ to save time
    'priority_min_savings': 3,  # Min minutes Priority must save
    'priority_max_extra': 5,    # Max extra $ for Priority
}

def log(msg):
    """Print log message"""
    print(f"[COMPARE] {msg}", flush=True)


async def get_uber_options(pickup, destination):
    """Get ride options from Uber Central without booking"""
    log("Fetching Uber Central options...")
    try:
        # Run the Uber automation but stop before confirming
        result = await uber_central_playwright.main(pickup, destination, dry_run=True)
        
        if result.get('success'):
            rides = result.get('rides', [])
            # Filter to only UberX and Priority
            uber_rides = [r for r in rides if r['type'] in ['UberX', 'Priority']]
            
            # Add platform info
            for ride in uber_rides:
                ride['platform'] = 'Uber'
                ride['full_type'] = f"Uber {ride['type']}"
            
            log(f"Found {len(uber_rides)} Uber options")
            return uber_rides
        else:
            log(f"Uber error: {result.get('error')}")
            return []
            
    except Exception as e:
        log(f"Error getting Uber options: {e}")
        return []


async def get_lyft_options(pickup, destination):
    """Launch Lyft script as subprocess and wait for results"""
    log("Launching Lyft script...")
    try:
        # Start Lyft script as subprocess with --dry-run flag
        process = await asyncio.create_subprocess_exec(
            'python3',
            'scripts/lyft_concierge_playwright.py',
            '--pickup', pickup,
            '--destination', destination,
            '--dry-run',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Wait for it to complete
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            # Parse JSON output
            output_lines = stdout.decode().strip().split('\n')
            json_line = output_lines[-1]  # Last line should be JSON
            data = json.loads(json_line)
            
            rides = data.get('rides', [])
            
            # Add platform info
            for ride in rides:
                ride['platform'] = 'Lyft'
                ride['full_type'] = f"Lyft {ride['type']}"
            
            log(f"Found {len(rides)} Lyft options")
            return rides
        else:
            log(f"Lyft script failed: {stderr.decode()}")
            return []
            
    except Exception as e:
        log(f"Error getting Lyft options: {e}")
        return []


def compare_and_select(uber_rides, lyft_rides):
    """
    Compare all rides from both platforms and select the best one.
    Returns: {
        'action': 'auto_confirm' | 'ask_confirmation',
        'selected_ride': {...},
        'reason': str,
        'all_options': [...]
    }
    """
    # Combine all rides
    all_rides = uber_rides + lyft_rides
    
    if not all_rides:
        return {
            'action': 'ask_confirmation',
            'reason': 'No rides available from either platform',
            'all_options': []
        }
    
    log(f"Comparing {len(all_rides)} total rides:")
    for ride in all_rides:
        log(f"  {ride['full_type']}: ${ride['price']:.2f}, {ride['eta_min']} min")
    
    # Filter rides that meet the ETA threshold (≤ 8 min)
    acceptable_rides = [r for r in all_rides if r['eta_min'] <= RULES['ask_if_wait_over']]
    
    # If we have rides within the ETA threshold, pick the cheapest of those
    if acceptable_rides:
        best_ride = min(acceptable_rides, key=lambda r: r['price'])
        
        # Rule 1: Never auto-book if over $40
        if best_ride['price'] > RULES['never_auto_over']:
            return {
                'action': 'ask_confirmation',
                'selected_ride': best_ride,
                'reason': f"Price ${best_ride['price']:.2f} exceeds auto-book limit of ${RULES['never_auto_over']}",
                'all_options': all_rides
            }
        
        # Rule 2: Ask if over $25
        if best_ride['price'] > RULES['ask_if_over']:
            return {
                'action': 'ask_confirmation',
                'selected_ride': best_ride,
                'reason': f"Price ${best_ride['price']:.2f} exceeds ask threshold of ${RULES['ask_if_over']}",
                'all_options': all_rides
            }
        
        # Auto-confirm the best ride within ETA threshold
        return {
            'action': 'auto_confirm',
            'selected_ride': best_ride,
            'reason': f"Best option across both platforms: {best_ride['full_type']} ${best_ride['price']:.2f}, {best_ride['eta_min']} min",
            'all_options': all_rides
        }
    
    # No rides within ETA threshold - find cheapest overall and ask
    cheapest = min(all_rides, key=lambda r: r['price'])
    return {
        'action': 'ask_confirmation',
        'selected_ride': cheapest,
        'reason': f"All rides exceed {RULES['ask_if_wait_over']} min wait threshold (cheapest: {cheapest['full_type']} ${cheapest['price']:.2f}, {cheapest['eta_min']} min)",
        'all_options': all_rides
    }


async def book_selected_ride(ride, pickup, destination):
    """Book the selected ride on the appropriate platform"""
    platform = ride['platform']
    log(f"Booking {ride['full_type']} for ${ride['price']:.2f}...")
    
    try:
        if platform == 'Uber':
            # Run Uber automation with the specific ride type
            result = await uber_central_playwright.main(
                pickup, 
                destination, 
                force_ride_type=ride['type']
            )
        elif platform == 'Lyft':
            # Run Lyft automation with the specific ride type
            result = await lyft_playwright.main(
                pickup, 
                destination, 
                force_ride_type=ride['type']
            )
        else:
            return {
                'success': False,
                'error': f"Unknown platform: {platform}"
            }
        
        return result
        
    except Exception as e:
        log(f"Error booking ride: {e}")
        return {
            'success': False,
            'error': str(e)
        }


async def main(pickup, destination):
    """Main comparison and booking flow"""
    try:
        log(f"Starting ride comparison: {pickup} → {destination}")
        log("=" * 60)
        
        # Fetch options from both platforms in PARALLEL
        log("🚀 Launching UBER and LYFT automations in parallel...")
        log("=" * 60)
        
        uber_task = asyncio.create_task(get_uber_options(pickup, destination))
        lyft_task = asyncio.create_task(get_lyft_options(pickup, destination))
        
        # Wait for both to complete
        log("⏳ Waiting for both platforms to return options...")
        uber_rides, lyft_rides = await asyncio.gather(uber_task, lyft_task)
        
        log("=" * 60)
        log(f"✅ Uber returned {len(uber_rides)} options")
        log(f"✅ Lyft returned {len(lyft_rides)} options")
        log("=" * 60)
        
        # Compare and select best ride
        decision = compare_and_select(uber_rides, lyft_rides)
        
        log(f"Decision: {decision['action']} - {decision['reason']}")
        
        # If auto-confirm, book the ride
        if decision['action'] == 'auto_confirm':
            selected_ride = decision['selected_ride']
            booking_result = await book_selected_ride(selected_ride, pickup, destination)
            
            if booking_result.get('success'):
                return {
                    'success': True,
                    'action': 'confirmed',
                    'ride': {
                        'platform': selected_ride['platform'],
                        'type': selected_ride['type'],
                        'full_type': selected_ride['full_type'],
                        'price': selected_ride['price'],
                        'eta_min': selected_ride['eta_min']
                    },
                    'reason': decision['reason'],
                    'all_options': decision['all_options'],
                    'pickup': pickup,
                    'destination': destination
                }
            else:
                return {
                    'success': False,
                    'error': f"Booking failed: {booking_result.get('error')}",
                    'all_options': decision['all_options']
                }
        else:
            # Ask for confirmation
            return {
                'success': True,
                'action': 'ask_confirmation',
                'recommended_ride': decision['selected_ride'],
                'all_options': decision['all_options'],
                'reason': decision['reason'],
                'pickup': pickup,
                'destination': destination
            }
        
    except Exception as e:
        log(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({
            'success': False,
            'error': 'Usage: python ride_comparison.py <pickup> <destination>'
        }))
        sys.exit(1)
    
    pickup = sys.argv[1]
    destination = sys.argv[2]
    
    result = asyncio.run(main(pickup, destination))
    print(json.dumps(result, indent=2))
