#!/usr/bin/env python3
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import os
import re

from flights_search_service import build_search_request, fetch_flights, format_results

load_dotenv()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FLIGHT_SMS_PORT = int(os.getenv("FLIGHT_SMS_PORT", "5002"))

HELP_TEXT = (
    "✈️ Text a search like: RIC to CDG May 26\n"
    "Round-trip also works: RIC to CDG May 26 to Jun 2"
)


def normalize_text(value):
    return " ".join((value or "").strip().split())


def parse_search_message(message_body):
    text = normalize_text(message_body)
    match = re.match(r"^([A-Za-z]{3})\s+to\s+([A-Za-z]{3})\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        raise ValueError(HELP_TEXT)

    origin = match.group(1).upper()
    destination = match.group(2).upper()
    remainder = match.group(3).strip()
    split = re.split(r"\s+to\s+", remainder, maxsplit=1, flags=re.IGNORECASE)
    depart_date = split[0].strip()
    return_date = split[1].strip() if len(split) > 1 else None

    return {
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date,
        "return_date": return_date,
        "nonstop_required": False,
        "refundable_required": False,
        "changes_outbound": False,
        "changes_return": True,
        "seat_preference": "seat choice included",
        "early_boarding": False,
        "bags": "checked",
        "preferred_airline": None,
        "avoid_airline": None,
        "miles_status": None,
        "auto_book": "ask",
        "anything_else": None,
    }


@app.route("/sms", methods=["POST"])
def sms_webhook():
    message_body = normalize_text(request.form.get("Body", ""))
    response = MessagingResponse()

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        response.message("❌ Twilio credentials are not configured on the server.")
        return str(response)

    lowered = message_body.lower()
    if lowered in {"hi", "hello", "start", "help", "flight", "flights"}:
        response.message(HELP_TEXT)
        return str(response)

    try:
        answers = parse_search_message(message_body)
        search_request = build_search_request(answers)
        results = fetch_flights(search_request)
        response.message(format_results(search_request, results))
    except Exception as exc:
        response.message(f"❌ {str(exc)[:300]}")
    return str(response)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "flight_sms_webhook_fast"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLIGHT_SMS_PORT, debug=False, use_reloader=False)
