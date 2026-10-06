"""Concept-level TF-IDF analysis over scraped Boise property-site text.

Concepts =
  1. capitalized multiword entities ("Boise River Greenbelt", "Micron Boise")
  2. lowercase attribute phrases that carry analytical meaning
     ("near downtown", "close to the freeway", "in-unit laundry")

Floorplan-section pages are precluded from analysis.

Outputs:
  data/concepts_df.md     - market-wide leitmotifs ranked by document frequency
  data/concepts_tfidf.md  - per-property distinctive concepts (TF-IDF)
"""

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
JSONL_PATH = DATA_DIR / "property_site_text.jsonl"
OUT_DF = DATA_DIR / "concepts_df.md"
OUT_TFIDF = DATA_DIR / "concepts_tfidf.md"

PRECLUDED_PAGES = re.compile(r"floorplan|floor-plan|floor_plans|availability|pricing", re.I)

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "at", "with",
    "your", "our", "our", "you", "we", "is", "are", "new", "home", "boise",
    "idaho", "id", "start", "view", "floor", "plan", "plans", "bed", "bath",
    "rent", "apartments", "apartment", "community", "welcome", "contact",
    "us", "apply", "schedule", "tour", "gallery", "amenities", "available",
    "availability", "call", "coming", "soon", "sq", "ft", "month", "de",
    "see", "find", "show", "more", "read", "less", "close", "clear", "now",
    "today", "back", "list", "details", "hours", "street", "west", "east",
    "north", "south", "welcome", "living", "living", "style", "modern",
}

JUNK_PATTERNS = [
    r"Skip to main content", r"[Cc]ookie\S*", r"Map [Dd]ata \S*[©\w]* ?\S*", r"Keyboard shortcuts",
    r"Terms", r"[Zz]oom", r"[Vv]iew on [Mm]ap", r"Pet Policy", r"Office [Hh]ours",
    r"I Agree", r"I Accept All\S*", r"I Reject All\S*", r"[Cc]ookie [Pp]references\S*",
    r"Floorplan layout\S*", r"Base Rent", r"Move [Ii]n [Dd]ate", r"Filters?\b",
    r"Walkscore for \w+", r"Walk [Ss]core", r"Bike [Ss]core", r"Walkscore",
    r"OpenStreetMap\S*", r"OpenFreeMap\S*", r"Google", r"[←→↑↓]", r"Jump \w+[^ ]*",
    r"[Ll]oad [Aa]ll", r"Privacy Policy\S*", r"How We Use Your Data\S*",
    r"All [Rr]ights [Rr]eserved\S*", r"[Ww]eb [Dd]esign\S*", r"Contact Method Popup\S*",
    r"Accessibility Screen-?Reader\S* Guide\S*", r"Click to toggle[^.]*", r"Report a map error\S*",
    r"AudioEye\S*", r"SuMoTuWeThFrSa\S*", r"BESbswy\S*", r"KeyAction\S*",
    r"Reset [Ff]ilters\S*", r"Toggle between metric[^.]*", r"[A-Z][a-z]+jump \w+",
]

# Words/phrases that pass the capitalized-run regex but carry no analytical meaning.
NOISE_CONCEPTS = {
    "this", "that", "these", "they", "there", "when", "whether", "what", "here",
    "just", "every", "any", "only", "once", "some", "one", "please", "thank",
    "explore", "enjoy", "experience", "discover", "imagine", "enjoy",
    "take", "move", "click", "book", "browse", "visit", "choose", "check",
    "share", "spend", "walk", "live", "come", "go", "open", "use", "feel",
    "see", "find", "enter", "take", "now", "today", "day", "days", "read",
    "applicants", "oops", "escape", "nestled", "surrounded", "unparalleled",
    "beautiful", "spacious", "luxury", "convenient", "located", "designed",
    "neighborhood", "center", "park", "pool", "private", "reserved", "location",
    "great", "love", "half", "there's", "yours", "join", "kickstart", "services",
    "staff", "updated", "brand", "good", "area", "kitchen", "sinks", "cable",
    "magazine", "ranked", "named", "combining", "old", "pair", "s paradise",
    "fulfilled", "story", "minute", "minutes", "bathrooms any", "bedrooms any",
    "bed", "bath", "walk", "done", "clear", "start", "local", "view units",
    "square feet", "sq ft", "dryer", "washer", "floor plans",
    "bedroom", "bathrooms", "de", "square", "road", "roadboise", "blvd",
    "boulevard", "street", "st", "ave", "rd", "city", "state", "country",
    "from", "with", "for", "each", "more", "sign in", "tap", "swipe",
    "schedule", "apply", "pricing", "availability", "amenities", "features",
    "portfolio: skip", "unavailable |", "new window", "privacy preferences",
    "cookies", "feedback",
}

