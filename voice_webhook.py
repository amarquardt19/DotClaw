#!/usr/bin/env python3
"""
Voice webhook for EmmyCar - receives Vapi.ai calls and triggers Uber bot
"""
import os
import json
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

SCRIPT = Path(__file__).parent / "scripts" / "uber_playwright_attach.py"
PORT = os.getenv("EMMYCAR_BROWSER_CDP_PORT", "18801")
HOME_ADDRESS = os.getenv("EMMYCAR_HOME_ADDRESS", "")

def run_uber_script(command=None, select_ride=None):
    """Run the Uber Playwright script and return JSON result"""
    cmd = ["python3", str(SCRIPT), "--cdp-url", f"http://127.0.0.1:{PORT}"]
    
    if command:
        # Strip ride type to avoid auto-selection
        cmd_clean = command
        for rt in ["uberx", "uberxl", "comfort", "black", "uberxxl", "electric"]:
            if command.lower().startswith(rt):
                cmd_clean = command[len(rt):].strip()
                break
        cmd.extend(["--ride-command", cmd_clean])
        
        if select_ride:
            # Request the ride after getting fares
            cmd.extend(["--select-ride", select_ride])
        else:
            # Just get fares
            cmd.extend(["--extract-fares"])
    elif select_ride:
        # Just select ride on current page (shouldn't happen in our flow)
        cmd.extend(["--select-ride", select_ride])
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    
    # Parse JSON from output - find the last complete JSON object
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

@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle Vapi webhook calls"""
    data = request.json
    
    # Save full payload to file for debugging
    with open("/tmp/vapi_webhook_payload.json", "w") as f:
        json.dump(data, f, indent=2)
    
    print(f"📞 Webhook received")
    print(f"🔍 Message type: {data.get('message', {}).get('type')}")
    print(f"🔍 Full message keys: {list(data.get('message', {}).keys())}")
    
    # Extract the function call from Vapi
    message_type = data.get("message", {}).get("type")
    
    # Handle both "tool-calls" and "function-call" message types
    if message_type in ["function-call", "tool-calls"]:
        # Get tool calls array
        tool_calls = data.get("message", {}).get("toolCalls", [])
        tool_call_id = None
        if tool_calls:
            tool_call = tool_calls[0]
            tool_call_id = tool_call.get("id")
            function_data = tool_call.get("function", {})
            function_name = function_data.get("name")
            parameters = function_data.get("arguments", {})
        else:
            # Fallback to old format
            function_call = data.get("message", {}).get("functionCall", {})
            function_name = function_call.get("name")
            parameters = function_call.get("parameters", {})
        
        print(f"🎯 Tool Call ID: {tool_call_id}")
        print(f"🎯 Function name: {function_name}")
        print(f"🎯 Parameters: {parameters}")
        
        if function_name in ["get_uber_fares", "call_ubers"]:
            try:
                # Get destination and pickup
                destination = parameters.get("destination", "")
                pickup = parameters.get("pickup", HOME_ADDRESS)
                confirm = parameters.get("confirm", False)
                
                if confirm:
                    # User confirmed - just select UberX and request (already on fare page)
                    print(f"🚀 REQUESTING UBERX (no navigation, already on fare page)")
                    result = run_uber_script(select_ride="uberx")
                    print(f"🚀 RIDE REQUEST RESULT: {result}")
                    
                    if result.get("rideRequested"):
                        response_text = "Ride requested. Driver arriving soon."
                    else:
                        response_text = "Unable to request ride. Please try again."
                else:
                    # Get fares for a destination
                    print(f"🚀 GETTING FARES: {pickup} → {destination}")
                    command = f"{pickup} to {destination}"
                    result = run_uber_script(command=command)
                    print(f"🚀 PLAYWRIGHT RETURNED: {result}")
                
                # Handle response based on whether this was a fare check or ride request
                if not confirm:
                    # This was a fare check
                    if "error" in result:
                        response_text = f"Error: {result['error']}"
                    else:
                        rides = result.get("rideOptions", [])
                        if not rides:
                            response_text = "No rides available"
                        else:
                            # Find UberX price
                            uberx_price = None
                            for ride in rides:
                                if ride.get("type", "").lower() == "uberx":
                                    uberx_price = ride.get("price")
                                    break
                            
                            if uberx_price:
                                response_text = f"UberX is {uberx_price}"
                            else:
                                response_text = "UberX not available"
                
                print(f"✅ Returning to Vapi: {response_text}")
                
                # Return in Vapi's expected format for synchronous tools
                if tool_call_id:
                    return jsonify({
                        "results": [{
                            "toolCallId": tool_call_id,
                            "result": response_text
                        }]
                    })
                else:
                    # Fallback for async or old format
                    return jsonify({"result": response_text})
            except Exception as e:
                print(f"❌ Error in webhook handler: {e}")
                error_response = f"Error: {str(e)}"
                if tool_call_id:
                    return jsonify({
                        "results": [{
                            "toolCallId": tool_call_id,
                            "result": error_response
                        }]
                    })
                else:
                    return jsonify({"result": error_response})
        
        elif function_name == "request_uber":
            # Request the ride
            ride_type = parameters.get("ride_type", "uberx")
            result = run_uber_script(select_ride=ride_type)
            
            if result.get("rideRequested"):
                return jsonify({
                    "result": "Ride requested successfully"
                })
            else:
                return jsonify({
                    "result": "Failed to request ride"
                })
    
    return jsonify({"status": "ok"})

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "emmycar-voice"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    print("🚗 EmmyCar Voice Webhook starting...")
    print(f"📍 Home address: {HOME_ADDRESS}")
    print(f"🌐 Webhook will be available at /webhook")
    print(f"🔌 Port: {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
