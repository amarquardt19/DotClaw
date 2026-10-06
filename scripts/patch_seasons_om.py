"""Swap Seasons at Meridian's property_site_text for the broker OM narrative,
so the concept analysis isn't dominated by one property's full website.
Also refresh its markdown. Third-party OM text: not written into Azure jsonl."""

import sys
from datetime import datetime

sys.path.insert(0, "scripts")
import apartments_com_scraper as S

OM_TEXT_PATH = "/tmp/seasons_om.txt"

with open(OM_TEXT_PATH) as f:
    om_text = f.read().strip()

conn = S._get_azure_conn()
cur = conn.cursor()
cur.execute(
    "UPDATE dbo.apartment_deals SET property_site_url=%s, property_site_text=%s WHERE deal_name=%s",
    ("OM: CBRE Seasons at Meridian", om_text[:15000], "Seasons at Meridian Apartments"),
)
conn.commit()
print("azure rows updated:", cur.rowcount)
cur.execute(
    "SELECT deal_name, address FROM dbo.apartment_deals WHERE deal_name=%s",
    ("Seasons at Meridian Apartments",),
)
name, address = cur.fetchone()
conn.close()

import json
url = None
with open("data/property_site_text.jsonl") as f:
    for line in f:
        rec = json.loads(line)
        if rec.get("deal_name") == name:
            url = rec.get("apartments_url")
            break

record = {
    "deal_name": name,
    "address": address,
    "apartments_url": url,
    "property_site_url": "OM: CBRE Seasons at Meridian",
    "captured_at": datetime.utcnow().isoformat(),
    "pages": [{"page": "om-narrative", "url": "OM: CBRE Seasons at Meridian", "text": om_text}],
}
S._write_property_site_md(record)
print("wrote markdown for", name)
