#!/usr/bin/env python3
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import json
import os
from pathlib import Path
from threading import Lock

load_dotenv()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FLIGHT_SMS_PORT = int(os.getenv("FLIGHT_SMS_PORT", "5002"))
FLIGHT_SMS_STATE_FILE = Path(
    os.getenv(
        "FLIGHT_SMS_STATE_FILE",
        "/Users/adammarq/CascadeProjects/openclaw-inbox-agent/data/flight_sms_sessions.json",
    )
)

SESSION_LOCK = Lock()

QUESTIONS = [
    {
        "key": "trip_type",
        "prompt": "✈️ Flight intake started. One-way or round-trip?",
        "parser": "trip_type",
    },
    {
        "key": "origin",
        "prompt": "What is the origin airport or city?",
        "parser": "text",
    },
    {
        "key": "destination",
        "prompt": "What is the destination airport or city?",
        "parser": "text",
    },
    {
        "key": "depart_date",
        "prompt": "What is the departure date?",
        "parser": "text",
    },
    {
        "key": "return_date",
        "prompt": "What is the return date? If one-way, reply 'skip'.",
        "parser": "return_date",
    },
    {
        "key": "nonstop_required",
        "prompt": "Nonstop required? Reply yes or no.",
        "parser": "yes_no",
    },
    {
        "key": "refundable_required",
        "prompt": "Refundable required? Reply yes or no.",
        "parser": "yes_no",
    },
    {
        "key": "changes_outbound",
        "prompt": "Are flexible changes important on the outbound flight? Reply yes or no.",
        "parser": "yes_no",
    },
    {
        "key": "changes_return",
        "prompt": "Are flexible changes important on the return flight? Reply yes or no.",
        "parser": "yes_no",
    },
    {
        "key": "seat_preference",
        "prompt": "Seat preference? For example: aisle, window, no preference, seat choice included.",
        "parser": "text",
    },
    {
        "key": "early_boarding",
        "prompt": "Need early boarding / overhead bin priority? Reply yes or no.",
        "parser": "yes_no",
    },
    {
        "key": "bags",
        "prompt": "Carry-on or checked bag?",
        "parser": "bag_type",
    },
    {
        "key": "preferred_airline",
        "prompt": "Preferred airline? Reply with an airline name or 'none'.",
        "parser": "optional_text",
    },
    {
        "key": "avoid_airline",
        "prompt": "Any airline to avoid? Reply with an airline name or 'none'.",
        "parser": "optional_text",
    },
    {
        "key": "miles_status",
        "prompt": "Do miles or status matter? Reply with details or 'no'.",
        "parser": "optional_text",
    },
    {
        "key": "auto_book",
        "prompt": "Auto-book threshold or ask first? Reply 'auto' or 'ask'.",
        "parser": "auto_book",
    },
    {
        "key": "anything_else",
        "prompt": "Anything else I should know? Reply with notes or 'none'.",
        "parser": "optional_text",
    },
]


def normalize_text(value):
    return " ".join(value.strip().split())


YES = {"yes", "y", "true", "required"}
NO = {"no", "n", "false", "not required"}


def parse_yes_no(value):
    lowered = normalize_text(value).lower()
    if lowered in YES:
        return True
    if lowered in NO:
        return False
    raise ValueError("Please reply yes or no.")


def parse_trip_type(value):
    lowered = normalize_text(value).lower()
    if lowered in {"one-way", "one way", "oneway"}:
        return "one-way"
    if lowered in {"round-trip", "round trip", "roundtrip"}:
        return "round-trip"
    raise ValueError("Reply 'one-way' or 'round-trip'.")


def parse_return_date(value, session):
    lowered = normalize_text(value).lower()
    if session.get("answers", {}).get("trip_type") == "one-way":
        if lowered in {"skip", "none", "n/a", "na"}:
            return None
        return normalize_text(value)
    if lowered in {"skip", "none", "n/a", "na"}:
        raise ValueError("For a round-trip, please provide the return date.")
    return normalize_text(value)


def parse_bag_type(value):
    lowered = normalize_text(value).lower()
    if "checked" in lowered:
        return "checked"
    if "carry" in lowered:
        return "carry-on"
    raise ValueError("Reply 'carry-on' or 'checked'.")


def parse_optional_text(value):
    lowered = normalize_text(value).lower()
    if lowered in {"none", "no", "n/a", "na", "skip"}:
        return None
    return normalize_text(value)


def parse_auto_book(value):
    lowered = normalize_text(value).lower()
    if lowered in {"ask", "ask first", "confirm", "manual"}:
        return "ask"
    if lowered in {"auto", "auto-book", "autobook"}:
        return "auto"
    raise ValueError("Reply 'ask' or 'auto'.")


