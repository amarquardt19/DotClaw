#!/usr/bin/env python3
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

load_dotenv()

app = Flask(__name__)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
HOME_ADDRESS = os.getenv("HOME_ADDRESS", "2810 Kensington Ave, Richmond VA")
EMMYCAR_BOT_TOKEN = os.getenv("EMMYCAR_BOT_TOKEN", "")
EMMYCAR_BOT_CHAT_ID = os.getenv("EMMYCAR_BOT_CHAT_ID", "")


def parse_sms_command(message):
    message = message.strip()
    message = re.sub(r'^(lyft|ride|uber|take me|go)\s+', '', message, flags=re.IGNORECASE)
    to_match = re.search(r'(.+?)\s+to\s+(.+)', message, re.IGNORECASE)
    if to_match:
        return {"pickup": to_match.group(1).strip(), "destination": to_match.group(2).strip()}
    return {"pickup": HOME_ADDRESS, "destination": message.strip()}


def send_telegram_message(message):
    if not EMMYCAR_BOT_TOKEN or not EMMYCAR_BOT_CHAT_ID:
        print("[telegram] EmmyCar Telegram credentials not configured")
        return False
    payload = urllib.parse.urlencode(
        {
            "chat_id": EMMYCAR_BOT_CHAT_ID,
            "text": message[:4000],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{EMMYCAR_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        print(f"[telegram] http error {e.code}: {e.read().decode('utf-8', errors='ignore')[:300]}")
    except Exception as e:
        print(f"[telegram] send failed: {e}")
    return False


def process_ride_request(from_number, pickup, destination):
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
        stderr = result.stderr.strip()
        if stderr:
            print(f"[ride_orchestrator_v2 stderr] {stderr[:1000]}")
        if not output:
            send_telegram_message(
                f"❌ Emmy Cars\nFrom: {from_number}\nPickup: {pickup}\nDestination: {destination}\n\nUnable to book ride: empty response from ride orchestrator"
            )
            return
        payload = json.loads(output.split("\n")[-1])
        if payload.get("success"):
            ride = payload.get("selected_ride", {})
            send_telegram_message(
                f"✅ Emmy Cars booked\nFrom: {from_number}\nPickup: {pickup}\nDestination: {destination}\n\n{ride.get('full_type', 'ride')} for ${ride.get('price', 0):.2f} ({ride.get('eta_min', '?')} min away)"
            )
        else:
            send_telegram_message(
                f"❌ Emmy Cars booking failed\nFrom: {from_number}\nPickup: {pickup}\nDestination: {destination}\n\n{payload.get('error', 'Unknown error')}"
            )
    except Exception as e:
        send_telegram_message(
            f"❌ Emmy Cars booking exception\nFrom: {from_number}\nPickup: {pickup}\nDestination: {destination}\n\n{str(e)[:250]}"
        )


@app.route('/sms', methods=['POST'])
def sms_webhook():
    from_number = request.form.get('From')
    message_body = request.form.get('Body', '')
    try:
        command = parse_sms_command(message_body)
        pickup = command['pickup']
        destination = command['destination']
        if not destination:
            resp = MessagingResponse()
            resp.message("❌ Please specify a destination. Example: 'Lyft to 3001 W Leigh St'")
            return str(resp)
        send_telegram_message(
            f"🚗 Emmy Cars request received\nFrom: {from_number}\nPickup: {pickup}\nDestination: {destination}\n\nComparing Uber and Lyft now."
        )
        resp = MessagingResponse()
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
