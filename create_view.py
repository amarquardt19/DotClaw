"""Create v_deals view in Azure SQL."""
import pymssql
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

conn = pymssql.connect(
    server=os.getenv('AZURE_SQL_SERVER'),
    user=os.getenv('AZURE_SQL_USER'),
    password=os.getenv('AZURE_SQL_PASSWORD'),
    database=os.getenv('AZURE_SQL_DATABASE')
)
cursor = conn.cursor()

# Drop existing view
try:
    cursor.execute("DROP VIEW v_deals")
    conn.commit()
except:
    conn.rollback()

# Create view with desired column order and naming
cursor.execute("""
CREATE VIEW v_deals AS
SELECT
    property_name,
    address,
    city,
    state,
    msa AS [Metro Area],
    asset_type,
    email_type,
    offers_due_date,
    units,
    year_built,
    avg_unit_size,
    occupancy,
    avg_rent,
    rent_per_sf,
    asking_price,
    cap_rate,
    price_per_unit,
    broker_name,
    broker_company,
    highlights,
    subject,
    received_date,
    extracted_at
FROM deals
WHERE is_deal = 1
""")
conn.commit()
print("v_deals view created. Query with: SELECT * FROM v_deals")
conn.close()