def parse_answer(question, value, session):
    parser = question["parser"]
    if parser == "text":
        cleaned = normalize_text(value)
        if not cleaned:
            raise ValueError("Please provide a value.")
        return cleaned
    if parser == "optional_text":
        return parse_optional_text(value)
    if parser == "yes_no":
        return parse_yes_no(value)
    if parser == "trip_type":
        return parse_trip_type(value)
    if parser == "return_date":
        return parse_return_date(value, session)
    if parser == "bag_type":
        return parse_bag_type(value)
    if parser == "auto_book":
        return parse_auto_book(value)
    raise ValueError("Unsupported parser")


def ensure_state_file():
    FLIGHT_SMS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not FLIGHT_SMS_STATE_FILE.exists():
        FLIGHT_SMS_STATE_FILE.write_text("{}")


def load_sessions():
    ensure_state_file()
    with SESSION_LOCK:
        try:
            return json.loads(FLIGHT_SMS_STATE_FILE.read_text() or "{}")
        except json.JSONDecodeError:
            return {}


def save_sessions(sessions):
    ensure_state_file()
    with SESSION_LOCK:
        FLIGHT_SMS_STATE_FILE.write_text(json.dumps(sessions, indent=2, sort_keys=True))


def get_session(from_number):
    sessions = load_sessions()
    session = sessions.get(from_number)
    if not session:
        session = {
            "question_index": 0,
            "answers": {
                "nonstop_required": False,
                "refundable_required": False,
                "changes_outbound": False,
                "changes_return": True,
                "seat_preference": "seat choice included",
                "early_boarding": False,
                "bags": "checked",
                "auto_book": "ask",
            },
        }
        sessions[from_number] = session
        save_sessions(sessions)
    return session


def update_session(from_number, session):
    sessions = load_sessions()
    sessions[from_number] = session
    save_sessions(sessions)


def reset_session(from_number):
    sessions = load_sessions()
    sessions.pop(from_number, None)
    save_sessions(sessions)


def next_unanswered_index(session):
    answers = session.get("answers", {})
    for index, question in enumerate(QUESTIONS):
        if question["key"] not in answers:
            return index
    return len(QUESTIONS)


def format_value(value):
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value in (None, ""):
        return "none"
    return str(value)


def build_summary(session):
    answers = session.get("answers", {})
    ordered_keys = [q["key"] for q in QUESTIONS]
    lines = ["✈️ Flight intake complete:"]
    for key in ordered_keys:
        if key in answers:
            label = key.replace("_", " ")
            lines.append(f"- {label}: {format_value(answers[key])}")
    lines.append("")
    lines.append("Reply 'edit <field>' to change something or 'search' when you want me to wire this into flight search next.")
    return "\n".join(lines)


def find_question_by_key(field_name):
    normalized = field_name.strip().lower().replace("-", "_").replace(" ", "_")
    for index, question in enumerate(QUESTIONS):
        if question["key"] == normalized:
            return index, question
    return None, None


@app.route('/sms', methods=['POST'])
def sms_webhook():
    from_number = request.form.get('From') or 'unknown'
    message_body = normalize_text(request.form.get('Body', ''))
    response = MessagingResponse()

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        response.message('❌ Twilio credentials are not configured on the server.')
        return str(response)

    lowered = message_body.lower()
    if lowered in {'reset', 'restart', 'start over'}:
        reset_session(from_number)
        session = get_session(from_number)
        response.message(QUESTIONS[0]['prompt'])
        return str(response)

    session = get_session(from_number)
    answers = session.setdefault('answers', {})

    if lowered in {'hi', 'hello', 'start', 'flight', 'flights'}:
        session['question_index'] = next_unanswered_index(session)
        update_session(from_number, session)
        if session['question_index'] >= len(QUESTIONS):
            response.message(build_summary(session))
        else:
            response.message(QUESTIONS[session['question_index']]['prompt'])
        return str(response)

    if lowered.startswith('edit '):
        field = message_body[5:]
        index, question = find_question_by_key(field)
        if question is None:
            response.message("❌ Unknown field. Example: edit return_date")
            return str(response)
        answers.pop(question['key'], None)
        session['question_index'] = index
        update_session(from_number, session)
        response.message(question['prompt'])
        return str(response)

    if lowered == 'status':
        response.message(build_summary(session))
        return str(response)

    question_index = next_unanswered_index(session)
    session['question_index'] = question_index

    if question_index >= len(QUESTIONS):
        response.message(build_summary(session))
        return str(response)

    question = QUESTIONS[question_index]
    try:
        answers[question['key']] = parse_answer(question, message_body, session)
    except ValueError as exc:
        response.message(f"❌ {exc} {question['prompt']}")
        return str(response)

    if question['key'] == 'trip_type' and answers['trip_type'] == 'one-way':
        answers['return_date'] = None

    next_index = next_unanswered_index(session)
    session['question_index'] = next_index
    update_session(from_number, session)

    if next_index >= len(QUESTIONS):
        response.message(build_summary(session))
    else:
        response.message(QUESTIONS[next_index]['prompt'])
    return str(response)


@app.route('/health', methods=['GET'])
def health():
    return {'status': 'ok', 'service': 'flight_sms_webhook'}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=FLIGHT_SMS_PORT, debug=False, use_reloader=False)
