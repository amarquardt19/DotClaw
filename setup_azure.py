"""Create deals table in Azure SQL and migrate data from SQLite."""
import pymssql
import sqlite3
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "deals.db"

def get_azure_conn():
    return pymssql.connect(
        server=os.getenv('AZURE_SQL_SERVER'),
        user=os.getenv('AZURE_SQL_USER'),
        password=os.getenv('AZURE_SQL_PASSWORD'),
        database=os.getenv('AZURE_SQL_DATABASE')
    )

def create_table():
    conn = get_azure_conn()
    cursor = conn.cursor()
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='deals' AND xtype='U')
        CREATE TABLE deals (
            uid              INT PRIMARY KEY,
            subject          NVARCHAR(500),
            received_date    NVARCHAR(50),
            property_name    NVARCHAR(300),
            address          NVARCHAR(500),
            city             NVARCHAR(100),
            state            NVARCHAR(10),
            msa              NVARCHAR(200),
            asset_type       NVARCHAR(100),
            email_type       NVARCHAR(100),
            offers_due_date  NVARCHAR(100),
            units            INT,
            year_built       NVARCHAR(20),
            avg_unit_size    FLOAT,
            occupancy        FLOAT,
            avg_rent         FLOAT,
            rent_per_sf      FLOAT,
            asking_price     NVARCHAR(100),
            cap_rate         FLOAT,
            price_per_unit   FLOAT,
            broker_name      NVARCHAR(200),
            broker_company   NVARCHAR(200),
            highlights       NVARCHAR(MAX),
            raw_ocr_text     NVARCHAR(MAX),
            extracted_at     DATETIME DEFAULT GETUTCDATE(),
            is_deal          INT DEFAULT 1
        )
    """)
    conn.commit()
    print("✓ deals table created in Azure SQL")
    conn.close()

def migrate_data():
    # Read from SQLite
    lite = sqlite3.connect(str(DB_PATH))
    lite.row_factory = sqlite3.Row
    rows = lite.execute("SELECT * FROM deals").fetchall()
    cols = rows[0].keys() if rows else []
    lite.close()

    # Insert into Azure SQL
    azure = get_azure_conn()
    cursor = azure.cursor()

    # Filter to columns that exist in Azure table
    azure_cols = [
        'uid', 'subject', 'received_date', 'property_name', 'address',
        'city', 'state', 'msa', 'asset_type', 'email_type', 'offers_due_date',
        'units', 'year_built', 'avg_unit_size', 'occupancy', 'avg_rent',
        'rent_per_sf', 'asking_price', 'cap_rate', 'price_per_unit',
        'broker_name', 'broker_company', 'highlights', 'raw_ocr_text', 'is_deal'
    ]
    use_cols = [c for c in azure_cols if c in cols]
    placeholders = ", ".join(["%s"] * len(use_cols))
    col_names = ", ".join(use_cols)

    inserted = 0
    for row in rows:
        vals = []
        for c in use_cols:
            v = row[c]
            if v is None or v == "":
                vals.append(None)
            else:
                vals.append(v)
        try:
            cursor.execute(
                f"INSERT INTO deals ({col_names}) VALUES ({placeholders})",
                tuple(vals)
            )
            inserted += 1
        except Exception as e:
            if "Violation of PRIMARY KEY" in str(e):
                pass  # already exists
            else:
                print(f"  Error uid={row['uid']}: {e}")

    azure.commit()
    print(f"✓ Migrated {inserted} rows from SQLite to Azure SQL")
    azure.close()

def create_deal_intake_table():
    conn = get_azure_conn()
    cursor = conn.cursor()
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='deal_intake' AND xtype='U')
        CREATE TABLE deal_intake (
            uid                        INT PRIMARY KEY,
            -- Scoring (top-level)
            score                      FLOAT,
            residential_interest_score  FLOAT,
            industrial_interest_score   FLOAT,
            -- Deal fields
            asset_class                NVARCHAR(100),
            product                    NVARCHAR(10),
            deal_name                  NVARCHAR(300),
            city                       NVARCHAR(100),
            state                      NVARCHAR(10),
            units                      INT,
            guidance_price             FLOAT,
            offer_due_date             NVARCHAR(100),
            transaction_type           NVARCHAR(200),
            sponsor                    NVARCHAR(300),
            broker                     NVARCHAR(300),
            broker_name                NVARCHAR(300),
            broker_firm                NVARCHAR(300),
            broker_email               NVARCHAR(300),
            broker_phone               NVARCHAR(100),
            rent                       NVARCHAR(200),
            occupancy                  FLOAT,
            vintage                    INT,
            notes                      NVARCHAR(MAX),
            -- Email metadata
            email_received_utc         NVARCHAR(50),
            email_from                 NVARCHAR(200),
            email_subject              NVARCHAR(500),
            source_mailbox             NVARCHAR(200),
            downloaded_files_count     INT,
            -- Extended deal fields
            address                    NVARCHAR(500),
            msa                        NVARCHAR(200),
            rent_psf                   FLOAT,
            avg_rent_monthly           FLOAT,
            rent_units                 NVARCHAR(50),
            unit_size                  FLOAT,
            buildings                  INT,
            building_size              FLOAT,
            price_per_sf               FLOAT,
            in_place_cap_rate          FLOAT,
            lease_type                 NVARCHAR(100),
            annual_escalations         FLOAT,
            going_in_cash_yield        FLOAT,
            discount_to_replacement    FLOAT,
            seller                     NVARCHAR(300),
            renovated                  BIT,
            renovation_year            INT,
            tenants                    NVARCHAR(MAX),
            walt                       FLOAT,
            flood_zone                 BIT,
            site_coverage_ratio        FLOAT,
            credit_tenant              BIT,
            clear_height               FLOAT,
            ceiling_height             FLOAT,
            message_id                 NVARCHAR(500),
            -- Scoring components
            is_residential             BIT,
            is_industrial              BIT,
            residential_gated          BIT,
            residential_gating_reason  NVARCHAR(100),
            residential_geo_score      FLOAT,
            residential_units_score    FLOAT,
            residential_price_score    FLOAT,
            residential_rent_score     FLOAT,
            residential_vintage_score  FLOAT,
            residential_returns_score  FLOAT,
            residential_strategy_score FLOAT,
            residential_notes_boost    FLOAT,
            score_version              NVARCHAR(50),
            score_missing_inputs       NVARCHAR(500),
            score_breakdown            NVARCHAR(MAX),
            -- Internal
            raw_ocr_text               NVARCHAR(MAX),
            extracted_at               DATETIME DEFAULT GETUTCDATE()
        )
    """)
    conn.commit()
    print("✓ deal_intake table created in Azure SQL")
    conn.close()


