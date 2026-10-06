#!/usr/bin/env python3
"""Daily base-fare snapshot for one fixed departure date across tracked routes.

Pulls the current Google Flights fare for a single target flight per route
(matched by airline + scheduled departure time) for a FIXED departure date,
and appends one row per route per day to a SQLite database. Designed to be
run once per day from launchd/cron; rerunning on the same day replaces that
day's rows.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

import pymssql
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from flights_search_service import (
    FlightSearchError,
    _flight_duration_minutes,
    _flight_name,
    _flight_price_text,
    _flight_stops_count,
    fetch_flights,
    rank_results,
)
from scripts.collect_sfo_ase_panel import (
    airline_matches,
    alliance_for_airline,
    build_competition_context,
    build_search,
    dep_time_bucket,
    extract_arrival_time,
    extract_departure_time,
    filtered_competitors_for,
    is_us_airline,
    make_flight_id,
    parse_clock_minutes,
    price_to_number,
    route_region_flags,
    summarize_competitor_group,
)

DEFAULT_RUN_LIST = ROOT / "run_list_daily_tracking.txt"
DEFAULT_DB = ROOT / "data" / "fare_tracking.db"
DEFAULT_DEPARTURE_DATE = "2026-10-22"
DEFAULT_TOLERANCE_MINUTES = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_fare_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts TEXT NOT NULL,
    query_date TEXT NOT NULL,
    service_label TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    route TEXT NOT NULL,
    departure_date TEXT NOT NULL,
    days_until_departure INTEGER,
    target_airline TEXT,
    target_departure_time TEXT,
    matched_target_flight INTEGER NOT NULL,
    airline TEXT,
    departure_time TEXT,
    arrival_time TEXT,
    departure_minutes INTEGER,
    duration_minutes INTEGER,
    stops_count INTEGER,
    base_fare_usd REAL,
    base_fare_text TEXT,
    price_diff_vs_cheapest_competing_nonstop REAL,
    cheapest_competing_nonstop_price REAL,
    cheapest_competing_nonstop_label TEXT,
    competing_nonstop_count INTEGER,
    flight_id TEXT,
    collection_status TEXT NOT NULL,
    error_message TEXT,
    raw_match_json TEXT,
    UNIQUE(query_date, service_label, departure_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_fare_snapshots_route_date
    ON daily_fare_snapshots(route, departure_date, query_date);
"""


