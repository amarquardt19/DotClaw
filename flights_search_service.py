#!/usr/bin/env python3
from datetime import datetime, timedelta
import re
import time

from fast_flights import FlightData, Passengers, create_filter, get_flights_from_filter

DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

FAST_FLIGHTS_RETRY_DELAYS = (0.0, 1.0, 2.5)


class FlightSearchError(RuntimeError):
    pass


def parse_date(text):
    text = text.strip().lower()
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return today.strftime("%Y-%m-%d")
    if text == "tomorrow":
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    for day_name, day_num in DAY_NAMES.items():
        if day_name in text:
            days_ahead = day_num - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = today + timedelta(days=days_ahead)
            return target.strftime("%Y-%m-%d")
    for fmt in ["%Y-%m-%d", "%b %d", "%B %d", "%m/%d"]:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt != "%Y-%m-%d":
                parsed = parsed.replace(year=now.year)
                if parsed < now:
                    parsed = parsed.replace(year=now.year + 1)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_airport(value):
    cleaned = re.sub(r"\s+", " ", value.strip()).upper()
    return cleaned


def build_search_request(answers):
    origin = normalize_airport(answers["origin"])
    destination = normalize_airport(answers["destination"])
    depart_date = parse_date(answers["depart_date"])
    if not depart_date:
        raise ValueError(f"Could not parse departure date: {answers['depart_date']}")

    return_date = None
    if answers.get("return_date"):
        return_date = parse_date(answers["return_date"])
        if not return_date:
            raise ValueError(f"Could not parse return date: {answers['return_date']}")

    return {
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date,
        "return_date": return_date,
        "nonstop_required": bool(answers.get("nonstop_required")),
        "seat_preference": answers.get("seat_preference") or "seat choice included",
        "bags": answers.get("bags") or "checked",
        "preferred_airline": answers.get("preferred_airline"),
        "avoid_airline": answers.get("avoid_airline"),
        "refundable_required": bool(answers.get("refundable_required")),
        "changes_outbound": bool(answers.get("changes_outbound")),
        "changes_return": bool(answers.get("changes_return")),
        "miles_status": answers.get("miles_status"),
        "auto_book": answers.get("auto_book") or "ask",
        "anything_else": answers.get("anything_else"),
    }


def fetch_flights(search_request):
    flight_data = [
        FlightData(
            date=search_request["depart_date"],
            from_airport=search_request["origin"],
            to_airport=search_request["destination"],
        )
    ]
    trip = "one-way"
    if search_request.get("return_date"):
        trip = "round-trip"
        flight_data.append(
            FlightData(
                date=search_request["return_date"],
                from_airport=search_request["destination"],
                to_airport=search_request["origin"],
            )
        )

    seat = "economy"
    seat_preference = (search_request.get("seat_preference") or "").lower()
    if "premium" in seat_preference:
        seat = "premium-economy"
    elif "business" in seat_preference:
        seat = "business"
    elif "first" in seat_preference:
        seat = "first"

    flight_filter = create_filter(
        flight_data=flight_data,
        trip=trip,
        seat=seat,
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
    )
    last_error = None
    for delay in FAST_FLIGHTS_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return get_flights_from_filter(filter=flight_filter)
        except Exception as exc:
            last_error = exc
            continue
    message = str(last_error or "Unknown Fast Flights error").strip()
    lowered = message.lower()
    if (
        "skip to main content" in lowered
        or "loading results" in lowered
        or "accessibility feedback" in lowered
        or "google" in lowered
    ):
        raise FlightSearchError(
            "Fast Flights hit a temporary Google/metadata scrape failure. Please try the same search again in a moment."
        ) from last_error
    if message:
        first_line = message.splitlines()[0][:220]
        raise FlightSearchError(f"Fast Flights search failed: {first_line}") from last_error
    raise FlightSearchError("Fast Flights search failed unexpectedly.") from last_error


def _flight_segments(result):
    flights = getattr(result, "flights", None)
    if flights:
        return list(flights)
    if hasattr(result, "departure") or hasattr(result, "arrival"):
        return [result]
    return []


def _flight_name(result):
    flights = _flight_segments(result)
    names = [getattr(f, "name", None) for f in flights if getattr(f, "name", None)]
    return " / ".join(names) if names else "Unknown airline"


def _flight_times(result):
    flights = _flight_segments(result)
    if not flights:
        return "?→?"
    first = flights[0]
    last = flights[-1]
    depart = getattr(first, "departure", None) or "?"
    arrive = getattr(last, "arrival", None) or "?"
    return f"{depart}→{arrive}"


def _flight_duration(result):
    flights = _flight_segments(result)
    if not flights:
        return "?"
    return getattr(result, "duration", None) or getattr(flights[0], "duration", None) or "?"


