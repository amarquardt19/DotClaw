#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

US_AIRPORTS = {
    "ACK", "ASE", "ATL", "BNA", "BOI", "BOS", "BZN", "DCA", "DEN", "DFW", "EWR", "HNL", "IAD", "JAC",
    "JFK", "LAX", "MIA", "OAK", "OGG", "ORD", "PDX", "PHX", "RIC", "SAN", "SEA", "SFO", "SJC", "SLC",
    "SUN",
}
CANADA_AIRPORTS = {"YQB", "YYZ", "YUL", "YVR", "YYC", "YOW", "YHZ"}
EUROPE_AIRPORTS = {"BCN", "CDG", "FCO", "MAD", "LHR", "AMS", "DUB", "LIS", "MXP"}
LATAM_AIRPORTS = {"BOG", "EZE", "GIG", "GRU", "LIM", "MEX", "SCL"}
CARIBBEAN_AIRPORTS = {"AUA", "CUN", "MBJ", "NAS", "PUJ", "SJU", "STT", "STX"}
STAR_ALLIANCE_TOKENS = ["united", "air canada", "lufthansa", "swiss", "austrian", "brussels", "tap", "sas", "copa"]
ONEWORLD_TOKENS = ["american", "alaska", "british airways", "iberia", "finnair", "japan airlines", "qantas", "cathay"]
SKYTEAM_TOKENS = ["delta", "air france", "klm", "virgin atlantic", "aeromexico", "ita"]
US_CARRIER_TOKENS = ["united", "american", "delta", "alaska", "hawaiian", "jetblue", "southwest"]

from flights_search_service import (
    FlightSearchError,
    _flight_duration,
    _flight_duration_minutes,
    _flight_name,
    _flight_price_text,
    _flight_stops_count,
    _flight_stops_text,
    _flight_time_summary,
    build_search_request,
    fetch_flights,
    rank_results,
)

TARGET_DEPARTURE_TEXT = "4:25 PM"
TARGET_MINUTES = 16 * 60 + 25
DEFAULT_TOLERANCE_MINUTES = 10
DEFAULT_OUTPUT = Path("/Users/adammarq/Downloads/Flights Claw Final Project.xlsx")
DEFAULT_SHEET = "sfo_ase_panel"