NOISE_CONCEPT_SUBSTRINGS = [
    "jump", "zoom", "cookie", "policy", "preferences", "rights reserved",
    "web design", "openmaptiles", "hours", "pm ", "am-", "asked questions",
    "popup", "shortcut", "keyboard", "scroll", "audioeye", "toggle", "drag",
    "page up", "page down", "mon-fri", "previous", "move left", "move right",
    "home ", "day", "week", "month", "sunday", "monday", "friday", "saturday",
]

# Capitalized multiword run: "Boise River Greenbelt", "Bogus Basin", "The Village at Meridian"
CONCEPT_RE = re.compile(
    r"\b((?:[A-Z0-9&'][A-Za-z0-9&'\-]*[a-z]|[A-Z][A-Za-z0-9&'\-]*)(?:\s+(?:"
    r"[A-Z][A-Za-z0-9&'\-]*|at|of|the|de|and|on"
    r")(?!,|\.))*\b)",
)


def clean_page_text(text):
    for pat in JUNK_PATTERNS:
        text = re.sub(pat, " ", text, flags=re.I)
    return text


# Lowercase attribute phrases that carry analytical meaning. Each maps to a
# canonical concept label; matched case-insensitively against page text.
ATTRIBUTE_PATTERNS = [
    (r"near downtown|downtown boise|close to downtown|walking distance to downtown|minutes? from downtown|downtown core|heart of (?:the )?(?:city|downtown)", "downtown proximity"),
    (r"close to the (?:freeway|highway)|easy (?:freeway|highway) access|convenient (?:freeway|highway)|easy access to (?:the )?(?:i-?\d+|freeway|highway)|minutes? from the (?:freeway|highway)", "highway access"),
    (r"in[- ]unit (?:laundry|washer)|washer (?:and|&) dryer in (?:each|every)|washer/dryer (?:included|in (?:each|every) (?:home|unit|apartment))", "in-unit laundry"),
    (r"pet[- ]friendly|dog park|pet (?:spa|detailing|centric)", "pet-friendly"),
    (r"ev (?:charging|charger)|electric vehicle charg", "EV charging"),
    (r"smart home|dwelo", "smart home"),
    (r"(?:rooftop|roof ?deck|skyline|panoramic|city|mountain|foothill|river)s? view", "views"),
    (r"coworking|co[- ]working|remote work|office space", "coworking"),
    (r"24[- ]?hour (?:fitness|gym)", "24h gym"),
    (r"resort[- ]style pool|heated pool|year[- ]round (?:pool|spa)", "resort/heated pool"),
    (r"(?:covered|garage|private) parking|attached garage|reserved (?:spot|parking)", "parking (covered/garage)"),
    (r"(?:on|near|access to|adjacent to) (?:the )?(?:boise river )?greenbelt", "greenbelt access"),
    (r"walkscore|walk score|walker'?s paradise|walkable", "walkability"),
    (r"newly renovated|recently renovated|renovated (?:in|units)|2019[-–]202\d renovated", "renovated"),
    (r"smoke[- ]free", "smoke-free"),
    (r"controlled access|gated", "controlled access"),
    (r"furnished|corporate housing", "furnished/corporate"),
    (r"affordable|income[- ]restricted|affordable program", "affordable/income-restricted"),
    (r"\bmicron\b", "Micron (employer)"),
    (r"\bi-?84\b", "I-84"),
    (r"bogu?s basin", "Bogus Basin"),
    (r"boise state|bsu\b", "Boise State"),
    (r"\briver\b", "river mention"),
    (r"\bfoothills?\b", "foothills"),
]
ATTRIBUTE_RES = [(re.compile(p, re.I), label) for p, label in ATTRIBUTE_PATTERNS]


