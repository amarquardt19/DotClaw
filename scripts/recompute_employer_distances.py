"""Recompute employer/BSU distances as DRIVING miles (OSRM) + minutes, geocoding
property addresses via Nominatim. Writes the Downloads CSV and updates
signal_near_employer_or_bsu / major_employer_name / total score in Azure."""

import csv
import json
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "scripts")
from apartments_com_scraper import _get_azure_conn

GEOCODE_CACHE = "data/geocode_cache.json"
OUT_CSV = "/Users/adammarq/Downloads/boise_properties_employer_and_bsu_distances.csv"

EMPLOYERS = {
    "Scentsy HQ": "2901 E Lanark St, Meridian, ID 83642",
    "Saint Alphonsus": "1055 N Curtis Rd, Boise, ID 83706",
    "WinCo Corporate": "650 N Armstrong Pl, Boise, ID 83704",
    "Micron": "8000 S Federal Way, Boise, ID 83716",
    "St. Luke's Meridian": "520 S Eagle Rd, Meridian, ID 83642",
    "Boise State": "1910 University Dr, Boise, ID 83725",
}

try:
    with open(GEOCODE_CACHE) as f:
        CACHE = json.load(f)
except OSError:
    CACHE = {}


import subprocess


def _http_json(url):
    out = subprocess.run(
        ["curl", "-sS", "--max-time", "30", "-A", "deal-pipeline-distance-recompute/1.0", url],
        capture_output=True, timeout=45, check=True,
    )
    return json.loads(out.stdout)


def geocode(addr):
    if addr in CACHE:
        return CACHE[addr]
    q = urllib.parse.urlencode(
        {"q": addr, "format": "json", "limit": 1, "countrycodes": "us"}
    )
    data = _http_json(f"https://nominatim.openstreetmap.org/search?{q}")
    time.sleep(1.1)
    if not data:
        CACHE[addr] = None
        return None
    loc = (float(data[0]["lat"]), float(data[0]["lon"]))
    CACHE[addr] = loc
    return loc


def osrm_drive(a, b):
    """a,b = (lat,lon). Returns (miles, minutes, snap_a, snap_b) snapped to network."""
    if a is None or b is None:
        return None, None
    # drop sub-meter jitter so identical trips reuse the cache
    a = (round(a[0], 4), round(a[1], 4))
    b = (round(b[0], 4), round(b[1], 4))
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{a[1]},{a[0]};{b[1]},{b[0]}?overview=false&steps=false"
    )
    data = _http_json(url)
    if data.get("code") != "Ok" or not data.get("routes"):
        return None, None
    route = data["routes"][0]
    return route["distance"] / 1609.34, route["duration"] / 60.0


# 1) load properties from Azure
conn = _get_azure_conn()
cur = conn.cursor()
cur.execute("""
SELECT deal_name, address, signal_near_employer_or_bsu, score
FROM dbo.apartment_deals
WHERE state='ID'
ORDER BY deal_name""")
props = [(n, a, float(s or 0), float(t or 0)) for n, a, s, t in cur.fetchall()]
print(f"{len(props)} ID properties")

# 2) geocode employers
emp_loc = {}
for name, addr in EMPLOYERS.items():
    loc = geocode(addr)
    print(f"  {name}: {loc}")
    if loc:
        emp_loc[name] = loc

# 3) geocode + route each property
rows = []
updated = 0
for i, (name, addr, old_signal, old_total) in enumerate(props, 1):
    loc = geocode(addr)
    if not loc:
        print(f"[{i}] GEOCODE MISS {name} :: {addr}")
        continue
    best_name, best_mi, best_min = None, None, None
    bsu_mi = None
    for emp, eloc in emp_loc.items():
        mi, mins = osrm_drive(loc, eloc)
        time.sleep(0.25)
        if mi is None:
            continue
        if emp == "Boise State":
            bsu_mi = mi
        elif best_mi is None or mi < best_mi:
            best_name, best_mi, best_min = emp, mi, mins
    rows.append((name, addr, best_name, best_mi, best_min, bsu_mi))
    print(f"[{i}] {name[:40]:40s} -> {best_name}: {best_mi:.1f} mi / BSU: {bsu_mi:.1f} mi" if bsu_mi is not None else f"[{i}] {name} no route")

    # scoring rule: <2.5mi employer, <=1mi BSU
    near_emp = best_mi is not None and best_mi < 2.5
    near_bsu = bsu_mi is not None and bsu_mi <= 1.0
    if near_emp and near_bsu:
        s, label = 1.0, f"{best_name} + Boise State"
    elif near_emp:
        s, label = 0.75, best_name
    elif near_bsu:
        s, label = 0.5, "Boise State"
    else:
        s, label = 0.0, None

    if abs(s - old_signal) > 1e-9:
        new_total = round(old_total + (s - old_signal), 2)
        cur.execute(
            "UPDATE dbo.apartment_deals SET signal_near_employer_or_bsu=%s, major_employer_name=%s, score=%s WHERE deal_name=%s",
            (s, label, new_total, name),
        )
        print(f"    DB updated: signal {old_signal}->{s}, score {old_total}->{new_total}")
        updated += 1
    else:
        cur.execute(
            "UPDATE dbo.apartment_deals SET major_employer_name=%s WHERE deal_name=%s",
            (label, name),
        )

conn.commit()
conn.close()

with open(GEOCODE_CACHE, "w") as f:
    json.dump(CACHE, f, indent=1)

with open(OUT_CSV, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["property_name", "address", "nearest_employer_excluding_boise_state",
                "driving_distance_to_nearest_employer_mi", "driving_minutes_to_nearest_employer",
                "driving_distance_to_boise_state_mi"])
    for name, addr, emp, mi, mins, bsu in rows:
        w.writerow([name, addr, emp or "", f"{mi:.2f}" if mi is not None else "",
                    f"{mins:.0f}" if mins is not None else "", f"{bsu:.2f}" if bsu is not None else ""])

print(f"\n{updated} signal changes; CSV -> {OUT_CSV}")
