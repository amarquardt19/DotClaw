#!/usr/bin/env python3
from dataclasses import dataclass
from datetime import datetime
import os
import re
from types import SimpleNamespace

import requests

from flights_search_service import build_search_request

DUFFEL_API_URL = os.environ.get("DUFFEL_API_URL", "https://api.duffel.com")
DUFFEL_API_TOKEN_ENV_KEYS = ("DUFFEL_API_TOKEN", "DUFFEL_ACCESS_TOKEN")


@dataclass
class DuffelSegment:
    name: str
    departure: str
    arrival: str
    duration: str
    stops: int = 0


@dataclass
class DuffelOfferSummary:
    flights: list
    duration: str
    stops: int
    current_price: str
    price: str
    duffel_offer_id: str
    owner_iata_code: str
    live_mode: bool


class DuffelClientError(RuntimeError):
    pass


def _duffel_token():
    for key in DUFFEL_API_TOKEN_ENV_KEYS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    raise DuffelClientError("Set DUFFEL_API_TOKEN in .env before using Duffel search.")


def _headers():
    return {
        "Authorization": f"Bearer {_duffel_token()}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _iso_duration_to_text(value):
    text = str(value or "").strip().upper()
    if not text:
        return "?"
    match = re.fullmatch(r"P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?", text)
    if not match:
        return text
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    if hours and minutes:
        return f"{hours} hr {minutes} min"
    if hours:
        return f"{hours} hr"
    return f"{minutes} min"


def _parse_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _format_dt(value):
    dt = _parse_datetime(value)
    if not dt:
        return str(value or "?")
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{hour}:{dt.strftime('%M %p')} on {dt.strftime('%a, %b')} {dt.day}"


def _segment_name(segment):
    for key in ("marketing_carrier", "operating_carrier", "carrier"):
        carrier = segment.get(key) or {}
        name = carrier.get("name")
        if name:
            return name
    return "Unknown airline"


def _segment_duration(segment):
    duration = segment.get("duration")
    if duration:
        return _iso_duration_to_text(duration)
    depart = _parse_datetime(segment.get("departing_at"))
    arrive = _parse_datetime(segment.get("arriving_at"))
    if depart and arrive and arrive >= depart:
        minutes = int((arrive - depart).total_seconds() // 60)
        hours, remainder = divmod(minutes, 60)
        if hours and remainder:
            return f"{hours} hr {remainder} min"
        if hours:
            return f"{hours} hr"
        return f"{remainder} min"
    return "?"


def _build_segment(segment):
    return DuffelSegment(
        name=_segment_name(segment),
        departure=_format_dt(segment.get("departing_at")),
        arrival=_format_dt(segment.get("arriving_at")),
        duration=_segment_duration(segment),
        stops=0,
    )


def _offer_segments(offer):
    slices = offer.get("slices") or []
    built = []
    for slice_item in slices:
        for segment in slice_item.get("segments") or []:
            built.append(_build_segment(segment))
    return built


def _offer_stops_count(offer):
    slices = offer.get("slices") or []
    if not slices:
        return 0
    return max(max(len(slice_item.get("segments") or []) - 1, 0) for slice_item in slices)


def _build_offer_summary(offer):
    owner = offer.get("owner") or {}
    segments = _offer_segments(offer)
    amount = offer.get("total_amount") or offer.get("base_amount") or "?"
    currency = offer.get("total_currency") or offer.get("base_currency") or ""
    price_text = f"{currency} {amount}".strip() if currency else str(amount)
    return DuffelOfferSummary(
        flights=segments,
        duration=_iso_duration_to_text(offer.get("total_duration")),
        stops=_offer_stops_count(offer),
        current_price=price_text,
        price=price_text,
        duffel_offer_id=offer.get("id") or "",
        owner_iata_code=owner.get("iata_code") or "",
        live_mode=bool(offer.get("live_mode")),
    )


def _post_offer_request(search_request):
    slices = [
        {
            "origin": search_request["origin"],
            "destination": search_request["destination"],
            "departure_date": search_request["depart_date"],
        }
    ]
    if search_request.get("return_date"):
        slices.append(
            {
                "origin": search_request["destination"],
                "destination": search_request["origin"],
                "departure_date": search_request["return_date"],
            }
        )
    seat_preference = (search_request.get("seat_preference") or "").lower()
    cabin_class = "economy"
    if "premium" in seat_preference:
        cabin_class = "premium_economy"
    elif "business" in seat_preference:
        cabin_class = "business"
    elif "first" in seat_preference:
        cabin_class = "first"
    payload = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"}],
            "cabin_class": cabin_class,
            "max_connections": 0 if search_request.get("nonstop_required") else 1,
        }
    }
    response = requests.post(
        f"{DUFFEL_API_URL}/air/offer_requests",
        headers=_headers(),
        json=payload,
        timeout=45,
    )
    if not response.ok:
        raise DuffelClientError(f"Duffel offer request failed: {response.status_code} {response.text[:300]}")
    body = response.json()
    data = body.get("data") or {}
    offer_request_id = data.get("id")
    if not offer_request_id:
        raise DuffelClientError("Duffel offer request did not return an id.")
    return offer_request_id


def _list_offers(offer_request_id, search_request):
    params = {
        "offer_request_id": offer_request_id,
        "limit": 50,
        "sort": "total_duration",
    }
    if search_request.get("nonstop_required"):
        params["max_connections"] = 0
    response = requests.get(
        f"{DUFFEL_API_URL}/air/offers",
        headers=_headers(),
        params=params,
        timeout=45,
    )
    if not response.ok:
        raise DuffelClientError(f"Duffel offers lookup failed: {response.status_code} {response.text[:300]}")
    body = response.json()
    return list(body.get("data") or [])


def fetch_flights(search_request):
    offer_request_id = _post_offer_request(search_request)
    offers = _list_offers(offer_request_id, search_request)
    summaries = [_build_offer_summary(offer) for offer in offers]
    live_mode = any(getattr(summary, "live_mode", False) for summary in summaries)
    return SimpleNamespace(flights=summaries, best_flights=[], other_flights=[], offer_request_id=offer_request_id, live_mode=live_mode)


__all__ = ["build_search_request", "fetch_flights", "DuffelClientError"]
