#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.collect_sfo_ase_panel import (
    DEFAULT_OUTPUT,
    apply_derived_fields,
    collect_rows,
    load_existing_sheet,
    parse_clock_minutes,
    save_sheet,
)

RUN_LINE_RE = re.compile(r"^([A-Z]{3})-([A-Z]{3})\s+(.+?)\s+(\d{1,2}:\d{2}\s*[AP]M)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-list", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--sheet-name", default="sfo_ase_panel_target_only")
    parser.add_argument("--tolerance-minutes", type=int, default=10)
    parser.add_argument("--save-every", type=int, default=1)
    return parser.parse_args()


def parse_run_list(path: Path) -> list[dict[str, str]]:
    services: list[dict[str, str]] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("*"):
            line = line[1:].strip()
        match = RUN_LINE_RE.match(line)
        if not match:
            raise ValueError(f"Could not parse run-list line: {raw_line}")
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
    if not services:
        raise ValueError("Run list is empty.")
    return services


def combine_frames(existing: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        combined = new_df
    elif new_df.empty:
        combined = existing
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)
    if combined.empty:
        return combined
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
    return apply_derived_fields(combined)


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def main() -> None:
    args = parse_args()
    run_list_path = Path(args.run_list)
    services = parse_run_list(run_list_path)
    start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    output_path = Path(args.output)
    existing = load_existing_sheet(output_path, args.sheet_name)
    total = len(services)
    batch_started = datetime.now()
    pending_new_frames: list[pd.DataFrame] = []

    for index, service in enumerate(services, start=1):
        service_started = datetime.now()
        elapsed_before = (service_started - batch_started).total_seconds()
        avg_before = elapsed_before / (index - 1) if index > 1 else None
        eta_before = avg_before * (total - index + 1) if avg_before is not None else None
        print(
            f"[{index}/{total}] Starting {service['service_label']}"
            + (f" | elapsed {format_duration(elapsed_before)} | eta {format_duration(eta_before)}" if eta_before is not None else "")
        )
        target_minutes = parse_clock_minutes(service["departure_time"])
        if target_minutes is None:
            raise ValueError(f"Could not parse departure time: {service['departure_time']}")
        new_rows = collect_rows(
            origin=service["origin"],
            destination=service["destination"],
            start_date=start_date,
            end_date=end_date,
            target_minutes=target_minutes,
            tolerance_minutes=args.tolerance_minutes,
            target_only=True,
            target_airline=service["airline"],
            service_label=service["service_label"],
            target_nonstop_only=True,
        )
        new_df = pd.DataFrame(new_rows)
        pending_new_frames.append(new_df)
        service_elapsed = (datetime.now() - service_started).total_seconds()
        matched_count = 0 if new_df.empty else int(new_df.get("matched_target_flight", pd.Series(dtype=bool)).fillna(False).sum())
        print(
            f"[{index}/{total}] Finished {service['service_label']}"
            f" | rows {len(new_df)} | matched {matched_count} | took {format_duration(service_elapsed)}"
        )
        should_save = (index % args.save_every == 0) or (index == total)
        if should_save:
            combined_new = pd.concat(pending_new_frames, ignore_index=True) if pending_new_frames else pd.DataFrame()
            existing = combine_frames(existing, combined_new)
            save_sheet(existing, output_path, args.sheet_name)
            pending_new_frames = []
            total_elapsed = (datetime.now() - batch_started).total_seconds()
            avg_per_service = total_elapsed / index
            remaining = avg_per_service * (total - index)
            print(
                f"[{index}/{total}] Saved progress to {output_path}"
                f" | total rows {len(existing)} | elapsed {format_duration(total_elapsed)} | eta {format_duration(remaining)}"
            )

    total_elapsed = (datetime.now() - batch_started).total_seconds()
    print(f"Completed batch of {total} flights in {format_duration(total_elapsed)}")


if __name__ == "__main__":
    main()
