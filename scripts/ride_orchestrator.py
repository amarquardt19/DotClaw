#!/usr/bin/env python3
"""
Simple orchestrator that launches Uber and Lyft scripts in parallel,
waits for both to return options, then compares and books the best ride.
"""

import subprocess
import json
import sys
import time
from datetime import datetime

# Log to both stderr and file
LOG_FILE = "/Users/adammarq/CascadeProjects/openclaw-inbox-agent/ride_orchestrator.log"
PRICE_TIE_THRESHOLD = 0.75

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] [ORCHESTRATOR] {msg}"
    print(f"[ORCHESTRATOR] {msg}", file=sys.stderr, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(log_msg + "\n")

def main():
    if len(sys.argv) < 3:
        print(json.dumps({
            'success': False,
            'error': 'Usage: python ride_orchestrator.py <pickup> <destination>'
        }))
        sys.exit(1)
    
    pickup = sys.argv[1]
    destination = sys.argv[2]
    
    log(f"Starting ride comparison: {pickup} → {destination}")
    log("=" * 70)
    log("🚀 Launching UBER and LYFT in parallel...")
    log("=" * 70)
    
    # Launch both scripts as separate processes
    uber_process = subprocess.Popen(
        ['python3', 'scripts/uber_central_playwright.py', pickup, destination, '--dry-run'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    lyft_process = subprocess.Popen(
        ['python3', 'scripts/lyft_concierge_playwright.py', 
         '--pickup', pickup, 
         '--destination', destination,
         '--dry-run',
         '--cdp-url', 'http://127.0.0.1:9222'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    log("✅ Both processes launched!")
    log("⏳ Waiting for results...")
    log("")
    
    # Wait for both to complete
    try:
        uber_stdout, uber_stderr = uber_process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        uber_process.kill()
        uber_stdout, uber_stderr = uber_process.communicate()
        log("❌ Uber timed out after 90 seconds")
    
    try:
        lyft_stdout, lyft_stderr = lyft_process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        lyft_process.kill()
        lyft_stdout, lyft_stderr = lyft_process.communicate()
        log("❌ Lyft timed out after 90 seconds")
    
    log("=" * 70)
    log("📊 Both platforms completed!")
    log("=" * 70)
    
    # Parse Uber results
    uber_rides = []
    try:
        uber_lines = uber_stdout.strip().split('\n')
        if not uber_lines or not uber_lines[-1]:
            log(f"❌ Uber returned no output")
            log(f"   STDERR: {uber_stderr[:500]}")
        else:
            uber_json = uber_lines[-1]  # Last line should be JSON
            uber_data = json.loads(uber_json)
            if uber_data.get('success'):
                uber_rides = uber_data.get('rides', [])
                for ride in uber_rides:
                    ride['platform'] = 'Uber'
                    ride['full_type'] = f"Uber {ride['type']}"
                log(f"✅ Uber: {len(uber_rides)} options")
            else:
                log(f"❌ Uber error: {uber_data.get('error')}")
                if uber_stderr:
                    log(f"   STDERR: {uber_stderr[:500]}")
    except Exception as e:
        log(f"❌ Uber parsing error: {e}")
        log(f"   Last line: {uber_lines[-1] if uber_lines else 'NONE'}")
        log(f"   STDERR: {uber_stderr[:500]}")
    
    # Parse Lyft results
    lyft_rides = []
    try:
        lyft_lines = lyft_stdout.strip().split('\n')
        lyft_json = lyft_lines[-1]  # Last line should be JSON
        lyft_data = json.loads(lyft_json)
        if lyft_data.get('success'):
            lyft_rides = lyft_data.get('rides', [])
            for ride in lyft_rides:
                ride['platform'] = 'Lyft'
                ride['full_type'] = f"Lyft {ride['type']}"
            log(f"✅ Lyft: {len(lyft_rides)} options")
            if not lyft_rides and lyft_stderr:
                log(f"   STDERR: {lyft_stderr[:500]}")
        else:
            log(f"❌ Lyft error: {lyft_data.get('error', 'Unknown error')}")
            if lyft_stderr:
                log(f"   STDERR: {lyft_stderr[:500]}")
    except Exception as e:
        log(f"❌ Lyft parsing error: {e}")
        if lyft_stderr:
            log(f"   STDERR: {lyft_stderr[:500]}")
    
    # Combine all rides
    all_rides = uber_rides + lyft_rides
    
    if not all_rides:
        result = {
            'success': False,
            'error': 'No rides available from either platform'
        }
        print(json.dumps(result, indent=2))
        sys.exit(1)
    
    log("")
    log("=" * 70)
    log("🔍 Comparing all options:")
    log("=" * 70)
    for ride in all_rides:
        log(f"  {ride['full_type']}: ${ride['price']:.2f}, {ride['eta_min']} min")
    
    # Find best ride (cheapest within 8 min threshold)
    acceptable_rides = [r for r in all_rides if r['eta_min'] <= 8]
    
    if acceptable_rides:
        cheapest_acceptable = min(acceptable_rides, key=lambda r: r['price'])
        near_tie_rides = [
            r for r in acceptable_rides
            if r['price'] <= cheapest_acceptable['price'] + PRICE_TIE_THRESHOLD
        ]
        
        if len(near_tie_rides) > 1:
            best_ride = min(near_tie_rides, key=lambda r: (r['eta_min'], r['price']))
            reason = (
                f"Picked fastest ride within ${PRICE_TIE_THRESHOLD:.2f} of cheapest "
                f"acceptable option"
            )
        else:
            best_ride = cheapest_acceptable
            reason = f"Cheapest option within 8 min threshold"
    else:
        cheapest_fallback = min(all_rides, key=lambda r: r['price'])
        near_tie_rides = [
            r for r in all_rides
            if r['price'] <= cheapest_fallback['price'] + PRICE_TIE_THRESHOLD
        ]
        
        if len(near_tie_rides) > 1:
            best_ride = min(near_tie_rides, key=lambda r: (r['eta_min'], r['price']))
            reason = (
                f"All rides exceed 8 min threshold; picked fastest ride within "
                f"${PRICE_TIE_THRESHOLD:.2f} of cheapest option"
            )
        else:
            best_ride = cheapest_fallback
            reason = f"All rides exceed 8 min threshold, picking cheapest"
     
    log("")
    log("=" * 70)
    log(f"🏆 WINNER: {best_ride['full_type']}")
    log(f"   Price: ${best_ride['price']:.2f}")
    log(f"   ETA: {best_ride['eta_min']} min")
    log(f"   Reason: {reason}")
    log("=" * 70)
    log("")
    log("🚀 Booking the winning ride...")
    log("=" * 70)
    
    # Book the winning ride
    if best_ride['platform'] == 'Uber':
        # Book on Uber with force_ride_type
        booking_process = subprocess.run(
            ['python3', 'scripts/uber_central_playwright.py', 
             pickup, destination,
             '--force-ride-type', best_ride['type']],
            capture_output=True,
            text=True,
            timeout=120
        )
    elif best_ride['platform'] == 'Lyft':
        # For Lyft, the page is already showing options - just click and confirm
        log(f"   Clicking Lyft {best_ride['type']} on existing page...")
        booking_process = subprocess.run(
            ['python3', 'scripts/lyft_finalize_booking.py', best_ride['type']],
            capture_output=True,
            text=True,
            timeout=30
        )
    
    # Check booking result
    if booking_process.returncode == 0:
        log("✅ Ride booked successfully!")
        booking_success = True
    else:
        log(f"❌ Booking failed!")
        log(f"   STDERR: {booking_process.stderr[:500]}")
        booking_success = False
    
    log("=" * 70)
    
    # Return result
    result = {
        'success': booking_success,
        'action': 'confirmed' if booking_success else 'failed',
        'selected_ride': {
            'platform': best_ride['platform'],
            'type': best_ride['type'],
            'full_type': best_ride['full_type'],
            'price': best_ride['price'],
            'eta_min': best_ride['eta_min']
        },
        'all_options': [{k: v for k, v in r.items() if k != 'element'} for r in all_rides],
        'reason': reason,
        'pickup': pickup,
        'destination': destination
    }
    
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
