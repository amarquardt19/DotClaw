#!/usr/bin/env python3
"""Update the dbo.v_deals view to reorder columns.

Usage:
  - Set environment variables: AZ_SQL_USER, AZ_SQL_PASS, AZ_SQL_SERVER, AZ_SQL_DB
  - Or pass a full SQLAlchemy URL with --url

Then run:
  python scripts/update_v_deals.py

The script will execute a CREATE OR ALTER VIEW statement that places
`received_date` immediately after `property_name` and moves `address`
after `broker_company`.
"""
import os
import sys
import argparse
from urllib.parse import quote_plus
from sqlalchemy import create_engine, text


VIEW_DDL = '''
CREATE OR ALTER VIEW dbo.v_deals AS
SELECT
  uid,
  property_name,
  received_date,
  city,
  state,
  msa,
  email_type,
  offers_due_date,
  units,
  year_built,
  occupancy,
  avg_rent,
  avg_unit_size,
  asking_price,
  cap_rate,
  broker_name,
  broker_company,
  address,
  highlights,
  raw_ocr_text,
  extracted_at,
  is_deal
FROM dbo.deals
WHERE is_deal = 1;
'''


def build_url_from_env():
    user = os.environ.get('AZ_SQL_USER')
    pwd = os.environ.get('AZ_SQL_PASS')
    server = os.environ.get('AZ_SQL_SERVER')
    db = os.environ.get('AZ_SQL_DB')
    if not all([user, pwd, server, db]):
        return None
    odbc = f"Driver={{ODBC Driver 18 for SQL Server}};Server={server},1433;Database={db};Uid={user};Pwd={pwd};Encrypt=yes;TrustServerCertificate=no;"
    return 'mssql+pyodbc:///?odbc_connect=' + quote_plus(odbc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', help='Full SQLAlchemy URL (optional)')
    p.add_argument('--dry-run', action='store_true', help='Show DDL and exit')
    args = p.parse_args()

    url = args.url or build_url_from_env()
    if args.dry_run:
        print('--- DDL to apply ---')
        print(VIEW_DDL)
        return

    if not url:
        print('No connection URL available. Set AZ_SQL_USER/AZ_SQL_PASS/AZ_SQL_SERVER/AZ_SQL_DB or pass --url', file=sys.stderr)
        sys.exit(2)

    print('Connecting to database...')
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            print('Applying view DDL...')
            conn.execute(text(VIEW_DDL))
        print('View dbo.v_deals updated successfully.')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