AZURE_COLUMNS = [
    ("snapshot_ts", "DATETIME2"),
    ("query_date", "DATE"),
    ("service_label", "NVARCHAR(120)"),
    ("origin", "NVARCHAR(3)"),
    ("destination", "NVARCHAR(3)"),
    ("route", "NVARCHAR(10)"),
    ("departure_date", "DATE"),
    ("days_until_departure", "INT"),
    ("target_airline", "NVARCHAR(50)"),
    ("target_departure_time", "NVARCHAR(40)"),
    ("matched_target_flight", "BIT"),
    ("airline", "NVARCHAR(100)"),
    ("departure_time", "NVARCHAR(40)"),
    ("arrival_time", "NVARCHAR(40)"),
    ("departure_minutes", "INT"),
    ("duration_minutes", "INT"),
    ("stops_count", "INT"),
    ("base_fare_usd", "FLOAT"),
    ("base_fare_text", "NVARCHAR(30)"),
    ("price_diff_vs_cheapest_competing_nonstop", "FLOAT"),
    ("cheapest_competing_nonstop_price", "FLOAT"),
    ("cheapest_competing_nonstop_label", "NVARCHAR(160)"),
    ("competing_nonstop_count", "INT"),
    ("alternate_nonstop_any_airline_count", "INT"),
    ("alternate_nonstop_any_airline_flag", "BIT"),
    ("alternate_nonstop_any_airline_labels", "NVARCHAR(MAX)"),
    ("cheaper_than_any_airline_alternate_nonstop", "BIT"),
    ("price_diff_vs_cheapest_any_airline_alternate_nonstop", "FLOAT"),
    ("alternate_nonstop_same_airline_count", "INT"),
    ("alternate_nonstop_same_airline_flag", "BIT"),
    ("alternate_nonstop_same_airline_labels", "NVARCHAR(MAX)"),
    ("cheaper_than_same_airline_alternate_nonstop", "BIT"),
    ("price_diff_vs_cheapest_same_airline_alternate_nonstop", "FLOAT"),
    ("alternate_nonstop_different_airline_count", "INT"),
    ("alternate_nonstop_different_airline_flag", "BIT"),
    ("alternate_nonstop_different_airline_labels", "NVARCHAR(MAX)"),
    ("cheaper_than_different_airline_alternate_nonstop", "BIT"),
    ("price_diff_vs_cheapest_different_airline_alternate_nonstop", "FLOAT"),
    ("alternate_nonstop_same_alliance_count", "INT"),
    ("alternate_nonstop_same_alliance_flag", "BIT"),
    ("alternate_nonstop_same_alliance_labels", "NVARCHAR(MAX)"),
    ("cheaper_than_same_alliance_alternate_nonstop", "BIT"),
    ("price_diff_vs_cheapest_same_alliance_alternate_nonstop", "FLOAT"),
    ("has_foreign_nonstop_competitor", "BIT"),
    ("foreign_nonstop_competitor_count", "INT"),
    ("us_nonstop_competitor_count", "INT"),
    ("same_alliance_nonstop_competitor_flag", "BIT"),
    ("is_international_route", "BIT"),
    ("is_europe_route", "BIT"),
    ("is_latam_route", "BIT"),
    ("is_canada_route", "BIT"),
    ("is_caribbean_route", "BIT"),
    ("flight_id", "NVARCHAR(20)"),
    ("collection_status", "NVARCHAR(20)"),
    ("error_message", "NVARCHAR(500)"),
    ("raw_match_json", "NVARCHAR(MAX)"),
]

NVARCHAR_LIMITS = {
    name: int(sql_type[9:-1])
    for name, sql_type in AZURE_COLUMNS
    if sql_type.startswith("NVARCHAR(") and sql_type != "NVARCHAR(MAX)"
}


def azure_value(column: str, value):
    if isinstance(value, str) and column in NVARCHAR_LIMITS:
        return value[: NVARCHAR_LIMITS[column]]
    return value


def sqlite_type(azure_type: str) -> str:
    if azure_type in ("BIT", "INT"):
        return "INTEGER"
    if azure_type == "FLOAT":
        return "REAL"
    return "TEXT"


def ensure_sqlite_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(daily_fare_snapshots)")}
    for name, azure_type in AZURE_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE daily_fare_snapshots ADD COLUMN {name} {sqlite_type(azure_type)}")


