import json
import re

PAT = re.compile(r"boise state|bsu\b", re.I)
OUT = "data/bsu_mentions.md"

lines = ["# Boise State / BSU mentions across Boise+Meridian property corpus", ""]
seen_lines = 0
with open("data/property_site_text.jsonl") as f:
    for line in f:
        rec = json.loads(line)
        name = rec.get("deal_name", "?")
        for page in rec.get("pages", []):
            text = " ".join(page.get("text", "").split())
            for m in PAT.finditer(text):
                start = max(0, m.start() - 220)
                end = min(len(text), m.end() + 220)
                snippet = text[start:end]
                # trim to nearest word boundaries
                lines.append(f"**{name}** ({page.get('page')}):")
                lines.append(f"> …{snippet}…")
                lines.append("")
                seen_lines += 1

with open(OUT, "w") as f:
    f.write("\n".join(lines))
print(f"{seen_lines} mentions -> {OUT}")
