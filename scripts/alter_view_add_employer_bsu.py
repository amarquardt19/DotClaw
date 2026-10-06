import sys

sys.path.insert(0, "scripts")
from apartments_com_scraper import _get_azure_conn

DDL = """
ALTER VIEW dbo.apartment_deals_cleaned AS
SELECT
    CAST(apartment_deal_id AS FLOAT) AS apartment_deal_id,
    CAST(score AS FLOAT) AS score,
    deal_name, address, city, state, county, neighborhood, transaction_type,
    CAST(units AS FLOAT) AS units,
    CAST(vintage AS INT) AS vintage,
    CAST(renovation_year AS FLOAT) AS renovation_year,
    renovated,
    CAST(example_rent_one_bed AS FLOAT) AS example_rent_one_bed,
    CAST(rent_psf AS FLOAT) AS rent_psf,
    CAST(example_one_bed_unit_size AS FLOAT) AS example_one_bed_unit_size,
    CAST(geo_score AS FLOAT) AS state_score,
    CAST(metro_score AS FLOAT) AS metro_score,
    CAST(parking_score AS FLOAT) AS parking_score,
    CAST(units_score AS FLOAT) AS units_score,
    CAST(price_score AS FLOAT) AS price_score,
    CAST(rent_score AS FLOAT) AS rent_score,
    CAST(vintage_score AS FLOAT) AS vintage_score,
    CAST(reno_boost_score AS FLOAT) AS reno_boost_score,
    CAST(returns_score AS FLOAT) AS returns_score,
    CAST(strategy_score AS FLOAT) AS strategy_score,
    CAST(signal_strong_school_district AS FLOAT) AS signal_strong_school_district,
    CAST(signal_limited_new_supply AS FLOAT) AS signal_limited_new_supply,
    CAST(signal_forecasted_job_growth AS FLOAT) AS signal_forecasted_job_growth,
    CAST(signal_fortune_500_presence AS FLOAT) AS signal_fortune_500_presence,
    CAST(signal_near_metro_transit AS FLOAT) AS signal_near_metro_transit,
    CAST(signal_near_downtown_retail_hub AS FLOAT) AS signal_near_downtown_retail_hub,
    CAST(signal_close_to_highway_entrance AS FLOAT) AS signal_close_to_highway_entrance,
    CAST(signal_next_to_park AS FLOAT) AS signal_next_to_park,
    CAST(signal_no_institutional_mgmt AS FLOAT) AS signal_no_institutional_mgmt,
    CAST(signal_no_in_unit_laundry AS FLOAT) AS signal_no_in_unit_laundry,
    CAST(signal_negative_building AS FLOAT) AS signal_negative_building,
    CAST(signal_negative_neighborhood AS FLOAT) AS signal_negative_neighborhood,
    CAST(signal_views AS FLOAT) AS signal_views,
    CAST(signal_near_employer_or_bsu AS FLOAT) AS signal_near_employer_or_bsu,
    major_employer_name,
    CAST(notes_boost_score AS FLOAT) AS notes_boost_score,
    CAST(total_score AS FLOAT) AS total_score,
    negative_signal_evidence, notes_score_reason_text, score_breakdown, notes,
    source_mailbox, neighborhood_notes, assigned_schools, transportation_notes,
    qualitative_signal_evidence, qualitative_signal_source, notes_raw,
    score_missing_inputs, msa, sponsor,
    CAST(uid AS FLOAT) AS uid,
    asset_class,
    CAST(signal_mark_to_market_below_market_rents AS FLOAT) AS signal_mark_to_market_below_market_rents,
    property_site_url,
    property_site_text,
    signal_evidence
FROM dbo.apartment_deals;
"""

conn = _get_azure_conn()
cur = conn.cursor()
cur.execute(DDL)
conn.commit()
cur.execute(
    "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='apartment_deals_cleaned' AND COLUMN_NAME IN ('signal_near_employer_or_bsu','major_employer_name')"
)
print(cur.fetchall())
cur.execute(
    "SELECT deal_name, signal_near_employer_or_bsu, major_employer_name FROM dbo.apartment_deals_cleaned WHERE signal_near_employer_or_bsu = 1.0"
)
[print(r) for r in cur.fetchall()]
conn.close()