def ensure_azure_columns(cursor, table: str) -> None:
    for name, azure_type in AZURE_COLUMNS:
        cursor.execute(
            f"IF COL_LENGTH('{table}', '{name}') IS NULL "
            f"ALTER TABLE {table} ADD [{name}] {azure_type} NULL"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--departure-date", default=DEFAULT_DEPARTURE_DATE)
    parser.add_argument("--run-list", default=str(DEFAULT_RUN_LIST))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--tolerance-minutes", type=int, default=DEFAULT_TOLERANCE_MINUTES)
    parser.add_argument("--sink", choices=["both", "azure", "sqlite"], default="both")
    parser.add_argument(
        "--recreate-azure-table",
        action="store_true",
        help="DROP and recreate the Azure table before inserting (destructive).",
    )
    parser.add_argument("--no-telegram", action="store_true", help="Skip the Telegram run summary.")
    parser.add_argument(
        "--skip-if-collected-today",
        action="store_true",
        help="Exit without collecting if today's rows already exist (catch-up job safety).",
    )
    return parser.parse_args()


def pick_target_flight(results, target_minutes: int, tolerance_minutes: int, target_airline: str, nonstop_only: bool = True):
    candidates = []
    for result in results:
        if not airline_matches(result, target_airline):
            continue
        if nonstop_only and _flight_stops_count(result) != 0:
            continue
        departure_minutes = parse_clock_minutes(extract_departure_time(result))
        if departure_minutes is None:
            continue
        delta = abs(departure_minutes - target_minutes)
        if delta <= tolerance_minutes:
            candidates.append((delta, _flight_duration_minutes(result) or 10**9, result))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


DATE_ONLY_LINE_RE = re.compile(r"^([A-Z]{3})-([A-Z]{3})\s+(\d{4}-\d{2}-\d{2})(?:\s*<\s*(\d+(?:\.\d+)?)h)?$")
DATED_TARGET_LINE_RE = re.compile(r"^([A-Z]{3})-([A-Z]{3})\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s+(\d{1,2}:\d{2}\s*[AP]M)$")

MAX_TRIP_OPTIONS = 10


def parse_tracking_run_list(path: Path) -> list[dict]:
    """Run list lines are one of:
    - 'ROUTE Airline TIME' (fixed panel, default departure date, nonstop target)
    - 'ROUTE YYYY-MM-DD [<Nh]' (dated trip: all itineraries under optional cap)
    - 'ROUTE YYYY-MM-DD Airline TIME' (dated specific flight, stops allowed)"""
    dated: list[dict] = []
    fixed_lines: list[str] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        target_match = DATED_TARGET_LINE_RE.match(line)
        if target_match:
            origin, destination, departure_date, airline, departure_time = target_match.groups()
            dated.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "airline": airline.strip(),
                    "departure_time": departure_time.strip().upper(),
                    "departure_date": departure_date,
                    "match_mode": "dated_flight",
                    "service_label": f"{origin}-{destination} {departure_date} {airline.strip()} {departure_time.strip().upper()}",
                }
            )
            continue
        match = DATE_ONLY_LINE_RE.match(line)
        if match:
            origin, destination, departure_date, cap_hours = match.groups()
            dated.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "airline": "",
                    "departure_time": "",
                    "departure_date": departure_date,
                    "match_mode": "dated_trip",
                    "max_duration_minutes": int(float(cap_hours) * 60) if cap_hours else None,
                    "service_label": f"{origin}-{destination} {departure_date}",
                }
            )
        else:
            fixed_lines.append(line)
    fixed = parse_run_list_from_lines(fixed_lines)
    for service in fixed:
        service["departure_date"] = None
        service["match_mode"] = "scheduled"
    return fixed + dated


def parse_run_list_from_lines(lines: list[str]) -> list[dict]:
    from scripts.run_flight_panel_batch import RUN_LINE_RE

    services: list[dict] = []
    for line in lines:
        if line.startswith("*"):
            line = line[1:].strip()
        match = RUN_LINE_RE.match(line)
        if not match:
            raise ValueError(f"Could not parse run-list line: {line}")
        origin, destination, airline, departure_time = match.groups()
        services.append(
            {
                "origin": origin,
                "destination": destination,
                "airline": airline.strip(),
                "departure_time": departure_time.strip().upper(),
                "service_label": f"{origin}-{destination} {airline.strip()} {departure_time.strip().upper()}",
            }
        )
    return services


def base_row(service: dict, departure_date: date, query_ts: datetime) -> dict:
    origin = service["origin"]
    destination = service["destination"]
    query_date = query_ts.date()
    return {
        "snapshot_ts": query_ts.isoformat(timespec="seconds"),
        "query_date": query_date.isoformat(),
        "service_label": service["service_label"],
        "origin": origin,
        "destination": destination,
        "route": f"{origin}-{destination}",
        "departure_date": departure_date.isoformat(),
        "days_until_departure": (departure_date - query_date).days,
        "target_airline": service["airline"],
        "target_departure_time": service["departure_time"],
        "matched_target_flight": 0,
        "collection_status": "no_match",
        "error_message": None,
        "raw_match_json": None,
        **route_region_flags(origin, destination),
    }