def _flight_duration_minutes(result):
    duration = str(_flight_duration(result) or "").strip().lower()
    if not duration:
        return None
    hours_match = re.search(r"(\d+)\s*hr", duration)
    minutes_match = re.search(r"(\d+)\s*min", duration)
    if not hours_match and not minutes_match:
        return None
    hours = int(hours_match.group(1)) if hours_match else 0
    minutes = int(minutes_match.group(1)) if minutes_match else 0
    return hours * 60 + minutes


def _compact_time(value):
    if not value:
        return "?"
    text = str(value).strip()
    match = re.match(r"^(\d{1,2}:\d{2}\s*[AP]M)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper().replace("  ", " ")
    return text


def _compact_day(value):
    if not value:
        return ""
    text = str(value).strip()
    match = re.search(r"on\s+([A-Za-z]{3}),?\s+([A-Za-z]{3}\s+\d{1,2})", text, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1).title()} {match.group(2).title()}"
    return ""


def _flight_time_summary(result):
    flights = _flight_segments(result)
    if not flights:
        return "? → ?"
    first = flights[0]
    last = flights[-1]
    depart = getattr(first, "departure", None) or "?"
    arrive = getattr(last, "arrival", None) or "?"
    depart_time = _compact_time(depart)
    arrive_time = _compact_time(arrive)
    arrive_day = _compact_day(arrive)
    if arrive_day:
        return f"{depart_time} → {arrive_time} ({arrive_day})"
    return f"{depart_time} → {arrive_time}"


def _flight_stops_count(result):
    flights = _flight_segments(result)
    if not flights:
        return None
    stops = getattr(result, "stops", None)
    if stops is None:
        stops = getattr(flights[0], "stops", None)
    if stops is None:
        return None
    try:
        return int(stops)
    except (TypeError, ValueError):
        return None


def _flight_stops_text(result):
    stops = _flight_stops_count(result)
    if stops is None:
        return "?"
    if stops == 0:
        return "Nonstop"
    if stops == 1:
        return "1 stop"
    return f"{stops} stops"


def _flight_price_value(result):
    raw = getattr(result, "current_price", None) or getattr(result, "price", None) or ""
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else 10**9


def _flight_price_text(result):
    return getattr(result, "current_price", None) or getattr(result, "price", None) or "?"


def rank_results(results, search_request):
    best_flights = list(getattr(results, "best_flights", []) or [])
    other_flights = list(getattr(results, "other_flights", []) or [])
    candidates = best_flights + other_flights
    if not candidates:
        candidates = list(getattr(results, "flights", []) or [])

    durations = [minutes for minutes in (_flight_duration_minutes(result) for result in candidates) if minutes is not None]
    if durations:
        fastest_minutes = min(durations)
        max_duration_minutes = fastest_minutes + 360
        candidates = [
            result
            for result in candidates
            if (_flight_duration_minutes(result) is None or _flight_duration_minutes(result) <= max_duration_minutes)
        ]

    preferred_airline = (search_request.get("preferred_airline") or "").lower()
    avoid_airline = (search_request.get("avoid_airline") or "").lower()
    nonstop_required = bool(search_request.get("nonstop_required"))

    def score(result):
        airline = _flight_name(result).lower()
        stops_count = _flight_stops_count(result)
        penalty = 0
        if nonstop_required and stops_count not in (None, 0):
            penalty += 100000
        elif stops_count not in (None, 0):
            penalty += stops_count * 75
        if preferred_airline and preferred_airline not in airline:
            penalty += 40
        if avoid_airline and avoid_airline in airline:
            penalty += 200
        return (_flight_price_value(result) + penalty, stops_count or 99, _flight_duration(result))

    ranked = sorted(candidates, key=score)
    deduped = []
    seen = set()
    for result in ranked:
        key = (
            _flight_name(result),
            _flight_time_summary(result),
            _flight_duration(result),
            _flight_stops_text(result),
            _flight_price_text(result),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(result)
    return deduped


def format_results(search_request, results, limit=5):
    ranked = rank_results(results, search_request)
    header = f"✈️ {search_request['origin']} → {search_request['destination']} ({search_request['depart_date']}"
    if search_request.get("return_date"):
        header += f" → {search_request['return_date']}"
    header += ")"
    lines = [header]
    if not ranked:
        lines.append("No flights found.")
        return "\n".join(lines)
    for index, result in enumerate(ranked[:limit], 1):
        lines.append(f"\n{index}. {_flight_name(result)} — {_flight_price_text(result)}")
        lines.append(f"   {_flight_time_summary(result)} · {_flight_duration(result)} · {_flight_stops_text(result)}")
    return "\n".join(lines)
