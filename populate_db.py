"""
Populate investments.db properties table from Claude-extracted inbox_deals.csv.
Replaces existing rows by email_uid with clean data.
"""
import csv
import json
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.expanduser("~/.openclaw/workspace/email_pipeline/data/investments.db")
CSV_PATH = os.path.expanduser("~/.openclaw/workspace/reports/inbox_deals.csv")


def parse_rent(rent_str: str):
    """Extract numeric rent from strings like '$1,802' or '$2,396/Month'."""
    if not rent_str:
        return None
    import re
    m = re.search(r"\$?([\d,]+(?:\.\d+)?)", rent_str.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def parse_occ(occ_str: str):
    """Extract numeric occupancy from strings like '98.9%' or '94'."""
    if not occ_str:
        return None
    import re
    m = re.search(r"([\d.]+)", occ_str)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def parse_int_or_none(val):
    if val is None or val == "":
        return None
    try:
        return int(val)
    except Exception:
        return None


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Read {len(rows)} rows from CSV")

    updated = 0
    inserted = 0

    for row in rows:
        email_uid = parse_int_or_none(row.get("id"))
        if email_uid is None:
            continue

        name = row.get("property", "").strip() or None
        market = row.get("market", "").strip() or None
        asset_type = row.get("asset_type", "").strip() or None
        units = parse_int_or_none(row.get("units"))
        rent_per_unit = parse_rent(row.get("rent", ""))
        occupancy = parse_occ(row.get("occupancy", ""))
        vintage = str(row.get("year_built", "")).strip() or None

        metadata = {}
        subject = row.get("subject", "").strip()
        if subject:
            metadata["subject"] = subject
        status = row.get("status", "").strip()
        if status:
            metadata["extraction_status"] = status

        metadata_json = json.dumps(metadata) if metadata else None

        # Check if row exists
        cur.execute("SELECT id FROM properties WHERE email_uid = ?", (email_uid,))
        existing = cur.fetchone()

        if existing:
            cur.execute(
                """UPDATE properties SET
                    name = ?,
                    market = ?,
                    asset_type = ?,
                    units = ?,
                    rent_per_unit = ?,
                    occupancy = ?,
                    vintage = ?,
                    metadata = ?
                WHERE email_uid = ?""",
                (name, market, asset_type, units, rent_per_unit, occupancy, vintage, metadata_json, email_uid),
            )
            updated += 1
            print(f"  Updated uid={email_uid}: {name}")
        else:
            cur.execute(
                """INSERT INTO properties (email_uid, name, market, asset_type, units, rent_per_unit, occupancy, vintage, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (email_uid, name, market, asset_type, units, rent_per_unit, occupancy, vintage, metadata_json),
            )
            inserted += 1
            print(f"  Inserted uid={email_uid}: {name}")

        # Also ensure emails table has this uid
        cur.execute("SELECT id FROM emails WHERE uid = ?", (email_uid,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO emails (uid, subject) VALUES (?, ?)",
                (email_uid, subject),
            )

    conn.commit()
    conn.close()

    print(f"\nDone. Updated: {updated}, Inserted: {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
