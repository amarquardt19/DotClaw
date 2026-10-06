#!/usr/bin/env python3
"""
Dead Simple Uber Telegram Bot
Just passes your message to the Playwright script that works perfectly.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import requests

# Load env
env = Path(__file__).parent / ".env"
for line in env.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

TOKEN = os.environ.get("EMMYCAR_BOT_TOKEN", "")
CHAT = int(os.environ.get("EMMYCAR_BOT_CHAT_ID", "0"))
HOME = os.environ.get("EMMYCAR_HOME_ADDRESS", "2835 Clay Street, San Francisco, CA 94115")
PORT = os.environ.get("EMMYCAR_BROWSER_CDP_PORT", "18801")
SCRIPT = Path(__file__).parent / "scripts" / "uber_playwright_attach.py"

API = f"https://api.telegram.org/bot{TOKEN}"
offset = 0
state = {}  # Track conversation state

def send(text):
    requests.post(f"{API}/sendMessage", json={"chat_id": CHAT, "text": text[:4000]})

def photo(path, caption=""):
    with open(path, "rb") as f:
        requests.post(f"{API}/sendPhoto", data={"chat_id": CHAT, "caption": caption}, files={"photo": f})

def run(command=None, pickup=None, destination=None, select_ride=None):
    """Run Playwright script, return JSON"""
    cmd = ["python3", str(SCRIPT), "--cdp-url", f"http://127.0.0.1:{PORT}"]
    
    if select_ride:
        # Select a specific ride (stops before clicking Request)
        cmd.extend(["--select-ride", select_ride])
    elif pickup and destination:
        # Just get fares, don't select anything
        cmd.extend(["--pickup", pickup, "--destination", destination, "--extract-fares", "--google-places", "--press-enter"])
    elif command:
        # Parse command but strip ride type to avoid auto-selection
        # Remove ride type from beginning
        cmd_clean = command
        for rt in ["uberx", "uberxl", "comfort", "black", "uberxxl", "electric"]:
            if command.lower().startswith(rt):
                cmd_clean = command[len(rt):].strip()
                break
        cmd.extend(["--ride-command", cmd_clean, "--extract-fares"])
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    
    # Find JSON in output - look for the last complete JSON object
    output = result.stdout
    
    # Find the last opening brace
    last_brace = output.rfind("\n{")
    if last_brace == -1:
        last_brace = output.find("{")
    if last_brace == -1:
        return {"error": "No JSON output"}
    
    # Extract from last { to end, then find the matching closing }
    json_candidate = output[last_brace:].strip()
    
    # Find the matching closing brace by counting braces
    brace_count = 0
    json_end = -1
    for i, char in enumerate(json_candidate):
        if char == '{':
            brace_count += 1
        elif char == '}':
            brace_count -= 1
            if brace_count == 0:
                json_end = i + 1
                break
    
    if json_end == -1:
        return {"error": "No complete JSON object found"}
    
    json_str = json_candidate[:json_end]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

# Main loop
print(f"🚗 Uber bot running (chat {CHAT})")

while True:
    try:
        # Get updates
        r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
        updates = r.json().get("result", [])
        
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            text = msg.get("text", "").strip()
            
            if not text:
                continue
            
            print(f"📱 {text}")
            
            # Check if confirming a ride
            if text.lower() in ["yes", "y", "confirm"]:
                if "pending_ride" in state:
                    ride_type = state["pending_ride"]
                    pickup = state.get("pickup", "")
                    destination = state.get("destination", "")
                    
                    send(f"🚗 Requesting {ride_type.upper()}...")
                    
                    # Run script with full command to navigate, select, and request
                    command = f"{pickup} to {destination}" if pickup and destination else None
                    result = run(command=command, select_ride=ride_type)
                    
                    if result.get("rideRequested"):
                        send("✅ Ride requested! Check your Uber app for driver details.")
                    else:
                        send("⚠️ Couldn't request ride. Check browser manually.")
                    
                    state.clear()
                else:
                    send("No ride to confirm")
                continue
            
            # Remove /ride /uber prefix first
            original_text = text
            for prefix in ["/ride", "/uber"]:
                if text.lower().startswith(prefix):
                    text = text[len(prefix):].strip()
            
            # Check if selecting by number
            if text.isdigit() and "ride_list" in state:
                idx = int(text) - 1
                if 0 <= idx < len(state["ride_list"]):
                    selected = state["ride_list"][idx]
                    ride_type = selected["type"]
                    price = selected["price"]
                    send(f"💰 {ride_type} costs {price}\n\nReply 'yes' to confirm (will NOT auto-request, you'll need to click in Uber)")
                    state["pending_ride"] = ride_type.lower()
                    state["pending_price"] = price
                else:
                    send(f"❌ Invalid number. Pick 1-{len(state['ride_list'])}")
                continue
            
            # Check if selecting a ride type by name (only if we have a pending result and it's JUST the ride type)
            ride_types = ["uberx", "uberxl", "comfort", "electric", "comfort electric", "uberxxl", "black"]
            text_lower = text.lower().strip()
            
            # Only treat as ride selection if: (1) we have last_result, (2) text is ONLY a ride type (no "to" keyword)
            if text_lower in ride_types and "last_result" in state and " to " not in text_lower:
                # Find the price from the last result
                rides = state["last_result"].get("rideOptions", [])
                price = None
                for ride in rides:
                    if ride["type"].lower() == text_lower:
                        price = ride["price"]
                        break
                
                if not price:
                    send(f"❌ {text_lower.upper()} not found in options")
                    continue
                
                send(f"💰 {text_lower.upper()} costs {price}\n\nReply 'yes' to confirm (will NOT auto-request, you'll need to click in Uber)")
                state["pending_ride"] = text_lower
                state["pending_price"] = price
                continue
            
            # Otherwise treat as new ride command
            if not text or text.startswith("/"):
                send(f"Send: uberx home to sfo\n\nJust type your ride request!")
                continue
            
            send("🚗 Getting ride...")
            
            try:
                # Run script - let it handle everything (including 'home')
                result = run(text)
                
                print(f"DEBUG: Script result keys: {result.keys()}")
                print(f"DEBUG: rideOptions: {result.get('rideOptions')}")
                
                if "error" in result:
                    send(f"❌ {result['error']}")
                    continue
                
                # Save result for ride selection
                state["last_result"] = result
                
                # Format output
                pickup = result.get("pickupResolved", {}).get("formattedAddress", "?")
                dest = result.get("destinationResolved", {}).get("formattedAddress", "?")
                rides = result.get("rideOptions", [])
                
                if not rides:
                    send(f"📍 {pickup}\n→ {dest}\n\n❌ No rides found\n\nDebug: Check browser manually")
                    state.clear()
                    continue
                
                # Show unique rides
                seen = set()
                msg = f"📍 {pickup}\n→ {dest}\n\n"
                for ride in rides:
                    if ride.get("type") and ride.get("price"):
                        if ride["type"] not in seen:
                            seen.add(ride["type"])
                            msg += f"• {ride['type']} {ride['price']}\n"
                
                if len(seen) == 0:
                    send(f"📍 {pickup}\n→ {dest}\n\n⚠️ Got {len(rides)} rides but couldn't parse prices. Check browser.")
                    continue
                
                # Save pickup/dest for later
                state["pickup"] = pickup
                state["destination"] = dest
                
                # Find UberX price and ask for confirmation
                uberx_price = None
                for ride in rides:
                    if ride.get("type", "").lower() == "uberx":
                        uberx_price = ride.get("price")
                        break
                
                if uberx_price:
                    msg += f"\n💰 UberX is {uberx_price}. Y or N?"
                    state["pending_ride"] = "uberx"
                    state["pending_price"] = uberx_price
                else:
                    msg += "\n⚠️ UberX not found. Reply with ride type to select."
                
                send(msg)
                
                # Screenshot
                ss = result.get("screenshot")
                if ss and Path(ss).exists():
                    photo(ss, "Uber")
                    
            except Exception as e:
                send(f"❌ Bot error: {e}")
                print(f"ERROR: {e}")
                import traceback
                traceback.print_exc()
    
    except KeyboardInterrupt:
        print("\n👋 Stopped")
        break
    except Exception as e:
        print(f"❌ {e}")
