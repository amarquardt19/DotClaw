import json
import re

PAT = re.compile(r"guarantor", re.I)
OUT = "data/guarantor_mentions.md"

lines = ["# Properties mentioning 'guarantor' in scraped text", "", "| Property | # mentions | Snippet |", "|---|---|---|"]
with open("data/property_site_text.jsonl") as f:
    for line in f:
        rec = json.loads(line)
        name = rec.get("deal_name", "?")
        count = 0
        snippet = ""
        for page in rec.get("pages", []):
            text = " ".join(page.get("text", "").split())
            for m in PAT.finditer(text):
                count += 1
                if not snippet:
                    s = max(0, m.start() - 100)
                    e = min(len(text), m.end() + 100)
                    snippet = text[s:e]
        if count:
            lines.append(f"| {name} | {count} | …{snippet}… |")

with open(OUT, "w") as f:
    f.write("\n".join(lines) + "\n")
print("wrote", OUT)
print("\n".join(lines))
