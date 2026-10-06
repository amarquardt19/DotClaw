"""Recompute units_score and total_score across dbo.apartment_deals using
the county-specific unit bands. total_score_new = total_score_old + (new_units - old_units)."""

import sys

sys.path.insert(0, "scripts")
from apartments_com_scraper import _get_azure_conn, _norm_text, _safe_int


def new_units_score(units, city, county):
    city_l = (city or "").lower()
    county_l = (county or "").lower()
    if units is None:
        return 0.0
    if county_l.startswith("san mateo"):
        return 2.0 if units <= 150 else (1.0 if units <= 250 else 0.0)
    if city_l == "san francisco" or county_l.startswith("san francisco"):
        return 2.0 if units <= 125 else (1.0 if units <= 225 else 0.0)
    if city_l in {"boise", "meridian", "eagle", "garden city", "kuna", "nampa"} or county_l.startswith("ada"):
        return 2.0 if units <= 200 else (1.0 if units < 300 else 0.0)
    if 100 <= units <= 300:
        return 2.0
    if 60 <= units <= 99 or 301 <= units <= 600:
        return 1.0
    return 0.0


conn = _get_azure_conn()
cur = conn.cursor()
cur.execute(
    "SELECT deal_name, city, county, units, units_score, score FROM dbo.apartment_deals"
)
rows = cur.fetchall()

changed = 0
for name, city, county, units, old_u, old_total in rows:
    units = _safe_int(units)
    old_u = float(old_u or 0.0)
    old_total = float(old_total or 0.0)
    new_u = new_units_score(units, _norm_text(city), _norm_text(county))
    if abs(new_u - old_u) < 1e-9:
        continue
    new_total = round(old_total + (new_u - old_u), 2)
    cur.execute(
        "UPDATE dbo.apartment_deals SET units_score=%s, score=%s WHERE deal_name=%s",
        (new_u, new_total, name),
    )
    if units is not None and 100 <= units <= 300:
        print(f"  changed: {name[:40]:40s} units={units} {old_u}->{new_u} total {old_total}->{new_total}")
    changed += 1

conn.commit()
print(f"done: {changed}/{len(rows)} rows updated")
cur.execute(
    "SELECT deal_name, city, county, units, units_score, score FROM dbo.apartment_deals WHERE deal_name LIKE '%888%'"
)
[print("verify:", r) for r in cur.fetchall()]
conn.close()
