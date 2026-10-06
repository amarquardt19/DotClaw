#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from datetime import datetime

LOG_FILE = "/Users/adammarq/CascadeProjects/openclaw-inbox-agent/ride_orchestrator.log"
PRICE_TIE_THRESHOLD = 0.75
LYFT_CDP_URL = os.getenv("EMMYCAR_LYFT_CDP_URL", "http://127.0.0.1:9333")
UBER_CDP_PORT = os.getenv("EMMYCAR_BROWSER_CDP_PORT", "9333")
RIDE_TYPE_PENALTIES = {
    "standard": 0.0,
    "uberx": 0.0,
    "wait & save": 1.5,
    "priority": 3.0,
    "xl": 3.0,
    "pet": 4.0,
}


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] [ORCHESTRATOR_V2] {msg}"
    print(f"[ORCHESTRATOR_V2] {msg}", file=sys.stderr, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(log_msg + "\n")


def ride_penalty(ride):
    return RIDE_TYPE_PENALTIES.get(ride["type"].strip().lower(), 0.75)


def ride_score(ride):
    return ride["price"] + ride_penalty(ride)


def log_ride_options(title, rides):
    log(title)
    for ride in rides:
        log(
            f"  {ride['full_type']}: ${ride['price']:.2f}, {ride['eta_min']} min, "
            f"penalty {ride_penalty(ride):.2f}, score {ride_score(ride):.2f}"
        )


def choose_best_ride(all_rides):
    acceptable_rides = [r for r in all_rides if r["eta_min"] <= 8]
    pool = acceptable_rides if acceptable_rides else all_rides
    log_ride_options("All evaluated ride options:", pool)
    best_scored = min(pool, key=lambda r: (ride_score(r), r["eta_min"], r["price"]))
    near_ties = [r for r in pool if ride_score(r) <= ride_score(best_scored) + PRICE_TIE_THRESHOLD]
    if len(near_ties) > 1:
        log_ride_options("Near-tied ride options:", near_ties)
        return min(near_ties, key=lambda r: (ride_penalty(r), r["eta_min"], r["price"]))
    return best_scored


def parse_result(stdout, stderr, platform):
    lines = stdout.strip().split("\n") if stdout.strip() else []
    if not lines:
        raise RuntimeError(f"{platform} returned no output. STDERR: {stderr[:500]}")
    payload = json.loads(lines[-1])
    if not payload.get("success"):
        raise RuntimeError(f"{platform} error: {payload.get('error', 'Unknown error')}")
    rides = payload.get("rides", [])
    for ride in rides:
        ride["platform"] = platform
        ride["full_type"] = f"{platform} {ride['type']}"
    return rides


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"success": False, "error": "Usage: python ride_orchestrator_v2.py <pickup> <destination>"}))
        sys.exit(1)

    pickup = sys.argv[1]
    destination = sys.argv[2]
    env = os.environ.copy()
    env["EMMYCAR_BROWSER_CDP_PORT"] = UBER_CDP_PORT
    env["EMMYCAR_LYFT_CDP_URL"] = LYFT_CDP_URL

    log(f"Starting ride comparison: {pickup} → {destination}")

    uber_process = subprocess.Popen(
        ["python3", "scripts/uber_central_playwright_v2.py", pickup, destination, "--dry-run"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    lyft_process = subprocess.Popen(
        ["python3", "scripts/lyft_concierge_playwright.py", "--pickup", pickup, "--destination", destination, "--dry-run", "--cdp-url", LYFT_CDP_URL],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    uber_stdout, uber_stderr = uber_process.communicate(timeout=120)
    lyft_stdout, lyft_stderr = lyft_process.communicate(timeout=120)

    uber_rides = parse_result(uber_stdout, uber_stderr, "Uber")
    lyft_rides = parse_result(lyft_stdout, lyft_stderr, "Lyft")
    all_rides = uber_rides + lyft_rides

    if not all_rides:
        print(json.dumps({"success": False, "error": "No rides available from either platform"}))
        sys.exit(1)

    log_ride_options("All returned ride options:", all_rides)
    best_ride = choose_best_ride(all_rides)
    log(
        f"Winner: {best_ride['full_type']} ${best_ride['price']:.2f} {best_ride['eta_min']} min "
        f"(score {ride_score(best_ride):.2f})"
    )

    if best_ride["platform"] == "Uber":
        booking_process = subprocess.run(
            ["python3", "scripts/uber_central_playwright_v2.py", pickup, destination, "--force-ride-type", best_ride["type"]],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    else:
        booking_process = subprocess.run(
            ["python3", "scripts/lyft_finalize_booking_v2.py", best_ride["type"]],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    success = booking_process.returncode == 0
    result = {
        "success": success,
        "action": "confirmed" if success else "failed",
        "selected_ride": {
            "platform": best_ride["platform"],
            "type": best_ride["type"],
            "full_type": best_ride["full_type"],
            "price": best_ride["price"],
            "eta_min": best_ride["eta_min"],
        },
        "all_options": all_rides,
        "pickup": pickup,
        "destination": destination,
    }
    if not success:
        result["error"] = (booking_process.stderr or booking_process.stdout or "Booking failed")[:500]
    print(json.dumps(result))


if __name__ == "__main__":
    main()