def create_raw_deal_emails_table():
    conn = get_azure_conn()
    cursor = conn.cursor()
    cursor.execute("""
        IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='raw_deal_emails' AND xtype='U')
        CREATE TABLE raw_deal_emails (
            uid                    INT PRIMARY KEY,
            -- Email metadata
            email_subject          NVARCHAR(500),
            email_from             NVARCHAR(200),
            email_received_utc     NVARCHAR(50),
            message_id             NVARCHAR(500),
            source_mailbox         NVARCHAR(200),
            -- Raw text content
            scraped_text           NVARCHAR(MAX),
            ocr_text               NVARCHAR(MAX),
            -- Claude extraction (raw JSON)
            extracted_json         NVARCHAR(MAX),
            -- Python-side enrichments
            resolved_msa           NVARCHAR(200),
            web_avg_rent_monthly   FLOAT,
            -- Gating
            pre_gated              BIT DEFAULT 0,
            gate_reason            NVARCHAR(100),
            -- Timestamps
            inserted_at            DATETIME DEFAULT GETUTCDATE()
        )
    """)
    conn.commit()
    print("✓ raw_deal_emails table created in Azure SQL")
    conn.close()


if __name__ == "__main__":
    create_table()
    create_deal_intake_table()
    create_raw_deal_emails_table()
    migrate_data()

    # Verify
    azure = get_azure_conn()
    cursor = azure.cursor()
    cursor.execute("SELECT COUNT(*) FROM deals WHERE is_deal=1")
    count = cursor.fetchone()[0]
    print(f"✓ Azure SQL now has {count} deals")
    azure.close()
