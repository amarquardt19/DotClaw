#!/usr/bin/env python3
"""Query deals.db with nicely formatted terminal output."""
import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "deals.db"

# Column definitions: (db_col, display_header, width, align, formatter)
COLUMNS = [
    ("uid",             "UID",       4, ">", None),
    ("property_name",   "Property",  28, "<", None),
    ("city",            "City",      16, "<", None),
    ("state",           "ST",         2, "<", None),
    ("msa",             "MSA",       30, "<", None),
    ("email_type",      "Email Type",16, "<", None),
    ("offers_due_date", "Offers Due",14, "<", None),
    ("units",           "Units",      6, ">", lambda v: f"{v:,}" if v else ""),
    ("year_built",      "Yr Built",   7, ">", None),
    ("occupancy",       "Occ%",       5, ">", lambda v: f"{v:.0f}%" if v else ""),
    ("avg_rent",        "Rent",       7, ">", lambda v: f"${v:,.0f}" if v else ""),
    ("avg_unit_size",   "Avg SF",     7, ">", lambda v: f"{v:,.0f}" if v else ""),
]

RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
CYAN  = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
BLUE  = "\033[34m"

TAG_COLORS = {
    "Call for Offers":      GREEN,
    "New Offering":         CYAN,
    "Offering Memorandum":  YELLOW,
    "Now Touring":          MAGENTA,
    "New Listing":          BLUE,
}


def fmt_val(val, col_def):
    _, _, width, align, formatter = col_def
    if val is None or val == "":
        s = ""
    elif formatter:
        s = formatter(val)
    else:
        s = str(val)
    # Truncate if too long
    if len(s) > width:
        s = s[:width - 1] + "…"
    if align == ">":
        return s.rjust(width)
    return s.ljust(width)


def color_email_type(val, width):
    if not val:
        return "".ljust(width)
    s = str(val)
    if len(s) > width:
        s = s[:width - 1] + "…"
    color = TAG_COLORS.get(val, DIM)
    padded = s.ljust(width)
    return f"{color}{padded}{RESET}"


def print_table(rows, filter_text=None):
    if filter_text:
        fl = filter_text.lower()
        rows = [r for r in rows if fl in " ".join(str(v or "") for v in r).lower()]

    # Header
    header = "  ".join(
        f"{BOLD}{CYAN}{col[1].ljust(col[2]) if col[3] == '<' else col[1].rjust(col[2])}{RESET}"
        for col in COLUMNS
    )
    sep = DIM + "─" * (sum(c[2] for c in COLUMNS) + 2 * (len(COLUMNS) - 1)) + RESET
    print(f"\n{header}")
    print(sep)

    for row in rows:
        parts = []
        for i, col_def in enumerate(COLUMNS):
            val = row[i]
            if col_def[0] == "email_type":
                parts.append(color_email_type(val, col_def[2]))
            else:
                parts.append(fmt_val(val, col_def))
        print("  ".join(parts))

    print(sep)
    print(f"{DIM}{len(rows)} deal(s){RESET}\n")


def main():
    ap = argparse.ArgumentParser(description="Query CRE deals")
    ap.add_argument("filter", nargs="?", default=None, help="Filter text (searches all columns)")
    ap.add_argument("--msa", help="Filter by MSA (partial match)")
    ap.add_argument("--state", help="Filter by state (2-letter)")
    ap.add_argument("--type", dest="email_type", help="Filter by email type")
    ap.add_argument("--sort", default="property_name", help="Sort column (default: property_name)")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    db_cols = ", ".join(c[0] for c in COLUMNS)

    where = ["is_deal = 1"]
    params = []
    if args.msa:
        where.append("msa LIKE ?")
        params.append(f"%{args.msa}%")
    if args.state:
        where.append("UPPER(state) = UPPER(?)")
        params.append(args.state)
    if args.email_type:
        where.append("email_type LIKE ?")
        params.append(f"%{args.email_type}%")

    valid_sorts = {c[0] for c in COLUMNS}
    sort_col = args.sort if args.sort in valid_sorts else "property_name"

    sql = f"SELECT {db_cols} FROM deals WHERE {' AND '.join(where)} ORDER BY {sort_col}"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    print_table(rows, filter_text=args.filter)


if __name__ == "__main__":
    main()
