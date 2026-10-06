import json
import re
from collections import defaultdict

SIGNALS = {
    "off-campus": r"off[- ]campus",
    "guarantor": r"guarantor",
    "by-the-bed": r"by the bed|per bed|individual lease|leased by the",
    "student-word": r"student housing|student(?:s)? (?:living|community|apartments)|for students\b",
    "bsu-adjacent": r"(?:steps|short walk|walking distance|across from|next to|blocks? from|min(?:ute)?s? (?:from|to|walk)) (?:boise state|bsu\b)",
    "summer-lease": r"summer (?:term )?lease|9[- ]month lease|academic (?:year )?lease",
    "furnished": r"fully furnished",
}
SIGNAL_RES = [(re.compile(v, re.I), k) for k, v in SIGNALS.items()]

results = []
with open("data/property_site_text.jsonl") as f:
    for line in f:
        rec = json.loads(line)
        name = rec.get("deal_name", "?")
        hits = defaultdict(list)
        for page in rec.get("pages", []):
            text = " ".join(page.get("text", "").split())
            for cre, k in SIGNAL_RES:
                for m in cre.finditer(text):
                    s = max(0, m.start() - 80)
                    e = min(len(text), m.end() + 80)
                    hits[k].append(text[s:e])
        score = 0
        score += 3 if hits["off-campus"] else 0
        score += 3 if hits["guarantor"] else 0
        score += 3 if hits["by-the-bed"] else 0
        score += 2 if hits["summer-lease"] else 0
        score += 2 if hits["bsu-adjacent"] else 0
        score += 1 if len(hits["student-word"]) > 1 else 0
        if score >= 2:
            results.append((score, name, dict(hits)))

results.sort(key=lambda r: -r[0])
print(f"{len(results)} properties with student-housing signals\n")
for score, name, hits in results:
    print(f"=== {name} (score {score}) ===")
    for k in ("off-campus", "guarantor", "by-the-bed", "summer-lease", "bsu-adjacent", "furnished"):
        if hits.get(k):
            print(f"  {k}: {hits[k][0][:180]}")
    print()
