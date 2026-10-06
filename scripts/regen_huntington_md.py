import sys
from datetime import datetime

sys.path.insert(0, "scripts")
import apartments_com_scraper as S

conn = S._get_azure_conn()
cur = conn.cursor()
cur.execute(
    "SELECT deal_name, address, property_site_url, property_site_text FROM dbo.apartment_deals WHERE deal_name=%s",
    ("Huntington",),
)
name, address, url, text = cur.fetchone()
conn.close()

record = {
    "deal_name": name,
    "address": address,
    "apartments_url": "https://www.apartments.com/huntington-boise-id/gvy35ex/",
    "property_site_url": url,
    "captured_at": datetime.utcnow().isoformat(),
    "pages": [{"page": "all", "url": url, "text": text}],
}
S._write_property_site_md(record)
print("wrote markdown for", name)
