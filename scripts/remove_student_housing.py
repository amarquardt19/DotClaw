import json
import os
import sys

sys.path.insert(0, "scripts")
from apartments_com_scraper import _get_azure_conn

REMOVE = {"Iconic Village,Boise, ID", "LOCAL Boise,Boise, ID"}

conn = _get_azure_conn()
cur = conn.cursor()
for name in REMOVE:
    cur.execute("DELETE FROM dbo.apartment_deals WHERE deal_name=%s", (name,))
    print(f"deleted {cur.rowcount} row: {name}")
conn.commit()
conn.close()

JSONL = "data/property_site_text.jsonl"
with open(JSONL) as f:
    recs = [json.loads(l) for l in f if l.strip()]
kept = [r for r in recs if r.get("deal_name") not in REMOVE]
with open(JSONL, "w") as f:
    for r in kept:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"jsonl: {len(recs)} -> {len(kept)}")

for fn in os.listdir("data/property_sites"):
    if fn.startswith(("iconic_village", "local_boise")):
        path = os.path.join("data/property_sites", fn)
        os.remove(path)
        print("removed", path)

parts = []
for fn in sorted(os.listdir("data/property_sites")):
    if fn.endswith(".md"):
        with open(os.path.join("data/property_sites", fn)) as f:
            parts.append(f.read())
with open("data/property_sites_all_boise.md", "w") as f:
    f.write("\n".join(parts))
print("rebuilt property_sites_all_boise.md from", len(parts), "md files")