def fetch_ranked_with_retry(origin: str, destination: str, departure_date: date):
    search_request = build_search(origin, destination, departure_date.isoformat())
    last_error = None
    for attempt in range(2):
        if attempt:
            time.sleep(5)
        try:
            results = fetch_flights(search_request)
        except FlightSearchError as exc:
            last_error = exc
            continue
        return rank_results(results, search_request), None
    return None, last_error


def snapshot_service(service: dict, departure_date: date, query_ts: datetime, tolerance_minutes: int) -> dict:
    row = base_row(service, departure_date, query_ts)
    target_minutes = parse_clock_minutes(service["departure_time"])
    if target_minutes is None:
        row["collection_status"] = "config_error"
        row["error_message"] = f"Unparseable target departure time: {service['departure_time']}"
        return row

    ranked, last_error = fetch_ranked_with_retry(service["origin"], service["destination"], departure_date)
    if ranked is None:
        row["collection_status"] = "search_error"
        row["error_message"] = str(last_error)[:500]
        return row
    nonstop_only = service.get("match_mode") != "dated_flight"
    target = pick_target_flight(ranked, target_minutes, tolerance_minutes, service["airline"], nonstop_only=nonstop_only)
    if target is None:
        nonstop_txt = "nonstop " if nonstop_only else ""
        row["error_message"] = f"No {nonstop_txt}{service['airline']} flight within {tolerance_minutes} min of {service['departure_time']}"
        return row

    fill_matched_row(row, target, ranked, service["origin"], service["destination"], departure_date)
    return row


def snapshot_dated_trip(service: dict, departure_date: date, query_ts: datetime) -> list[dict]:
    """Track all reasonably-priced-duration itineraries for a dated trip:
    every option under the max-duration cap, one row each, ranked by price."""
    row = base_row(service, departure_date, query_ts)
    ranked, last_error = fetch_ranked_with_retry(service["origin"], service["destination"], departure_date)
    if ranked is None:
        row["collection_status"] = "search_error"
        row["error_message"] = str(last_error)[:500]
        return [row]

    cap = service.get("max_duration_minutes")
    options = []
    for result in ranked:
        price_value = price_to_number(_flight_price_text(result))
        if price_value is None:
            continue
        duration_minutes = _flight_duration_minutes(result)
        if cap is not None and (duration_minutes is None or duration_minutes > cap):
            continue
        options.append((price_value, result))
    options.sort(key=lambda item: item[0])
    options = options[:MAX_TRIP_OPTIONS]

    if not options:
        cap_text = f"{cap / 60:g}h" if cap else "any duration"
        row["error_message"] = f"No priced itineraries under {cap_text}"
        return [row]

    rows = []
    for option_index, (_, result) in enumerate(options, start=1):
        option_row = dict(row)
        option_row["service_label"] = f"{service['service_label']} opt{option_index}"
        fill_matched_row(option_row, result, ranked, service["origin"], service["destination"], departure_date)
        rows.append(option_row)
    return rows


