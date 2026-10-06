#!/usr/bin/env python3
"""Deduplicate the `deals` table in a SQLite database.

By default the script shows duplicate groups and a dry-run summary.
Use `--apply` to make a backup and remove duplicates, keeping the row
with the smallest `uid` for each `property_name, received_date` group.
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime


def find_duplicates(conn):
    q = '''
    SELECT property_name, received_date, COUNT(*) AS cnt, group_concat(uid) AS uids
    FROM deals
    GROUP BY property_name, received_date
    HAVING cnt>1
    ORDER BY cnt DESC
    '''
    cur = conn.execute(q)
    return cur.fetchall()


def rows_to_remove_count(conn):
    q = '''
    SELECT SUM(cnt-1) AS rows_to_remove FROM (
      SELECT COUNT(*) AS cnt FROM deals GROUP BY property_name, received_date HAVING cnt>1
    )
    '''
    cur = conn.execute(q)
    r = cur.fetchone()
    return r[0] if r and r[0] is not None else 0


def preview_rows_to_remove(conn, limit=50):
    q = '''
    SELECT uid, property_name, substr(subject,1,120) AS subject_preview, received_date
    FROM deals
    WHERE uid IN (
      SELECT uid FROM deals WHERE uid NOT IN (SELECT MIN(uid) FROM deals GROUP BY property_name, received_date)
    )
    ORDER BY property_name, received_date
    LIMIT ?
    '''
    cur = conn.execute(q, (limit,))
    return cur.fetchall()


def apply_dedupe(conn):
    # delete duplicates, keep the lowest uid per group
    q = """
    DELETE FROM deals
    WHERE uid NOT IN (
      SELECT MIN(uid) FROM deals GROUP BY property_name, received_date
    )
    """
    cur = conn.execute(q)
    return cur.rowcount


def main():
    p = argparse.ArgumentParser(description="Dedupe deals.db by property_name + received_date")
    p.add_argument("--db", default="deals.db", help="Path to deals.db")
    p.add_argument("--apply", action="store_true", help="Apply changes (will backup DB first)")
    p.add_argument("--vacuum", action="store_true", help="Run VACUUM after applying")
    p.add_argument("--limit-preview", type=int, default=50, help="How many sample rows to show")
    args = p.parse_args()

    db = args.db

    try:
        conn = sqlite3.connect(db)
    except sqlite3.Error as e:
        print("Failed to open DB:", e)
        sys.exit(2)

    conn.row_factory = sqlite3.Row

    dup_groups = find_duplicates(conn)
    total_dups = sum(r[2] for r in dup_groups) if dup_groups else 0
    rows_to_remove = rows_to_remove_count(conn)

    print(f"DB: {db}")
    cur = conn.execute('SELECT COUNT(*) FROM deals')
    total_rows = cur.fetchone()[0]
    print(f"Total rows before: {total_rows}")
    print(f"Duplicate groups: {len(dup_groups)}")
    print(f"Rows that would be removed: {rows_to_remove}\n")

    if dup_groups:
        print("Sample duplicate groups (property_name | received_date | cnt | uids):")
        for row in dup_groups[:50]:
            print(f"- {row['property_name']!s} | {row['received_date']!s} | {row['cnt']} | {row['uids']}")

    sample = preview_rows_to_remove(conn, limit=args.limit_preview)
    if sample:
        print("\nSample rows that would be removed (uid | property_name | subject_preview | received_date):")
        for r in sample:
            print(f"- {r['uid']} | {r['property_name']!s} | {r['subject_preview']!s} | {r['received_date']!s}")

    if not args.apply:
        print("\nDry run complete. Run with --apply to perform dedupe (a backup will be created).")
        return

    # backup
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = f"{db}.bak.{ts}"
    print(f"Creating backup: {backup}")
    shutil.copy2(db, backup)

    print("Applying dedupe: deleting duplicate rows...")
    cur = conn.cursor()
    try:
        cur.execute('BEGIN')
        deleted = apply_dedupe(conn)
        cur.execute('COMMIT')
    except Exception:
        cur.execute('ROLLBACK')
        raise

    print(f"Deleted {deleted} rows.")

    if args.vacuum:
        print("Running VACUUM to reclaim space...")
        conn.execute('VACUUM')

    cur = conn.execute('SELECT COUNT(*) FROM deals')
    print(f"Total rows after: {cur.fetchone()[0]}")


if __name__ == '__main__':
    main()
