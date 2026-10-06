import csv
import sys

sys.path.insert(0, "scripts")
from apartments_com_scraper import _get_azure_conn

CSV = "/Users/adammarq/Downloads/boise_properties_employer_and_bsu_distances.csv"

# score rule
def score(emp_mi, bsu_mi):
    near_emp = emp_mi is not None and emp_mi < 2.5
    near_bsu = bsu_mi is not None and bsu_mi <= 1.0
    if near_emp and near_bsu:
        return 1.0
    if near_emp:
        return 0.75
    if near_bsu:
        return 0.5
    return 0.0


with open(CSV, newline="", encoding="utf-8-sig") as f:
    rows = [r for r in csv.DictReader(f) if r.get("property_name")]

updates = []
for r in rows:
    try:
        emp_mi = float(r["distance_to_nearest_employer_mi"]) if r["distance_to_nearest_employer_mi"] else None
        bsu_mi = float(r["distance_to_boise_state_mi"]) if r["distance_to_boise_state_mi"] else None
    except ValueError:
        continue
    s = score(emp_mi, bsu_mi)
    near_emp = emp_mi is not None and emp_mi < 2.5
    near_bsu = bsu_mi is not None and bsu_mi <= 1.0
    if near_emp and near_bsu:
        label = f"{r['nearest_employer_excluding_boise_state']} + Boise State"
    elif near_emp:
        label = r["nearest_employer_excluding_boise_state"]
    elif near_bsu:
        label = "Boise State"
    else:
        label = ""
    updates.append((s, label, r["address"]))

conn = _get_azure_conn()
cur = conn.cursor()

for stmt in (
    "ALTER TABLE dbo.apartment_deals ADD signal_near_employer_or_bsu FLOAT NULL",
    "ALTER TABLE dbo.apartment_deals ADD major_employer_name NVARCHAR(200) NULL",
):
    try:
        cur.execute(stmt)
        print("added:", stmt.split("ADD ")[1].split(" ")[0])
    except Exception as e:
        print("skip (exists?):", e)

ok = miss = 0
for s, label, addr in updates:
    cur.execute(
        "UPDATE dbo.apartment_deals SET signal_near_employer_or_bsu=%s, major_employer_name=%s WHERE address=%s",
        (s, label or None, addr),
    )
    if cur.rowcount:
        ok += 1
    else:
        miss += 1
        print("  no match:", addr)
conn.commit()

cur.execute("""
SELECT deal_name, signal_near_employer_or_bsu, major_employer_name
FROM dbo.apartment_deals WHERE signal_near_employer_or_bsu IS NOT NULL
ORDER BY signal_near_employer_or_bsu DESC, deal_name""")
print(f"\nupdated {ok}, missed {miss}\n")
for name, s, label in cur.fetchall():
    print(f"  {s}  {name[:45]:45s}  {label}")
conn.close()
