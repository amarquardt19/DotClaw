#!/usr/bin/env python3
from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import json
import os
import re
import subprocess
import threading

load_dotenv()

app = Flask(__name__)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "+18333504966")
HOME_ADDRESS = os.getenv("HOME_ADDRESS", "2810 Kensington Ave, Richmond VA")


def parse_sms_command(message):
    message = message.strip()
    message = re.sub(r'^(lyft|ride|uber|take me|go)\s+', '', message, flags=re.IGNORECASE)
    to_match = re.search(r'(.+?)\s+to\s+(.+)', message, re.IGNORECASE)
    if to_match:
        return {"pickup": to_match.group(1).strip(), "destination": to_match.group(2).strip()}
    return {"pickup": HOME_ADDRESS, "destination": message.strip()}


def send_sms(to_number, message):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(body=message, from_=TWILIO_PHONE_NUMBER, to=to_number)


def process_ride_request(to_number, pickup, destination):
    script_path = os.path.join(os.path.dirname(__file__), "scripts", "ride_orchestrator_v2.py")
    try:
        result = subprocess.run(
            ["python3", script_path, pickup, destination],
            capture_output=True,
            text=True,
            timeout=240,
            cwd=os.path.dirname(__file__),
            env={**os.environ, "EMMYCAR_BROWSER_CDP_PORT": "9333", "EMMYCAR_LYFT_CDP_URL": "http://127.0.0.1:9333"},
        )
        output = result.stdout.strip()
        if not output:
            send_sms(to_number, "❌ Unable to book ride: empty response from ride orchestrator")
            return
        payload = json.loads(output.split("\n")[-1])
        if payload.get("success"):
            ride = payload.get("selected_ride", {})
            send_sms(to_number, f"✅ Booked {ride.get('full_type', 'ride')} for ${ride.get('price', 0):.2f} ({ride.get('eta_min', '?')} min away)")
        else:
            send_sms(to_number, f"❌ Unable to book ride: {payload.get('error', 'Unknown error')}")
    except Exception as e:
        send_sms(to_number, f"❌ Unable to book ride: {str(e)[:250]}")


@app.route('/sms', methods=['POST'])
def sms_webhook():
    from_number = request.form.get('From')
    message_body = request.form.get('Body', '')
    try:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            resp = MessagingResponse()
            resp.message('❌ Twilio credentials are not configured on the webhook server')
            return str(resp)
        command = parse_sms_command(message_body)
        pickup = command['pickup']
        destination = command['destination']
        if not destination:
            resp = MessagingResponse()
            resp.message("❌ Please specify a destination. Example: 'Lyft to 3001 W Leigh St'")
            return str(resp)
        resp = MessagingResponse()
        resp.message(f'🚗 Comparing Uber & Lyft to {destination}...')
        threading.Thread(target=process_ride_request, args=(from_number, pickup, destination), daemon=True).start()
        return str(resp)
    except Exception as e:
        resp = MessagingResponse()
        resp.message(f'❌ Error: {str(e)}')
        return str(resp)


@app.route('/health', methods=['GET'])
def health():
    return {'status': 'ok'}


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
