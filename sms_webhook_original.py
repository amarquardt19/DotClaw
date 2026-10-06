#!/usr/bin/env python3
"""
SMS webhook for Lyft Concierge bot
Receives SMS from Twilio, runs Lyft automation, sends confirmation
"""

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import os
import json
import subprocess
import re
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Twilio credentials
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "+18333504966")

# Default pickup address
HOME_ADDRESS = os.getenv("HOME_ADDRESS", "2810 Kensington Ave, Richmond VA")

def parse_sms_command(message):
    """
    Parse SMS message to extract pickup and destination
    
    Examples:
    - "Lyft to 3001 W Leigh St" -> pickup=HOME, dest=3001 W Leigh St
    - "2810 kensington to fighting fish" -> pickup=2810 kensington, dest=fighting fish
    - "fighting fish" -> pickup=HOME, dest=fighting fish
    """
    message = message.strip()
    
    # Remove common prefixes
    message = re.sub(r'^(lyft|ride|uber|take me|go)\s+', '', message, flags=re.IGNORECASE)
    
    # Check for "X to Y" pattern
    to_match = re.search(r'(.+?)\s+to\s+(.+)', message, re.IGNORECASE)
    if to_match:
        pickup = to_match.group(1).strip()
        destination = to_match.group(2).strip()
    else:
        # No "to" - assume destination only, use home as pickup
        pickup = HOME_ADDRESS
        destination = message.strip()
    
    return {
        "pickup": pickup,
        "destination": destination
    }

@app.route("/sms", methods=["POST"])
def sms_webhook():
    """Handle incoming SMS from Twilio"""
    
    # Get message details
    from_number = request.form.get("From")
    message_body = request.form.get("Body", "")
    
    print(f"[SMS] Received from {from_number}: {message_body}")
    
    # Parse command
    try:
        command = parse_sms_command(message_body)
        pickup = command["pickup"]
        destination = command["destination"]
        
        if not destination:
            resp = MessagingResponse()
            resp.message("❌ Please specify a destination. Example: 'Lyft to 3001 W Leigh St'")
            return str(resp)
        
        # Send acknowledgment
        resp = MessagingResponse()
        resp.message(f"🚗 Booking Lyft to {destination}...")
        
        # Run Lyft automation in background
        script_path = os.path.join(os.path.dirname(__file__), "scripts", "lyft_concierge_playwright.py")
        print(f"[DEBUG] Running script: {script_path}")
        print(f"[DEBUG] Pickup: {pickup}, Destination: {destination}")
        
        result = subprocess.run(
            ["python3", script_path, "--pickup", pickup, "--destination", destination, "--auto-confirm"],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        print(f"[DEBUG] Script exit code: {result.returncode}")
        print(f"[DEBUG] Script stdout: {result.stdout[:500]}")
        print(f"[DEBUG] Script stderr: {result.stderr[:500]}")
        
        # Parse result
        output = result.stdout
        try:
            # Find JSON output - look for complete JSON object
            json_start = output.rfind("{")
            if json_start != -1:
                # Find the matching closing brace
                json_str = output[json_start:]
                # Try to parse incrementally to find valid JSON
                brace_count = 0
                json_end = 0
                for i, char in enumerate(json_str):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            json_end = i + 1
                            break
                
                if json_end > 0:
                    json_str = json_str[:json_end]
                
                data = json.loads(json_str)
                
                if data.get("auto_confirmed"):
                    ride = data["rides"][0] if data.get("rides") else {}
                    price = ride.get("price", "?")
                    eta = ride.get("eta_min", "?")
                    ride_type = data.get("confirmed_ride", "Standard")
                    pickup = data.get("pickup_resolved", {}).get("canonical", data.get("pickup", ""))
                    destination = data.get("destination_resolved", {}).get("canonical", data.get("destination", ""))
                    reason = data.get("auto_confirm_reason", "")
                    
                    # Build confirmation message
                    message = f"✅ Lyft {ride_type} requested!\n\n"
                    message += f"📍 From: {pickup}\n"
                    message += f"📍 To: {destination}\n\n"
                    message += f"💰 Price: ${price}\n"
                    message += f"⏱️ Pickup: {eta} min\n\n"
                    if reason:
                        message += f"Auto-confirmed: {reason}"
                    
                    # Send confirmation
                    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                    client.messages.create(
                        body=message,
                        from_=TWILIO_PHONE_NUMBER,
                        to=from_number
                    )
                else:
                    reason = data.get("auto_confirm_reason", "Unknown reason")
                    rides = data.get("rides", [])
                    
                    if rides:
                        ride_list = "\n".join([f"- {r['type']}: ${r['price']} ({r['eta_min']} min)" for r in rides])
                        message = f"⚠️ Manual confirmation needed:\n{reason}\n\nOptions:\n{ride_list}"
                    else:
                        message = f"❌ No rides available: {reason}"
                    
                    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                    client.messages.create(
                        body=message,
                        from_=TWILIO_PHONE_NUMBER,
                        to=from_number
                    )
        except Exception as e:
            print(f"Error parsing result: {e}")
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.messages.create(
                body=f"❌ Error booking ride: {str(e)}",
                from_=TWILIO_PHONE_NUMBER,
                to=from_number
            )
        
        return str(resp)
        
    except Exception as e:
        print(f"Error: {e}")
        resp = MessagingResponse()
        resp.message(f"❌ Error: {str(e)}")
        return str(resp)

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    print(f"Starting SMS webhook on port {port}...")
    print(f"Twilio Account SID: {TWILIO_ACCOUNT_SID or 'NOT SET'}")
    print(f"Twilio Auth Token: {'SET' if TWILIO_AUTH_TOKEN else 'NOT SET'}")
    print(f"Twilio Phone: {TWILIO_PHONE_NUMBER or 'NOT SET'}")
    
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("WARNING: Twilio credentials not set! Check your .env file")
    
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