def fill_matched_row(row: dict, target, ranked, origin: str, destination: str, departure_date: date) -> dict:
    departure_time = extract_departure_time(target)
    arrival_time = extract_arrival_time(target)
    departure_minutes = parse_clock_minutes(departure_time)
    price_text = _flight_price_text(target)
    price_value = price_to_number(price_text)
    duration_minutes = _flight_duration_minutes(target)
    stops_count = _flight_stops_count(target)

    context_rows = build_competition_context(ranked)
    time_bucket = dep_time_bucket(departure_minutes)
    competitors = filtered_competitors_for(target, context_rows, time_bucket, departure_minutes)
    priced = [c for c in competitors if c.get("price_value") is not None]
    cheapest = min(priced, key=lambda c: c["price_value"]) if priced else None

    airline_name = _flight_name(target)
    normalized_result_airline = (airline_name or "").strip().lower()
    same_airline_rows = []
    different_airline_rows = []
    for competitor in competitors:
        competitor_airline = (competitor.get("airline") or "").strip().lower()
        if normalized_result_airline and competitor_airline == normalized_result_airline:
            same_airline_rows.append(competitor)
        else:
            different_airline_rows.append(competitor)
    any_airline_summary = summarize_competitor_group(competitors, price_value)
    same_airline_summary = summarize_competitor_group(same_airline_rows, price_value)
    different_airline_summary = summarize_competitor_group(different_airline_rows, price_value)
    target_alliance = alliance_for_airline(airline_name)
    foreign_count = 0
    us_count = 0
    same_alliance_flag = False
    same_alliance_rows = []
    for competitor in competitors:
        competitor_airline = competitor.get("airline")
        if is_us_airline(competitor_airline):
            us_count += 1
        else:
            foreign_count += 1
        competitor_alliance = alliance_for_airline(competitor_airline)
        if target_alliance and competitor_alliance == target_alliance:
            same_alliance_flag = True
            same_alliance_rows.append(competitor)
    same_alliance_summary = summarize_competitor_group(same_alliance_rows, price_value)

    row.update(
        {
            "matched_target_flight": 1,
            "collection_status": "matched",
            "airline": airline_name,
            "departure_time": departure_time,
            "arrival_time": arrival_time,
            "departure_minutes": departure_minutes,
            "duration_minutes": duration_minutes,
            "stops_count": stops_count,
            "base_fare_usd": price_value,
            "base_fare_text": price_text,
            "price_diff_vs_cheapest_competing_nonstop": (
                None if price_value is None or cheapest is None else price_value - cheapest["price_value"]
            ),
            "cheapest_competing_nonstop_price": None if cheapest is None else cheapest["price_value"],
            "cheapest_competing_nonstop_label": (
                None if cheapest is None else f"{cheapest.get('airline')} {cheapest.get('departure_time')}"
            ),
            "competing_nonstop_count": len(competitors),
            "alternate_nonstop_any_airline_count": any_airline_summary["count"],
            "alternate_nonstop_any_airline_flag": any_airline_summary["has_any"],
            "alternate_nonstop_any_airline_labels": any_airline_summary["labels"],
            "cheaper_than_any_airline_alternate_nonstop": any_airline_summary["cheaper_flag"],
            "price_diff_vs_cheapest_any_airline_alternate_nonstop": any_airline_summary["price_diff_vs_cheapest"],
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
            "alternate_nonstop_same_alliance_count": same_alliance_summary["count"],
            "alternate_nonstop_same_alliance_flag": same_alliance_summary["has_any"],
            "alternate_nonstop_same_alliance_labels": same_alliance_summary["labels"],
            "cheaper_than_same_alliance_alternate_nonstop": same_alliance_summary["cheaper_flag"],
            "price_diff_vs_cheapest_same_alliance_alternate_nonstop": same_alliance_summary["price_diff_vs_cheapest"],
            "has_foreign_nonstop_competitor": foreign_count > 0,
            "foreign_nonstop_competitor_count": foreign_count,
            "us_nonstop_competitor_count": us_count,
            "same_alliance_nonstop_competitor_flag": same_alliance_flag,
            "flight_id": make_flight_id(
                origin,
                destination,
                departure_date,
                airline_name,
                departure_time,
                arrival_time,
                getattr(target, "duration", ""),
                str(stops_count),
            ),
            "raw_match_json": json.dumps(
                {
                    "name": _flight_name(target),
                    "departure": departure_time,
                    "arrival": arrival_time,
                    "price": price_text,
                    "stops": stops_count,
                }
            ),
        }
    )
    return row


COLUMNS = [name for name, _ in AZURE_COLUMNS]


