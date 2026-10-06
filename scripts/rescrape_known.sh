#!/bin/zsh
# Re-scrapes only the known/identified properties (URL whitelist pulled from Azure).
# Run me:  zsh scripts/rescrape_known.sh
cd "$(dirname "$0")/.."

python3 - <<'EOF'
import sys
sys.path.insert(0, 'scripts')
import apartments_com_scraper as S
conn = S._get_azure_conn(); cur = conn.cursor()
cur.execute("SELECT deal_name, source_mailbox FROM dbo.apartment_deals ORDER BY uid")
rows = cur.fetchall(); conn.close()
names = """Vasara,Lume,The Hayden,Woodland Park,Bridgepointe,CitySouth,De Sabla Apartments,Highwater,Indigo,707 Leahy,Pacifica Park,Laurel Crossing Apartment Homes,Franklin 299,Skyline Heights Apartments,eaves Foster City,Peninsula Pines Apartments South,Bay Terrace,Springline,The Triton,Artisan Crossing,Two50 Downtown,Franklin Street Apartment Homes,Park Place at San Mateo,Cirrus Redwood City,Harbor Cove,RYAN TOWER,Trestle,Waters Edge,Fairmont Apartments,West 5th,Hillsdale Garden,2727 Edison,Beach Cove,Courtyard Apartments,El Camino Real,Altitude Apartments South,Shadow Cove,One Three Four Elm Apartments,The Heltsley,Clubview Apartment Homes,Marymount Manor,201 Marshall,Skyline View,Bell South City South,Station Park Green,Lakeshore Landing,Fosters Landing,Huxley,One Hundred Grand,Lantern Cove,ROEN Menlo Park,Chesapeake Point,The Lagoons,The Bower,The Villas at Bair Island,888 San Mateo Apartments,Northpark,Stanford Garden Apartments,Burlingame Towers,Evergreen Ridge Apartments,Oakview,Skycrest Apartments,Crystal Springs Terrace,Marymount Tower,Amberwood Apartments,Stone Creek Apartments,Aperture,The Lark,Seacliff,Redwood Plaza Village""".split(',')
db = {(n or '').strip().lower(): u for n, u in rows if u and 'apartments.com' in str(u)}
found, missing = [], []
for n in names:
    u = db.get(n.strip().lower())
    (found.append(u) if u else missing.append(n))
with open('/tmp/rescrape_urls.txt', 'w') as fh:
    fh.write("\n".join(found) + "\n")
print(f"URLs: {len(found)} | missing (no URL in Azure): {len(missing)} -> {missing}")
EOF

python3 scripts/apartments_com_scraper.py --no-playwright \
  --urls-file /tmp/rescrape_urls.txt \
  --workbook "$HOME/Downloads/Dealsifta Apartments Resaved.xlsx" \
  --sheet "XL dealsifta (2)" \
  --skip 0 --max 100