def make_flight_id(origin: str, destination: str, departure_date: date, airline: str, departure_time: str, arrival_time: str, duration_text: str, stops_text: str) -> str:
    key = "|".join(
        [
            origin,
            destination,
            departure_date.isoformat(),
            str(airline or "").strip().lower(),
            str(departure_time or "").strip().lower(),
            str(arrival_time or "").strip().lower(),
            str(duration_text or "").strip().lower(),
            str(stops_text or "").strip().lower(),
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def make_snapshot_id(query_ts: datetime, flight_id: str, price_text: str | None) -> str:
    key = "|".join([query_ts.isoformat(timespec="seconds"), flight_id, str(price_text or "")])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="SFO")
    parser.add_argument("--destination", default="ASE")
    parser.add_argument("--start-date", default=date.today().isoformat())
    parser.add_argument("--end-date", default="2026-09-15")
    parser.add_argument("--target-time", default=TARGET_DEPARTURE_TEXT)
    parser.add_argument("--tolerance-minutes", type=int, default=DEFAULT_TOLERANCE_MINUTES)
    parser.add_argument("--target-only", action="store_true")
    parser.add_argument("--target-nonstop-only", action="store_true")
    parser.add_argument("--target-airline", default="")
    parser.add_argument("--service-label", default="")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--sheet-name", default=DEFAULT_SHEET)
    return parser.parse_args()


def daterange(start_date: date, end_date: date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def parse_clock_minutes(value: str | None) -> int | None:
    if not value:
        return None
    text = str(value).strip().upper().replace("  ", " ")
    match = re.search(r"(\d{1,2}:\d{2})\s*([AP]M)", text)
    if not match:
        return None
    parsed = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%I:%M %p")
    return parsed.hour * 60 + parsed.minute


def extract_departure_time(result) -> str:
    summary = _flight_time_summary(result)
    return summary.split("→", 1)[0].strip() if "→" in summary else summary.strip()


def extract_arrival_time(result) -> str:
    summary = _flight_time_summary(result)
    return summary.split("→", 1)[1].strip() if "→" in summary else ""


def is_united(result) -> bool:
    return "united" in (_flight_name(result) or "").lower()


def airline_matches(result, target_airline: str) -> bool:
    if not target_airline:
        return True
    return target_airline.strip().lower() in (_flight_name(result) or "").lower()


def pick_target_flight(results, target_minutes: int, tolerance_minutes: int):
    candidates = []
    for result in results:
        if not is_united(result):
            continue
        departure_time = extract_departure_time(result)
        departure_minutes = parse_clock_minutes(departure_time)
        if departure_minutes is None:
            continue
        delta = abs(departure_minutes - target_minutes)
        if delta <= tolerance_minutes:
            candidates.append((delta, _flight_stops_count(result) or 99, _flight_duration_minutes(result) or 10**9, result))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def price_to_number(value: str | None) -> float | None:
    if value is None:
        return None
    digits = re.sub(r"[^\d.]", "", str(value))
    return float(digits) if digits else None


def is_holiday_window(departure_date: date) -> bool:
    return departure_date.month == 7 and 1 <= departure_date.day <= 7


def holiday_season_label(departure_date: date) -> str:
    if departure_date.month == 7 and 1 <= departure_date.day <= 7:
        return "fourth_of_july"
    if departure_date.month == 9 and 1 <= departure_date.day <= 7:
        return "labor_day"
    return "none"


def is_fourth_of_july_window(departure_date: date) -> bool:
    return departure_date.month == 7 and 1 <= departure_date.day <= 7


def is_labor_day_window(departure_date: date) -> bool:
    return departure_date.month == 9 and 1 <= departure_date.day <= 7


def dep_time_bucket(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    if minutes < 360:
        return "red_eye"
    if minutes < 720:
        return "morning"
    if minutes < 1020:
        return "afternoon"
    return "evening"


def lead_time_bucket(days_until_departure: int) -> str:
    if days_until_departure <= 3:
        return "last_minute"
    if days_until_departure <= 14:
        return "near_term"
    if days_until_departure <= 35:
        return "mid_curve"
    if days_until_departure <= 70:
        return "advance"
    return "far_out"


def fare_bucket_proxy(days_until_departure: int, price_value: float | None, price_quantile: float | None, holiday_flag: bool) -> str:
    if price_value is None:
        return "Unknown"
    if days_until_departure <= 2 or (price_quantile is not None and price_quantile >= 0.92):
        return "Y"
    if days_until_departure <= 7 or (holiday_flag and price_quantile is not None and price_quantile >= 0.8):
        return "B"
    if days_until_departure <= 21 or (price_quantile is not None and price_quantile >= 0.65):
        return "M"
    if days_until_departure <= 45 or (price_quantile is not None and price_quantile >= 0.45):
        return "H"
    if days_until_departure <= 75 or (price_quantile is not None and price_quantile >= 0.25):
        return "Q"
    return "V"


def build_search(origin: str, destination: str, departure_date: str) -> dict:
    return build_search_request(
        {
            "origin": origin,
            "destination": destination,
            "depart_date": departure_date,
            "return_date": None,
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
    )


def build_competition_context(results) -> list[dict]:
    context = []
    for result in results:
        departure_time = extract_departure_time(result)
        departure_minutes = parse_clock_minutes(departure_time)
        price_text = _flight_price_text(result)
        price_value = price_to_number(price_text)
        stops_count = _flight_stops_count(result)
        context.append(
            {
                "result": result,
                "departure_time": departure_time,
                "departure_minutes": departure_minutes,
                "time_of_day_bucket": dep_time_bucket(departure_minutes),
                "price_text": price_text,
                "price_value": price_value,
                "stops_count": stops_count,
                "nonstop_flag": stops_count == 0,
                "airline": _flight_name(result),
            }
        )
    return context


def airport_country_code(airport_code: str | None) -> str | None:
    code = (airport_code or "").strip().upper()
    if code in US_AIRPORTS:
        return "US"
    if code in CANADA_AIRPORTS:
        return "CA"
    if code in EUROPE_AIRPORTS:
        return "EU"
    if code in LATAM_AIRPORTS:
        return "LATAM"
    if code in CARIBBEAN_AIRPORTS:
        return "CARIBBEAN"
    return None


def is_international_route(origin: str | None, destination: str | None) -> bool:
    origin_country = airport_country_code(origin)
    destination_country = airport_country_code(destination)
    if not origin_country or not destination_country:
        return False
    return origin_country != destination_country


def route_region_flags(origin: str | None, destination: str | None) -> dict[str, bool]:
    destination_country = airport_country_code(destination)
    return {
        "is_international_route": is_international_route(origin, destination),
        "is_europe_route": destination_country == "EU",
        "is_latam_route": destination_country == "LATAM",
        "is_canada_route": destination_country == "CA",
        "is_caribbean_route": destination_country == "CARIBBEAN",
    }


def is_budget_airline(airline_name: str | None) -> bool:
    normalized = (airline_name or "").strip().lower()
    if not normalized:
        return False
    if "southwest" in normalized:
        return False
    budget_tokens = [
        "spirit",
        "frontier",
        "allegiant",
        "sun country",
        "breeze",
        "avelo",
        "jetblue",
    ]
    return any(token in normalized for token in budget_tokens)


def is_competing_time_window(item: dict, target_bucket: str, target_departure_minutes: int | None) -> bool:
    item_minutes = item.get("departure_minutes")
    same_bucket = item.get("time_of_day_bucket") == target_bucket
    within_four_hours = False
    if item_minutes is not None and target_departure_minutes is not None:
        within_four_hours = abs(int(item_minutes) - int(target_departure_minutes)) <= 240
    return same_bucket or within_four_hours


def competition_dedupe_key(item: dict) -> tuple:
    return (
        (item.get("airline") or "").strip().lower(),
        item.get("departure_time"),
        item.get("departure_minutes"),
        item.get("stops_count"),
    )


def alliance_for_airline(airline_name: str | None) -> str | None:
    normalized = (airline_name or "").strip().lower()
    if not normalized:
        return None
    if any(token in normalized for token in STAR_ALLIANCE_TOKENS):
        return "star_alliance"
    if any(token in normalized for token in ONEWORLD_TOKENS):
        return "oneworld"
    if any(token in normalized for token in SKYTEAM_TOKENS):
        return "skyteam"
    return None


def is_us_airline(airline_name: str | None) -> bool:
    normalized = (airline_name or "").strip().lower()
    if not normalized:
        return False
    return any(token in normalized for token in US_CARRIER_TOKENS)


def filtered_competitors_for(result, context_rows: list[dict], target_bucket: str, target_departure_minutes: int | None) -> list[dict]:
    competitors = []
    seen = set()
    for item in context_rows:
        if item["result"] is result:
            continue
        if not item.get("nonstop_flag"):
            continue
        if is_budget_airline(item.get("airline")):
            continue
        if not is_competing_time_window(item, target_bucket, target_departure_minutes):
            continue
        dedupe_key = competition_dedupe_key(item)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        competitors.append(item)
    return competitors


def competitor_label(item: dict) -> str:
    airline = (item.get("airline") or "").strip()
    departure_time = (item.get("departure_time") or "").strip()
    arrival_time = ""
    result = item.get("result")
    if result is not None:
        arrival_time = extract_arrival_time(result)
    if departure_time and arrival_time:
        return f"{airline} {departure_time} -> {arrival_time}"
    if departure_time:
        return f"{airline} {departure_time}"
    return airline


def summarize_competitor_group(competitors: list[dict], price_value: float | None) -> dict[str, object]:
    labels = [competitor_label(item) for item in competitors if competitor_label(item)]
    priced_competitors = [item for item in competitors if item.get("price_value") is not None]
    cheapest_price = min((item["price_value"] for item in priced_competitors), default=None)
    return {
        "count": len(competitors),
        "labels": " | ".join(labels),
        "has_any": len(competitors) > 0,
        "cheaper_flag": None if price_value is None or cheapest_price is None else price_value < cheapest_price,
        "price_diff_vs_cheapest": None if price_value is None or cheapest_price is None else price_value - cheapest_price,
    }


def competition_metrics_for(result, context_rows: list[dict], target_bucket: str, target_departure_minutes: int | None, price_value: float | None) -> tuple[int, bool | None, float | None]:
    competitors = filtered_competitors_for(result, context_rows, target_bucket, target_departure_minutes)
    competitor_count = len(competitors)
    priced_competitors = [item for item in competitors if item.get("price_value") is not None]
    if price_value is None or not priced_competitors:
        return competitor_count, None, None
    cheapest_competitor = min(item["price_value"] for item in priced_competitors)
    return competitor_count, price_value < cheapest_competitor, price_value - cheapest_competitor


def other_airline_competition_metrics_for(result, context_rows: list[dict], target_bucket: str, target_departure_minutes: int | None, target_airline: str, price_value: float | None) -> tuple[int, bool | None, float | None]:
    normalized_target_airline = (target_airline or "").strip().lower()
    competitors = []
    for item in filtered_competitors_for(result, context_rows, target_bucket, target_departure_minutes):
        competitor_airline = (item.get("airline") or "").strip().lower()
        if normalized_target_airline and normalized_target_airline in competitor_airline:
            continue
        competitors.append(item)
    competitor_count = len(competitors)
    priced_competitors = [item for item in competitors if item.get("price_value") is not None]
    if price_value is None or not priced_competitors:
        return competitor_count, None, None
    cheapest_competitor = min(item["price_value"] for item in priced_competitors)
    return competitor_count, price_value < cheapest_competitor, price_value - cheapest_competitor


def collect_rows(origin: str, destination: str, start_date: date, end_date: date, target_minutes: int, tolerance_minutes: int, target_only: bool = False, target_airline: str = "", service_label: str = "", target_nonstop_only: bool = False) -> list[dict]:
    rows = []
    query_ts = datetime.now()
    query_date = query_ts.date()
    normalized_airline = target_airline.strip()
    route_flags = route_region_flags(origin, destination)
    for departure_date in daterange(start_date, end_date):
        search_request = build_search(origin, destination, departure_date.isoformat())
        try:
            results = fetch_flights(search_request)
        except FlightSearchError as exc:
            rows.append(
                {
                    "query_timestamp": query_ts.isoformat(timespec="seconds"),
                    "query_date": query_date.isoformat(),
                    "origin": origin,
                    "destination": destination,
                    "route": f"{origin}-{destination}",
                    "departure_date": departure_date.isoformat(),
                    "days_until_departure": (departure_date - query_date).days,
                    "target_departure_time": TARGET_DEPARTURE_TEXT,
                    "target_airline": normalized_airline,
                    "route_service_label": service_label,
                    "matched_target_flight": False,
                    "is_target_time_flight": False,
                    "collection_status": "search_error",
                    "error_message": str(exc),
                    "market_type": "leisure",
                    **route_flags,
                }
            )
            continue
        ranked = rank_results(results, search_request)
        days_until_departure = (departure_date - query_date).days
        holiday_flag = is_holiday_window(departure_date)
        holiday_label = holiday_season_label(departure_date)
        fourth_of_july_flag = is_fourth_of_july_window(departure_date)
        labor_day_flag = is_labor_day_window(departure_date)
        context_rows = build_competition_context(ranked)
        for result in ranked:
            departure_time = extract_departure_time(result)
            arrival_time = extract_arrival_time(result)
            departure_minutes = parse_clock_minutes(departure_time)
            price_text = _flight_price_text(result)
            price_value = price_to_number(price_text)
            duration_text = _flight_duration(result)
            stops_text = _flight_stops_text(result)
            airline = _flight_name(result)
            flight_id = make_flight_id(
                origin,
                destination,
                departure_date,
                airline,
                departure_time,
                arrival_time,
                duration_text,
                stops_text,
            )
            is_target_time = False
            if departure_minutes is not None:
                is_target_time = abs(departure_minutes - target_minutes) <= tolerance_minutes and airline_matches(result, normalized_airline)
                if is_target_time and target_nonstop_only and (_flight_stops_count(result) != 0):
                    is_target_time = False
            if target_only and not is_target_time:
                continue
            time_bucket = dep_time_bucket(departure_minutes)
            competitor_count, cheaper_than_competitors, price_diff_vs_cheapest_competitor = competition_metrics_for(
                result,
                context_rows,
                time_bucket,
                departure_minutes,
                price_value,
            )
            other_airline_competitor_count, cheaper_than_other_airline_competitors, price_diff_vs_cheapest_other_airline_competitor = other_airline_competition_metrics_for(
                result,
                context_rows,
                time_bucket,
                departure_minutes,
                normalized_airline,
                price_value,
            )
            competitor_rows = filtered_competitors_for(result, context_rows, time_bucket, departure_minutes)
            same_airline_alternate_rows = []
            different_airline_alternate_rows = []
            normalized_result_airline = (airline or "").strip().lower()
            for competitor in competitor_rows:
                competitor_airline_name = (competitor.get("airline") or "").strip().lower()
                if normalized_result_airline and competitor_airline_name == normalized_result_airline:
                    same_airline_alternate_rows.append(competitor)
                else:
                    different_airline_alternate_rows.append(competitor)
            any_airline_summary = summarize_competitor_group(competitor_rows, price_value)
            same_airline_summary = summarize_competitor_group(same_airline_alternate_rows, price_value)
            different_airline_summary = summarize_competitor_group(different_airline_alternate_rows, price_value)
            target_alliance = alliance_for_airline(airline)
            foreign_nonstop_competitor_count = 0
            us_nonstop_competitor_count = 0
            same_alliance_nonstop_competitor_flag = False
            for competitor in competitor_rows:
                competitor_airline = competitor.get("airline")
                if is_us_airline(competitor_airline):
                    us_nonstop_competitor_count += 1
                else:
                    foreign_nonstop_competitor_count += 1
                competitor_alliance = alliance_for_airline(competitor_airline)
                if target_alliance and competitor_alliance == target_alliance:
                    same_alliance_nonstop_competitor_flag = True
            rows.append(
                {
                    "snapshot_id": make_snapshot_id(query_ts, flight_id, price_text),
                    "flight_id": flight_id,
                    "query_timestamp": query_ts.isoformat(timespec="seconds"),
                    "query_date": query_date.isoformat(),
                    "origin": origin,
                    "destination": destination,
                    "route": f"{origin}-{destination}",
                    "departure_date": departure_date.isoformat(),
                    "days_until_departure": days_until_departure,
                    "departure_dow": departure_date.strftime("%A"),
                    "query_dow": query_date.strftime("%A"),
                    "is_weekend_departure": departure_date.weekday() >= 4,
                    "month_departure": departure_date.month,
                    "holiday_window_flag": holiday_flag,
                    "holiday_season": holiday_label,
                    "fourth_of_july_flag": fourth_of_july_flag,
                    "labor_day_flag": labor_day_flag,
                    "market_type": "leisure",
                    "target_departure_time": TARGET_DEPARTURE_TEXT,
                    "target_airline": normalized_airline,
                    "route_service_label": service_label,
                    "matched_target_flight": True,
                    "is_target_time_flight": is_target_time,
                    "collection_status": "matched",
                    "airline": airline,
                    "departure_time": departure_time,
                    "arrival_time": arrival_time,
                    "departure_minutes": departure_minutes,
                    "time_of_day_bucket": time_bucket,
                    "duration_text": duration_text,
                    "duration_minutes": _flight_duration_minutes(result),
                    "stops_count": _flight_stops_count(result),
                    "stops_text": stops_text,
                    "nonstop_flag": (_flight_stops_count(result) == 0),
                    "competing_nonstop_same_time_bucket_count": competitor_count,
                    "cheaper_than_competing_nonstops": cheaper_than_competitors,
                    "price_diff_vs_cheapest_competing_nonstop": price_diff_vs_cheapest_competitor,
                    "alternate_nonstop_any_airline_count": any_airline_summary["count"],
                    "alternate_nonstop_any_airline_flag": any_airline_summary["has_any"],
                    "alternate_nonstop_any_airline_labels": any_airline_summary["labels"],
                    "cheaper_than_any_airline_alternate_nonstop": any_airline_summary["cheaper_flag"],
                    "price_diff_vs_cheapest_any_airline_alternate_nonstop": any_airline_summary["price_diff_vs_cheapest"],
                    "competing_nonstop_other_airlines_same_time_bucket_count": other_airline_competitor_count,
                    "cheaper_than_other_airline_competing_nonstops": cheaper_than_other_airline_competitors,
                    "price_diff_vs_cheapest_other_airline_competing_nonstop": price_diff_vs_cheapest_other_airline_competitor,
                    "alternate_nonstop_same_airline_count": same_airline_summary["count"],
                    "alternate_nonstop_same_airline_flag": same_airline_summary["has_any"],
                    "alternate_nonstop_same_airline_labels": same_airline_summary["labels"],
                    "cheaper_than_same_airline_alternate_nonstop": same_airline_summary["cheaper_flag"],
                    "price_diff_vs_cheapest_same_airline_alternate_nonstop": same_airline_summary["price_diff_vs_cheapest"],
                    "alternate_nonstop_different_airline_count": different_airline_summary["count"],
                    "alternate_nonstop_different_airline_flag": different_airline_summary["has_any"],
                    "alternate_nonstop_different_airline_labels": different_airline_summary["labels"],
                    "cheaper_than_different_airline_alternate_nonstop": different_airline_summary["cheaper_flag"],
                    "price_diff_vs_cheapest_different_airline_alternate_nonstop": different_airline_summary["price_diff_vs_cheapest"],
                    "price_text": price_text,
                    "price_value": price_value,
                    "lead_time_bucket": lead_time_bucket(days_until_departure),
                    "has_foreign_nonstop_competitor": foreign_nonstop_competitor_count > 0,
                    "foreign_nonstop_competitor_count": foreign_nonstop_competitor_count,
                    "us_nonstop_competitor_count": us_nonstop_competitor_count,
                    "same_alliance_nonstop_competitor_flag": same_alliance_nonstop_competitor_flag,
                    **route_flags,
                    "error_message": None,
                }
            )
    return rows


def load_existing_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(path, sheet_name=sheet_name)
    except ValueError:
        return pd.DataFrame()


def assign_service_fare_letter_buckets(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "route_service_label" not in df.columns or "price_value" not in df.columns:
        return df
    df = df.copy()
    letters = list("MNOPQRSTUVWXYZ")
    bucket_cols = [
        "fare_letter_bucket",
        "fare_letter_bucket_min_price",
        "fare_letter_bucket_max_price",
        "fare_letter_bucket_distinct_fare_count",
    ]
    for col in bucket_cols:
        df[col] = None

    working = df[df.get("matched_target_flight", pd.Series(dtype=bool)).fillna(False).astype(bool) & df["price_value"].notna()].copy()
    if working.empty:
        return df

    for service_label, group in working.groupby("route_service_label", dropna=False):
        distinct_prices = sorted({float(v) for v in group["price_value"].dropna().tolist()})
        if not distinct_prices:
            continue
        if "service_distinct_fare_count_total" not in df.columns:
            df["service_distinct_fare_count_total"] = None
        bucket_count = min(len(distinct_prices), len(letters))
        bucket_size = max(1, -(-len(distinct_prices) // bucket_count))
        price_to_letter = {}
        summaries = {}
        for idx in range(bucket_count):
            start = idx * bucket_size
            stop = min(len(distinct_prices), (idx + 1) * bucket_size)
            chunk = distinct_prices[start:stop]
            if not chunk:
                continue
            letter = letters[idx]
            summaries[letter] = {
                "min_price": chunk[0],
                "max_price": chunk[-1],
                "distinct_count": len(chunk),
            }
            for price in chunk:
                price_to_letter[price] = letter
        service_mask = df["route_service_label"].eq(service_label) & df["price_value"].notna()
        for row_idx in df.index[service_mask]:
            price = float(df.at[row_idx, "price_value"])
            letter = price_to_letter.get(price)
            if not letter:
                continue
            df.at[row_idx, "fare_letter_bucket"] = letter
            df.at[row_idx, "fare_letter_bucket_min_price"] = summaries[letter]["min_price"]
            df.at[row_idx, "fare_letter_bucket_max_price"] = summaries[letter]["max_price"]
            df.at[row_idx, "fare_letter_bucket_distinct_fare_count"] = summaries[letter]["distinct_count"]

        service_rows = df.index[df["route_service_label"].eq(service_label)]
        df.loc[service_rows, "service_distinct_fare_count_total"] = len(distinct_prices)
        for letter in letters:
            count_col = f"service_fare_letter_{letter}_distinct_fare_count"
            min_col = f"service_fare_letter_{letter}_min_price"
            max_col = f"service_fare_letter_{letter}_max_price"
            if count_col not in df.columns:
                df[count_col] = None
            if min_col not in df.columns:
                df[min_col] = None
            if max_col not in df.columns:
                df[max_col] = None
            if letter in summaries:
                df.loc[service_rows, count_col] = summaries[letter]["distinct_count"]
                df.loc[service_rows, min_col] = summaries[letter]["min_price"]
                df.loc[service_rows, max_col] = summaries[letter]["max_price"]
            else:
                df.loc[service_rows, count_col] = 0
    return df


def estimate_booking_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    for col in [
        "estimated_booking_pressure_score",
        "estimated_booking_pressure_band",
        "estimated_bookings_daily",
    ]:
        df[col] = None

    matched_mask = df.get("matched_target_flight", pd.Series(dtype=bool)).fillna(False).astype(bool)
    if not matched_mask.any():
        return df

    working = df.loc[matched_mask].copy()
    days = pd.to_numeric(working.get("days_until_departure"), errors="coerce").fillna(0)
    duration = pd.to_numeric(working.get("duration_minutes"), errors="coerce").fillna(0)
    same_bucket_comp = pd.to_numeric(working.get("competing_nonstop_same_time_bucket_count"), errors="coerce").fillna(0)
    other_airline_comp = pd.to_numeric(working.get("competing_nonstop_other_airlines_same_time_bucket_count"), errors="coerce").fillna(0)
    price_delta = pd.to_numeric(working.get("price_difference_prev_snapshot"), errors="coerce").fillna(0)
    comp_delta = pd.to_numeric(working.get("price_diff_vs_cheapest_other_airline_competing_nonstop"), errors="coerce").fillna(0)

    lead_score = pd.Series(0.25, index=working.index, dtype=float)
    lead_score = lead_score.mask((days >= 30) & (days <= 60), 1.0)
    lead_score = lead_score.mask((days >= 14) & (days < 30), 0.8)
    lead_score = lead_score.mask((days > 60) & (days <= 90), 0.55)
    lead_score = lead_score.mask(days < 14, 0.45)
    lead_score = lead_score.mask(days > 90, 0.2)

    long_trip_bonus = (duration >= 300).astype(float) * 0.18 + (duration >= 420).astype(float) * 0.12
    holiday_bonus = working.get("fourth_of_july_flag", pd.Series(False, index=working.index)).fillna(False).astype(float) * 0.28
    holiday_bonus += working.get("labor_day_flag", pd.Series(False, index=working.index)).fillna(False).astype(float) * 0.18

    fare_signal = (price_delta.clip(-250, 250) / 250.0) * 0.35
    competition_signal = (-(same_bucket_comp.clip(0, 6)) * 0.06) + (-(other_airline_comp.clip(0, 6)) * 0.08)
    relative_price_signal = (-(comp_delta.clip(-300, 300)) / 300.0) * 0.12

    score = (lead_score + long_trip_bonus + holiday_bonus + fare_signal + competition_signal + relative_price_signal).clip(0.05, 1.6)
    weekend_bonus = working.get("is_weekend_departure", pd.Series(False, index=working.index)).fillna(False).astype(float) * 0.35
    fare_step_bonus = (price_delta > 25).astype(float) * 0.75 + (price_delta > 75).astype(float) * 0.5
    fare_drop_penalty = (price_delta < -30).astype(float) * 0.6 + (price_delta < -90).astype(float) * 0.5
    raw_bookings = (
        -0.25
        + score * 2.35
        + (duration >= 300).astype(float) * 0.25
        + weekend_bonus
        + fare_step_bonus
        - fare_drop_penalty
    )
    bookings = raw_bookings.round().clip(lower=0, upper=6)

    band = pd.Series("medium", index=working.index, dtype=object)
    band = band.mask(score < 0.45, "low")
    band = band.mask(score >= 0.95, "high")

    df.loc[working.index, "estimated_booking_pressure_score"] = score.round(3)
    df.loc[working.index, "estimated_booking_pressure_band"] = band
    df.loc[working.index, "estimated_bookings_daily"] = bookings.astype(int)

    if "route_service_label" in df.columns:
        service_labels = working["route_service_label"].dropna().unique().tolist()
        if "service_estimated_bookings_total_period" not in df.columns:
            df["service_estimated_bookings_total_period"] = None
        if "service_estimated_bookings_avg_daily" not in df.columns:
            df["service_estimated_bookings_avg_daily"] = None
        if "service_observation_days" not in df.columns:
            df["service_observation_days"] = None
        for service_label in service_labels:
            service_mask = working["route_service_label"].eq(service_label)
            service_rows = df.index[df["route_service_label"].eq(service_label)]
            service_bookings = bookings.loc[service_mask]
            total_bookings = int(service_bookings.sum()) if not service_bookings.empty else 0
            observation_days = int(service_bookings.shape[0]) if not service_bookings.empty else 0
            avg_daily = round(total_bookings / observation_days, 2) if observation_days else None
            df.loc[service_rows, "service_estimated_bookings_total_period"] = total_bookings
            df.loc[service_rows, "service_estimated_bookings_avg_daily"] = avg_daily
            df.loc[service_rows, "service_observation_days"] = observation_days
    return df


def infer_aircraft_type(duration_minutes: float | int | None, route: str | None, airline: str | None) -> str:
    duration = 0 if duration_minutes is None or pd.isna(duration_minutes) else float(duration_minutes)
    route_text = (route or "").upper()
    airline_text = (airline or "").lower()
    if "HNL" in route_text and duration >= 600:
        if "united" in airline_text:
            return "Boeing 777"
        return "Airbus A321XLR / Widebody"
    if duration >= 300:
        return "Boeing 737 / Airbus A320"
    if duration >= 180:
        return "Airbus A320 / Boeing 737-800"
    return "Embraer / Narrowbody"


def infer_aircraft_capacity(aircraft_type: str) -> int:
    aircraft = (aircraft_type or "").lower()
    if "777" in aircraft:
        return 276
    if "321" in aircraft:
        return 190
    if "737" in aircraft or "a320" in aircraft:
        return 166
    if "embraer" in aircraft:
        return 76
    return 160


def infer_starting_seats_available(capacity: int, duration_minutes: float | int | None, holiday_season: str | None) -> int:
    duration = 0 if duration_minutes is None or pd.isna(duration_minutes) else float(duration_minutes)
    availability_ratio = 0.62
    if duration >= 300:
        availability_ratio += 0.08
    if duration >= 420:
        availability_ratio += 0.04
    if holiday_season == "fourth_of_july":
        availability_ratio -= 0.12
    elif holiday_season == "labor_day":
        availability_ratio -= 0.08
    availability_ratio = min(max(availability_ratio, 0.28), 0.82)
    return int(round(capacity * availability_ratio))


def infer_target_end_load_factor(
    duration_minutes: float | int | None,
    holiday_season: str | None,
    same_bucket_competitors: float | int | None,
    other_airline_competitors: float | int | None,
) -> float:
    duration = 0 if duration_minutes is None or pd.isna(duration_minutes) else float(duration_minutes)
    same_comp = 0 if same_bucket_competitors is None or pd.isna(same_bucket_competitors) else float(same_bucket_competitors)
    other_comp = 0 if other_airline_competitors is None or pd.isna(other_airline_competitors) else float(other_airline_competitors)
    target = 0.88
    if duration >= 180:
        target += 0.02
    if duration >= 300:
        target += 0.01
    if holiday_season == "fourth_of_july":
        target += 0.03
    elif holiday_season == "labor_day":
        target += 0.02
    target -= min(same_comp, 4) * 0.006
    target -= min(other_comp, 4) * 0.01
    return round(min(max(target, 0.85), 0.95), 3)


def infer_booked_share_before_period(
    start_days_until_departure: float | int | None,
    duration_minutes: float | int | None,
    holiday_season: str | None,
) -> float:
    start_days = 0 if start_days_until_departure is None or pd.isna(start_days_until_departure) else float(start_days_until_departure)
    duration = 0 if duration_minutes is None or pd.isna(duration_minutes) else float(duration_minutes)
    if start_days >= 90:
        share = 0.14
    elif start_days >= 75:
        share = 0.20
    elif start_days >= 60:
        share = 0.28
    elif start_days >= 45:
        share = 0.40
    elif start_days >= 30:
        share = 0.54
    elif start_days >= 21:
        share = 0.66
    elif start_days >= 14:
        share = 0.76
    elif start_days >= 7:
        share = 0.84
    else:
        share = 0.90
    if duration >= 180:
        share += 0.03
    if duration >= 300:
        share += 0.02
    if holiday_season == "fourth_of_july":
        share += 0.04
    elif holiday_season == "labor_day":
        share += 0.02
    return round(min(max(share, 0.10), 0.92), 3)


def apply_capacity_allocation_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    base_cols = [
        "inferred_aircraft_type",
        "estimated_aircraft_capacity",
        "estimated_starting_seats_available",
        "estimated_bookings_this_period",
        "estimated_booked_through_period",
        "estimated_remaining_capacity_end_of_period",
        "service_target_end_load_factor",
        "service_estimated_booked_before_period",
        "service_estimated_booked_during_observation_period",
        "service_amount_booked_in_period",
        "service_estimated_booked_total_period",
        "service_estimated_remaining_sellable_capacity_end_period",
        "service_estimated_unsold_physical_capacity_end_period",
        "service_estimated_load_factor_end_period",
    ]
    for col in base_cols:
        if col not in df.columns:
            df[col] = None

    letters = list("MNOPQRSTUVWXYZ")
    for letter in letters:
        booked_col = f"service_fare_letter_{letter}_estimated_booked_period"
        remaining_col = f"service_fare_letter_{letter}_estimated_remaining_end_period"
        if booked_col not in df.columns:
            df[booked_col] = None
        if remaining_col not in df.columns:
            df[remaining_col] = None

    matched_mask = df.get("matched_target_flight", pd.Series(dtype=bool)).fillna(False).astype(bool)
    working = df.loc[matched_mask].copy()
    if working.empty or "route_service_label" not in working.columns:
        return df

    working["duration_minutes"] = pd.to_numeric(working.get("duration_minutes"), errors="coerce")
    working["estimated_bookings_daily"] = pd.to_numeric(working.get("estimated_bookings_daily"), errors="coerce").fillna(0)
    working["days_until_departure"] = pd.to_numeric(working.get("days_until_departure"), errors="coerce")

    for service_label, group in working.groupby("route_service_label", dropna=False):
        if pd.isna(service_label):
            continue
        service_rows = df.index[df["route_service_label"].eq(service_label)]
        group = group.sort_values(["days_until_departure", "departure_date", "query_timestamp"], ascending=[False, True, True])
        sample = group.iloc[0]
        aircraft = infer_aircraft_type(sample.get("duration_minutes"), sample.get("route"), sample.get("airline"))
        capacity = infer_aircraft_capacity(aircraft)
        start_days_until_departure = pd.to_numeric(group["days_until_departure"], errors="coerce").max()
        target_end_load_factor = infer_target_end_load_factor(
            sample.get("duration_minutes"),
            sample.get("holiday_season"),
            sample.get("competing_nonstop_same_time_bucket_count"),
            sample.get("competing_nonstop_other_airlines_same_time_bucket_count"),
        )
        target_end_booked = int(round(capacity * target_end_load_factor))
        booked_share_before_period = infer_booked_share_before_period(
            start_days_until_departure,
            sample.get("duration_minutes"),
            sample.get("holiday_season"),
        )
        booked_before_period = min(target_end_booked, int(round(target_end_booked * booked_share_before_period)))
        starting_seats = max(0, capacity - booked_before_period)
        remaining_target_to_sell = max(0, target_end_booked - booked_before_period)
        observed_booked_raw = int(group["estimated_bookings_daily"].sum()) if not group.empty else 0
        booked_during_period = min(observed_booked_raw, remaining_target_to_sell)

        raw_daily = pd.to_numeric(group["estimated_bookings_daily"], errors="coerce").fillna(0)
        raw_daily = raw_daily.clip(lower=0)
        raw_total_daily = int(raw_daily.sum())
        if raw_total_daily > 0 and booked_during_period >= 0:
            scale = booked_during_period / raw_total_daily
            adjusted_daily = (raw_daily * scale).round().astype(int)
            diff = booked_during_period - int(adjusted_daily.sum())
            if diff != 0:
                order = raw_daily.sort_values(ascending=False).index.tolist()
                idx = 0
                while diff != 0 and order:
                    row_idx = order[idx % len(order)]
                    current = int(adjusted_daily.loc[row_idx])
                    if diff > 0:
                        adjusted_daily.loc[row_idx] = current + 1
                        diff -= 1
                    elif current > 0:
                        adjusted_daily.loc[row_idx] = current - 1
                        diff += 1
                    idx += 1
        else:
            adjusted_daily = pd.Series(0, index=group.index, dtype=int)

        cumulative = adjusted_daily.cumsum().clip(upper=booked_during_period)
        booked_this_period = cumulative.diff().fillna(cumulative).astype(int)
        remaining = (starting_seats - cumulative).clip(lower=0)
        booked_during_period = int(cumulative.iloc[-1]) if not cumulative.empty else 0
        booked_total_period = min(capacity, booked_before_period + booked_during_period, target_end_booked)
        remaining_sellable_end = max(0, starting_seats - booked_during_period)
        unsold_physical_end = max(0, capacity - booked_total_period)
        load_factor_end = round(booked_total_period / capacity, 3) if capacity else None
        df.loc[group.index, "inferred_aircraft_type"] = aircraft
        df.loc[group.index, "estimated_aircraft_capacity"] = capacity
        df.loc[group.index, "estimated_starting_seats_available"] = starting_seats
        df.loc[group.index, "estimated_bookings_this_period"] = booked_this_period.astype(int)
        df.loc[group.index, "estimated_booked_through_period"] = cumulative.astype(int)
        df.loc[group.index, "estimated_remaining_capacity_end_of_period"] = remaining.astype(int)
        df.loc[service_rows, "inferred_aircraft_type"] = aircraft
        df.loc[service_rows, "estimated_aircraft_capacity"] = capacity
        df.loc[service_rows, "estimated_starting_seats_available"] = starting_seats
        df.loc[service_rows, "service_target_end_load_factor"] = target_end_load_factor
        df.loc[service_rows, "service_estimated_booked_before_period"] = booked_before_period
        df.loc[service_rows, "service_estimated_booked_during_observation_period"] = booked_during_period
        df.loc[service_rows, "service_estimated_booked_total_period"] = booked_total_period
        df.loc[service_rows, "service_estimated_remaining_sellable_capacity_end_period"] = remaining_sellable_end
        df.loc[service_rows, "service_estimated_unsold_physical_capacity_end_period"] = unsold_physical_end
        df.loc[service_rows, "service_estimated_load_factor_end_period"] = load_factor_end

        alloc_group = group.copy()
        alloc_group["estimated_bookings_this_period"] = booked_this_period.astype(int)
        raw_booked_by_letter = alloc_group.groupby("fare_letter_bucket", dropna=True)["estimated_bookings_this_period"].sum().to_dict()
        raw_total_booked = int(sum(raw_booked_by_letter.values()))
        if raw_total_booked > 0 and booked_during_period >= 0:
            scale = booked_during_period / raw_total_booked
            booked_by_letter = {letter: int(round(val * scale)) for letter, val in raw_booked_by_letter.items()}
            diff = booked_during_period - int(sum(booked_by_letter.values()))
            if diff != 0:
                ordered_letters = sorted(raw_booked_by_letter.items(), key=lambda item: item[1], reverse=True)
                idx = 0
                while diff != 0 and ordered_letters:
                    letter = ordered_letters[idx % len(ordered_letters)][0]
                    current = booked_by_letter.get(letter, 0)
                    if diff > 0:
                        booked_by_letter[letter] = current + 1
                        diff -= 1
                    elif current > 0:
                        booked_by_letter[letter] = current - 1
                        diff += 1
                    idx += 1
        else:
            booked_by_letter = {letter: 0 for letter in raw_booked_by_letter}
        total_booked_allocated_period = 0
        for letter in letters:
            booked_val = int(booked_by_letter.get(letter, 0))
            total_booked_allocated_period += booked_val
            booked_col = f"service_fare_letter_{letter}_estimated_booked_period"
            remaining_col = f"service_fare_letter_{letter}_estimated_remaining_end_period"
            distinct_count_col = f"service_fare_letter_{letter}_distinct_fare_count"
            remaining_val = 0
            if distinct_count_col in df.columns:
                distinct_val = pd.to_numeric(df.loc[service_rows, distinct_count_col], errors="coerce").dropna()
                if not distinct_val.empty:
                    notional_letter_capacity = max(0, int(round(starting_seats * 0.07 * max(1, float(distinct_val.iloc[0])))))
                    remaining_val = max(0, notional_letter_capacity - booked_val)
            df.loc[service_rows, booked_col] = booked_val
            df.loc[service_rows, remaining_col] = remaining_val
        df.loc[service_rows, "service_amount_booked_in_period"] = total_booked_allocated_period
    return df


def apply_derived_fields(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["query_timestamp"] = pd.to_datetime(df["query_timestamp"], errors="coerce")
    df["query_date"] = pd.to_datetime(df["query_date"], errors="coerce").dt.date
    df["departure_date"] = pd.to_datetime(df["departure_date"], errors="coerce").dt.date
    df["price_value"] = pd.to_numeric(df.get("price_value"), errors="coerce")
    matched_mask = df["matched_target_flight"].fillna(False).astype(bool) & df["price_value"].notna()
    matched = df.loc[matched_mask].copy()
    if matched.empty:
        df["price_difference_prev_snapshot"] = None
        df["price_difference_prev_departure"] = None
        df["price_difference_from_min_for_departure"] = None
        df["price_quantile_overall"] = None
        df["fare_bucket_proxy"] = None
        df["fare_letter_bucket"] = None
        df["fare_letter_bucket_min_price"] = None
        df["fare_letter_bucket_max_price"] = None
        df["fare_letter_bucket_distinct_fare_count"] = None
        df["estimated_booking_pressure_score"] = None
        df["estimated_booking_pressure_band"] = None
        df["estimated_bookings_daily"] = None
        df["period_60_to_30_days_out_flag"] = False
        df["period_30_to_14_days_out_flag"] = False
        df["period_14_to_7_days_out_flag"] = False
        df["period_7_to_3_days_out_flag"] = False
        df["period_3_to_0_days_out_flag"] = False
        return df
    group_key = "flight_id" if "flight_id" in matched.columns else "departure_date"
    matched = matched.sort_values([group_key, "query_timestamp"])
    matched["price_difference_prev_snapshot"] = matched.groupby(group_key)["price_value"].diff()
    matched = matched.sort_values(["query_timestamp", "departure_date", "departure_minutes"])
    matched["price_difference_prev_departure"] = matched.groupby("query_date")["price_value"].diff()
    matched["price_difference_from_min_for_departure"] = matched["price_value"] - matched.groupby("departure_date")["price_value"].transform("min")
    matched["price_quantile_overall"] = matched["price_value"].rank(pct=True, method="average")
    matched["fare_bucket_proxy"] = matched.apply(
        lambda row: fare_bucket_proxy(
            int(row["days_until_departure"]),
            None if pd.isna(row["price_value"]) else float(row["price_value"]),
            None if pd.isna(row["price_quantile_overall"]) else float(row["price_quantile_overall"]),
            bool(row.get("holiday_window_flag", False)),
        ),
        axis=1,
    )
    derived_cols = [
        "price_difference_prev_snapshot",
        "price_difference_prev_departure",
        "price_difference_from_min_for_departure",
        "price_quantile_overall",
        "fare_bucket_proxy",
    ]
    for col in derived_cols:
        df[col] = None
    df.loc[matched.index, derived_cols] = matched[derived_cols]
    days_until_departure = pd.to_numeric(df.get("days_until_departure"), errors="coerce")
    df["period_60_to_30_days_out_flag"] = days_until_departure.le(60) & days_until_departure.gt(30)
    df["period_30_to_14_days_out_flag"] = days_until_departure.le(30) & days_until_departure.gt(14)
    df["period_14_to_7_days_out_flag"] = days_until_departure.le(14) & days_until_departure.gt(7)
    df["period_7_to_3_days_out_flag"] = days_until_departure.le(7) & days_until_departure.gt(3)
    df["period_3_to_0_days_out_flag"] = days_until_departure.le(3) & days_until_departure.ge(0)
    if "departure_date" in df.columns:
        departure_dates = pd.to_datetime(df["departure_date"], errors="coerce")
        df["fourth_of_july_flag"] = departure_dates.dt.month.eq(7) & departure_dates.dt.day.between(1, 7)
        df["labor_day_flag"] = departure_dates.dt.month.eq(9) & departure_dates.dt.day.between(1, 7)
    df = assign_service_fare_letter_buckets(df)
    df = estimate_booking_metrics(df)
    df = apply_capacity_allocation_metrics(df)
    df["query_timestamp"] = df["query_timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    df["query_date"] = df["query_date"].astype(str)
    df["departure_date"] = df["departure_date"].astype(str)
    sort_cols = [col for col in ["query_timestamp", "departure_date", "departure_minutes", "price_value"] if col in df.columns]
    ascending = [False, True, True, True][: len(sort_cols)]
    return df.sort_values(sort_cols, ascending=ascending)


def save_sheet(df: pd.DataFrame, path: Path, sheet_name: str) -> None:
    mode = "a" if path.exists() else "w"
    writer_kwargs = {"engine": "openpyxl", "mode": mode}
    if path.exists():
        writer_kwargs["if_sheet_exists"] = "replace"
    with pd.ExcelWriter(path, **writer_kwargs) as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)


def main() -> None:
    args = parse_args()
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    target_minutes = parse_clock_minutes(args.target_time)
    if target_minutes is None:
        raise ValueError(f"Could not parse target time: {args.target_time}")
    output_path = Path(args.output)
    existing = load_existing_sheet(output_path, args.sheet_name)
    new_rows = collect_rows(
        origin=args.origin.upper(),
        destination=args.destination.upper(),
        start_date=start_date,
        end_date=end_date,
        target_minutes=target_minutes,
        tolerance_minutes=args.tolerance_minutes,
        target_only=args.target_only,
        target_airline=args.target_airline,
        service_label=args.service_label,
        target_nonstop_only=args.target_nonstop_only,
    )
    new_df = pd.DataFrame(new_rows)
    if existing.empty:
        combined = new_df
    elif new_df.empty:
        combined = existing
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)
    if not combined.empty:
        dedupe_cols = [
            "snapshot_id",
            "query_timestamp",
            "flight_id",
            "route",
            "departure_date",
            "collection_status",
        ]
        present = [col for col in dedupe_cols if col in combined.columns]
        combined = combined.drop_duplicates(subset=present, keep="last")
        combined = apply_derived_fields(combined)
    save_sheet(combined, output_path, args.sheet_name)
    matched_count = 0 if combined.empty else int((combined.get("matched_target_flight", pd.Series(dtype=bool)).fillna(False)).sum())
    print(f"Wrote sheet '{args.sheet_name}' to {output_path}")
    print(f"Total rows: {len(combined)}")
    print(f"Matched rows: {matched_count}")
    if not new_df.empty:
        print(f"New rows added this run: {len(new_df)}")


if __name__ == "__main__":
    main()