def insert_rows_sqlite(db_path: Path, rows: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        ensure_sqlite_columns(conn)
        conn.executemany(
            f"INSERT OR REPLACE INTO daily_fare_snapshots ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in COLUMNS)})",
            [tuple(row.get(col) for col in COLUMNS) for row in rows],
        )


def azure_connect():
    return pymssql.connect(
        server=os.environ["AZURE_SQL_SERVER"],
        user=os.environ["AZURE_SQL_USER"],
        password=os.environ["AZURE_SQL_PASSWORD"],
        database=os.environ["AZURE_SQL_DATABASE"],
        tds_version="7.3",
    )


def insert_rows_azure(rows: list[dict], recreate_table: bool = False) -> None:
    table = os.environ.get("AZURE_SQL_TABLE", "dbo.FlightSifta")
    col_defs = ", ".join(f"[{name}] {sql_type}" for name, sql_type in AZURE_COLUMNS)
    conn = azure_connect()
    try:
        cursor = conn.cursor()
        if recreate_table:
            cursor.execute(f"IF OBJECT_ID('{table}', 'U') IS NOT NULL DROP TABLE {table}")
        cursor.execute(
            f"IF OBJECT_ID('{table}', 'U') IS NULL "
            f"CREATE TABLE {table} (id INT IDENTITY(1,1) PRIMARY KEY, {col_defs})"
        )
        ensure_azure_columns(cursor, table)
        if rows:
            query_date = rows[0]["query_date"]
            labels = sorted({r["service_label"] for r in rows})
            label_placeholders = ", ".join(["%s"] * len(labels))
            cursor.execute(
                f"DELETE FROM {table} WHERE query_date = %s "
                f"AND service_label IN ({label_placeholders})",
                (query_date, *labels),
            )
            placeholders = ", ".join(["%s"] * len(COLUMNS))
            col_names = ", ".join(f"[{c}]" for c in COLUMNS)
            cursor.executemany(
                f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})",
                [tuple(azure_value(col, row.get(col)) for col in COLUMNS) for row in rows],
            )
        conn.commit()
        cursor.close()
    finally:
        conn.close()


def load_prior_fares(db_path: Path, query_date: str) -> dict[str, float]:
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "daily_fare_snapshots" not in tables:
            return {}
        prior_date = conn.execute(
            "SELECT MAX(query_date) FROM daily_fare_snapshots WHERE query_date < ?", (query_date,)
        ).fetchone()[0]
        if not prior_date:
            return {}
        return {
            label: fare
            for label, fare in conn.execute(
                "SELECT service_label, base_fare_usd FROM daily_fare_snapshots WHERE query_date = ?",
                (prior_date,),
            )
            if fare is not None
        }


def send_telegram_summary(rows: list[dict], query_date: str, total_services: int, prior_fares: dict[str, float]) -> None:
    token = os.environ.get("FAST_FLIGHTS_BOT_TOKEN")
    chat_id = os.environ.get("FLIGHT_BOT_CHAT_ID")
    if not token or not chat_id:
        print("Telegram summary skipped (FAST_FLIGHTS_BOT_TOKEN / FLIGHT_BOT_CHAT_ID not set)")
        return
    matched = sum(r["matched_target_flight"] for r in rows)
    problem_rows = [r for r in rows if r["collection_status"] in ("no_match", "search_error", "config_error")]
    problem_labels = sorted({r["service_label"] for r in problem_rows})
    lines = [
        f"Fare snapshot {query_date} complete",
        f"{total_services} routes checked: {matched} collected, {len(problem_labels)} no-match",
    ]
    trip_best: dict[str, dict] = {}
    for row in rows:
        if row["service_label"].endswith(" opt1") and row["matched_target_flight"]:
            trip_best[row["service_label"].rsplit(" opt1", 1)[0]] = row
    if trip_best:
        lines.append("")
        lines.append("Your trips (cheapest reasonable today):")
        for label in sorted(trip_best, key=lambda l: trip_best[l]["departure_date"]):
            row = trip_best[label]
            fare = row["base_fare_usd"]
            prior = prior_fares.get(row["service_label"])
            delta = ""
            if prior is not None and fare is not None:
                change = fare - prior
                if change > 0:
                    delta = f" (+${change:.0f})"
                elif change < 0:
                    delta = f" (-${abs(change):.0f})"
                else:
                    delta = " (flat)"
            lines.append(f"  {label}: ${fare:.0f}{delta}")
    if problem_labels:
        lines.append("")
        lines.append(f"No-match ({len(problem_labels)}): " + ", ".join(problem_labels[:10]) + (" ..." if len(problem_labels) > 10 else ""))
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines)},
            timeout=15,
        )
        resp.raise_for_status()
        print("Telegram summary sent")
    except Exception as exc:
        print(f"Telegram summary failed (run data still saved): {exc}")