def extract_concepts(text):
    text = clean_page_text(text)
    concepts = []
    for cre, label in ATTRIBUTE_RES:
        if cre.search(text):
            concepts.append(label)
    for m in CONCEPT_RE.finditer(text):
        raw = " ".join(m.group(0).split())
        drop_props = re.sub(r"'s\b", "", raw)
        words = drop_props.split()
        if len(words) == 1 and len(words[0]) < 4:
            continue
        lowered = [re.sub(r"['’]s$", "", w.lower()) for w in words]
        # strip leading/trailing stopwords (The X, X and Y middle "the"/"of"/"at" kept)
        while lowered and lowered[0] in STOPWORDS:
            lowered.pop(0)
        while lowered and lowered[-1] in STOPWORDS:
            lowered.pop()
        if not lowered or len(lowered) > 6:
            continue
        if all(w in STOPWORDS for w in lowered):
            continue
        concept = " ".join(lowered)
        if concept in NOISE_CONCEPTS:
            continue
        if any(sub in concept for sub in NOISE_CONCEPT_SUBSTRINGS):
            continue
        concepts.append(concept)
    return concepts


def name_words(deal_name):
    """Normalized word set of the property name, for self-name filtering."""
    name = deal_name.split(",")[0]
    name = re.sub(r"\d+.*", "", name)
    return {w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 2}


def load_docs():
    docs = {}  # deal_name -> list of concepts
    with open(JSONL_PATH) as f:
        for line in f:
            rec = json.loads(line)
            name = rec.get("deal_name")
            if not name or "boise" not in name.lower():
                continue
            concepts = []
            for page in rec.get("pages", []):
                page_name = str(page.get("page", "")).lower()
                if PRECLUDED_PAGES.search(page_name):
                    continue
                page_text = page.get("text", "")
                page_concepts = extract_concepts(page_text)
                own = name_words(name)
                page_concepts = [c for c in page_concepts if not own.intersection(c.split())]
                concepts.extend(page_concepts)
            docs[name] = concepts
    return docs


def main():
    docs = load_docs()
    print(f"{len(docs)} properties loaded")

    doc_freq = Counter()
    term_freq = {}
    for name, concepts in docs.items():
        tf = Counter(concepts)
        term_freq[name] = tf
        for c in set(concepts):
            doc_freq[c] += 1

    n_docs = len(docs)

    # --- DF ranking: concepts appearing at >= 3 properties ---
    df_lines = ["# Boise property-site concept analysis — document frequency",
                "",
                f"Across {n_docs} properties (floorplan sections excluded).",
                "",
                "| Concept | # Properties | Count |",
                "|---|---|---|"]
    for concept, df_cnt in sorted(doc_freq.items(), key=lambda kv: (-kv[1], kv[0])):
        if df_cnt < 3:
            break
        total = sum(term_freq[n][concept] for n in docs)
        df_lines.append(f"| {concept} | {df_cnt} | {total} |")
    OUT_DF.write_text("\n".join(df_lines) + "\n")
    print(f"wrote {OUT_DF}")

    # --- TF-IDF per property: top distinctive concepts ---
    tfidf_lines = ["# Boise property-site concept analysis — distinctive (TF-IDF)",
                   "",
                   "Concepts that disproportionately appear at this property vs. the market.",
                   ""]
    for name in sorted(docs):
        tf = term_freq[name]
        if not tf:
            continue
        scores = {}
        for concept, cnt in tf.items():
            df_cnt = doc_freq[concept]
            idf = math.log((n_docs + 1) / (df_cnt + 1)) + 1
            scores[concept] = (1 + math.log(cnt)) * idf
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:10]
        tfidf_lines.append(f"**{name}**: " + ", ".join(f'"{c}" ({term_freq[name][c]}x)' for c, _ in top))
        tfidf_lines.append("")
    OUT_TFIDF.write_text("\n".join(tfidf_lines) + "\n")
    print(f"wrote {OUT_TFIDF}")


if __name__ == "__main__":
    main()
