import json

PATH = "data/property_site_text.jsonl"
with open("/tmp/seasons_om.txt") as f:
    om_text = f.read().strip()

recs = []
with open(PATH) as f:
    for line in f:
        rec = json.loads(line)
        if rec.get("deal_name") == "Seasons at Meridian Apartments":
            rec["property_site_url"] = "OM: CBRE Seasons at Meridian"
            rec["pages"] = [{"page": "om-narrative", "url": "OM: CBRE Seasons at Meridian", "text": om_text}]
            rec["text_len"] = len(om_text)
        recs.append(rec)

with open(PATH, "w") as f:
    for rec in recs:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("patched", len(recs), "records")