LOCK_PATH = ROOT / "data" / ".fare_snapshot.lock"


def acquire_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text().strip())
            os.kill(pid, 0)
            return False  # another live run holds the lock
        except (ValueError, ProcessLookupError, PermissionError):
            LOCK_PATH.unlink(missing_ok=True)  # stale lock
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def main() -> None:
    args = parse_args()
    if not acquire_lock():
        print("Another fare snapshot run is already in progress — exiting")
        return
    try:
        run(args)
    finally:
        release_lock()


def run(args: argparse.Namespace) -> None:
    query_ts = datetime.now()
    if args.skip_if_collected_today:
        db_path = Path(args.db)
        if db_path.exists():
            with sqlite3.connect(db_path) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "daily_fare_snapshots" in tables:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM daily_fare_snapshots WHERE query_date = ?",
                        (query_ts.date().isoformat(),),
                    ).fetchone()[0]
                    if count > 0:
                        print(f"Catch-up skipped: {count} rows already collected for {query_ts.date()}")
                        return
    services = parse_tracking_run_list(Path(args.run_list))
    default_departure_date = datetime.strptime(args.departure_date, "%Y-%m-%d").date()
    rows = []
    active_services = []
    for service in services:
        service_date = (
            datetime.strptime(service["departure_date"], "%Y-%m-%d").date()
            if service.get("departure_date")
            else default_departure_date
        )
        if service_date < query_ts.date():
            print(f"[skip] {service['service_label']} — departure {service_date} has passed, lifecycle expired")
            continue
        active_services.append((service, service_date))
    for index, (service, service_date) in enumerate(active_services, start=1):
        if service.get("match_mode") == "dated_trip":
            service_rows = snapshot_dated_trip(service, service_date, query_ts)
            rows.extend(service_rows)
            matched_rows = [r for r in service_rows if r["matched_target_flight"]]
            cheapest_fare = matched_rows[0]["base_fare_usd"] if matched_rows else None
            fare = f"${cheapest_fare:.0f}" if cheapest_fare is not None else "n/a"
            print(
                f"[{index}/{len(active_services)}] {service['service_label']} -> {service_rows[0]['collection_status']}"
                f" | {len(matched_rows)} options | cheapest {fare}"
            )
        else:
            row = snapshot_service(service, service_date, query_ts, args.tolerance_minutes)
            rows.append(row)
            fare = f"${row['base_fare_usd']:.0f}" if row.get("base_fare_usd") is not None else "n/a"
            print(
                f"[{index}/{len(active_services)}] {service['service_label']} -> {row['collection_status']}"
                f" | fare {fare} | competitors {row.get('competing_nonstop_count')}"
                + (f" | {row['error_message']}" if row.get("error_message") else "")
            )
    prior_fares = load_prior_fares(Path(args.db), query_ts.date().isoformat())
    if args.sink in ("both", "sqlite"):
        insert_rows_sqlite(Path(args.db), rows)
        print(f"Logged {len(rows)} rows to SQLite {args.db}")
    if args.sink in ("both", "azure"):
        insert_rows_azure(rows, recreate_table=args.recreate_azure_table)
        print(f"Pushed {len(rows)} rows to Azure SQL {os.environ.get('AZURE_SQL_TABLE', 'dbo.FlightSifta')}")
    matched = sum(r["matched_target_flight"] for r in rows)
    print(f"Done: {len(rows)} rows ({matched} matched)")
    if not args.no_telegram:
        send_telegram_summary(rows, query_ts.date().isoformat(), len(active_services), prior_fares)


if __name__ == "__main__":
    main()
