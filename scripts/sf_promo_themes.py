#!/usr/bin/env python3
"""SF promo-theme explorer: scrape SF apartment listings and analyze what
each neighborhood's marketing copy promotes most.

Two modes:
  scrape   - crawl apartments.com search URL, capture About/Neighborhood text
             per property into data/sf_promo_texts.jsonl
  analyze  - read the JSONL (no scraping) and print/CSV theme stats per
             neighborhood. Run this freely after scraping to iterate on themes.

Usage:
  python3 scripts/sf_promo_themes.py scrape --cdp-url http://localhost:9222 --max 25
  python3 scripts/sf_promo_themes.py analyze
"""
import argparse
import csv
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apartments_com_scraper as S

DEFAULT_URL = "https://www.apartments.com/apartments/san-francisco-ca/?bb=4torppwm0Okrzxg6D"
TEXTS_PATH = Path(__file__).resolve().parent.parent / "data" / "sf_promo_texts.jsonl"
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "sf_promo_themes.csv"
NEG_CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "sf_negative_evidence.csv"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sf_promo.db"
EXCLUDED_NEIGHBORHOODS = {"tenderloin"}

PROPS_TABLE = "sf_promo_properties"
THEMES_TABLE = "sf_promo_themes"


def _get_db():
    import sqlite3
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"""
CREATE TABLE IF NOT EXISTS {PROPS_TABLE} (
    property_id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    name TEXT,
    units INTEGER,
    neighborhood TEXT,
    about TEXT,
    neighborhood_text TEXT,
    negative_building INTEGER,
    negative_neighborhood INTEGER,
    negative_building_evidence TEXT,
    negative_neighborhood_evidence TEXT,
    full_text TEXT,
    scraped_at TEXT DEFAULT (datetime('now'))
)
""")
    try:
        conn.execute(f"ALTER TABLE {PROPS_TABLE} ADD COLUMN units INTEGER")
    except Exception:
        pass
    conn.execute(f"""
CREATE TABLE IF NOT EXISTS {THEMES_TABLE} (
    theme_id INTEGER PRIMARY KEY AUTOINCREMENT,
    neighborhood TEXT,
    property_count INTEGER,
    theme TEXT,
    mention_count INTEGER,
    pct INTEGER,
    analyzed_at TEXT DEFAULT (datetime('now'))
)
""")
    conn.commit()
    return conn


def _upsert_property(conn, record) -> None:
    conn.execute(f"""
INSERT INTO {PROPS_TABLE} (url, name, units, neighborhood, about, neighborhood_text,
    negative_building, negative_neighborhood,
    negative_building_evidence, negative_neighborhood_evidence, full_text, scraped_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
ON CONFLICT(url) DO UPDATE SET
    name=excluded.name, units=excluded.units, neighborhood=excluded.neighborhood, about=excluded.about,
    neighborhood_text=excluded.neighborhood_text,
    negative_building=excluded.negative_building, negative_neighborhood=excluded.negative_neighborhood,
    negative_building_evidence=excluded.negative_building_evidence,
    negative_neighborhood_evidence=excluded.negative_neighborhood_evidence,
    full_text=excluded.full_text, scraped_at=excluded.scraped_at
""", (
        record["url"], record["name"], record.get("units"), record["neighborhood"], record["about"], record["neighborhood_text"],
        int(bool(record["negative_building"])), int(bool(record["negative_neighborhood"])),
        record["negative_building_evidence"], record["negative_neighborhood_evidence"],
        record["full_text"],
    ))
    conn.commit()


def _load_records():
    conn = _get_db()
    conn.row_factory = __import__("sqlite3").Row
    cur = conn.execute(f"SELECT url, name, units, neighborhood, about, neighborhood_text, negative_building, negative_neighborhood, negative_building_evidence, negative_neighborhood_evidence, full_text FROM {PROPS_TABLE}")
    records = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not records:
        raise RuntimeError("no rows in sqlite db")
    return records

THEMES = {
    "bay/water views": r"bay view|water view|views? of the (?:bay|water|ocean)|panoramic view|skyline view|city views?",
    "rooftop deck": r"rooftop|roof deck|sky (?:deck|lounge|terrace)",
    "fitness/gym": r"fitness (?:center|studio)|gym|24[- ]hour fitness|yoga",
    "pool/spa": r"\bpool\b|\bspa\b|hot tub|sauna",
    "pet friendly": r"pet[- ]friendly|dog park|pet spa|pet run|cats? (?:and|&) dogs?|pets? welcome",
    "in-unit laundry": r"washer(?:/| and | & )dryer|in[- ]unit (?:laundry|washer)|laundry in (?:unit|home)",
    "coworking/wfh": r"co[- ]?working|work[- ]from[- ]home|business center|conference room|wfh",
    "EV charging": r"ev charging|electric vehicle|tesla charging",
    "parking": r"parking (?:garage|available|on[- ]site)|garage parking|reserved parking|underground parking",
    "doorman/concierge": r"doorman|concierge|24[- ]hour (?:lobby|attendant)|package (?:room|service|concierge)",
    "transit (BART/Muni)": r"\bbart\b|\bmuni\b|caltrain|ferry|transit[- ]friendly|steps (?:to|from) (?:the )?(?:bart|muni)",
    "walkability": r"walk score|walkable|steps (?:to|from)|walk to|pedestrian",
    "dining/nightlife": r"restaurants?|dining|nightlife|bars? and|cafes?|eateries|foodie",
    "parks/outdoors": r"\bparks?\b|green space|trail|waterfront|embarcadero|golden gate park|dolores park",
    "luxury/high-end": r"luxury|luxurious|high[- ]end|upscale|premium finishes|designer|quartz|stainless steel",
    "renovated/modern": r"renovated|remodeled|newly (?:built|constructed)|modern|contemporary|brand new",
    "historic/character": r"historic|vintage|classic (?:san francisco|edwardian|victorian)|character|charm",
    "tech/shuttle": r"tech shuttle|shuttle (?:stop|service)|silicon valley|commuter",
    "storage": r"storage (?:unit|space|available)|extra storage|bike storage",
    "furnished/flexible": r"furnished|short[- ]term lease|flexible lease|corporate",
    "security/gated": r"gated|controlled access|security|video intercom|keyless",
    "community/events": r"resident events?|community (?:events?|room)|clubhouse|social (?:lounge|events?)",
    "air conditioning": r"air condition|a/c|central air",
    "affordability/income": r"affordable housing|income (?:restricted|limits)|below market rate|\bbmr\b",
}

STOPWORDS = set("""
a an and are as at be by for from has have in is it its of on or that the to with
you your we our this these those will can all also more new one two just like
san francisco apartment apartments home homes living live located offers offer
featuring features feature enjoy residents resident property community amenities
""".split())


def _extract_neighborhood_name(raw_text: str, neighborhood_section: str) -> str:
    for src in (raw_text, neighborhood_section):
        src = src or ""
        m = re.search(r"about living in ([A-Z][\w' .&-]{2,40}?)\s*(?:\n|Average Prices)", src)
        if m:
            return m.group(1).strip()
        m = re.search(r"base rent averages by bedroom\.?\s*\n?([A-Z][\w' .&-]{2,40}?)\s+San Francisco", src)
        if m:
            return m.group(1).strip()
        m = re.search(r"^([A-Z][\w' .&-]{2,40}?) is a neighborhood", src, re.M)
        if m:
            return m.group(1).strip()
        m = re.search(r"neighborhood of ([A-Z][\w' .&-]{2,40}?)[,.]", src)
        if m:
            return m.group(1).strip()
    return "Unknown"


def scrape(args) -> None:
    from playwright.sync_api import sync_playwright

    TEXTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        if args.cdp_url:
            browser = p.chromium.connect_over_cdp(args.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1600, "height": 1200})
        else:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context(viewport={"width": 1600, "height": 1200})
        page = context.new_page()
        page.goto(args.url, timeout=60000, wait_until="domcontentloaded")
        time.sleep(8)

        listings, seen = [], set()
        target = args.skip + args.max
        page_num = 1
        while len(listings) < target and page_num <= 10:
            if page_num > 1:
                base_url = re.sub(r"/\d+/?(?=\?)", "", args.url)
                paged_url = re.sub(r"/?\?", f"/{page_num}/?", base_url, count=1)
                print(f"search page {page_num}: {paged_url}", flush=True)
                page.goto(paged_url, timeout=60000, wait_until="domcontentloaded")
                time.sleep(3.0)
            before = len(listings)
            for item in S._collect_listing_cards(page):
                u = item.get("url") or ""
                if S._is_property_url(u) and u not in seen:
                    seen.add(u)
                    listings.append(item)
            if len(listings) == before:
                break
            page_num += 1
        sliced = listings[args.skip:args.skip + args.max]
        print(f"discovered {len(listings)} listings, processing {len(sliced)}", flush=True)

        sql_conn = None
        try:
            sql_conn = _get_db()
            print(f"SQLite upsert enabled: {DB_PATH} ({PROPS_TABLE})", flush=True)
        except Exception as exc:
            print(f"SQLite disabled: {exc}", flush=True)

        with TEXTS_PATH.open("a") as out:
            for idx, listing in enumerate(sliced, start=1):
                url = listing.get("url")
                if not url:
                    continue
                print(f"[{idx}/{len(sliced)}] {url}", flush=True)
                if idx > 1:
                    time.sleep(random.uniform(args.sleep_min, args.sleep_max))
                detail_page = None
                try:
                    detail_page = context.new_page()
                    detail = S._scrape_detail(detail_page, url)
                    raw_text = str(detail.get("raw_text") or "")
                    if len(raw_text) < 500:
                        print(f"    BLOCKED/thin page ({len(raw_text)}ch) - bot wall, stopping. Resume with --skip {args.skip + idx}", flush=True)
                        break
                    title = S._extract_title_from_raw_text(raw_text, url)
                    units = S._extract_units(raw_text)
                    if args.min_units and units is not None and units < args.min_units:
                        print(f"    skip <{args.min_units} units ({units}): {title}", flush=True)
                        detail_page.close()
                        detail_page = None
                        continue
                    neighborhood_section = S._extract_text_section(
                        raw_text, "Neighborhood",
                        ["Average Prices by Area", "Education", "Schools", "Transportation", "Points of Interest", "Reviews"],
                    )
                    about_section = ""
                    for label in (f"About {title}", "About This Property"):
                        about_section = S._extract_text_section(
                            raw_text, label,
                            ["Average Prices by Area", "Neighborhood", "Education", "Schools", "Transportation", "Points of Interest", "Reviews", "Lease Details", "Fees", "Ratings"],
                        )
                        if about_section:
                            break
                    neighborhood = _extract_neighborhood_name(raw_text, neighborhood_section)
                    if neighborhood.lower() in EXCLUDED_NEIGHBORHOODS:
                        print(f"    skip excluded neighborhood: {title} ({neighborhood})", flush=True)
                        continue
                    combined = S._norm_text(raw_text)
                    _, neg_building, building_evidence, neighborhood_evidence = S._strong_negative_location_signal(combined)
                    neg_neighborhood = bool(neighborhood_evidence)
                    record = {
                        "url": url,
                        "name": title,
                        "units": units,
                        "neighborhood": neighborhood,
                        "about": about_section,
                        "neighborhood_text": neighborhood_section,
                        "negative_building": neg_building,
                        "negative_neighborhood": neg_neighborhood,
                        "negative_building_evidence": building_evidence,
                        "negative_neighborhood_evidence": neighborhood_evidence,
                        "full_text": raw_text,
                    }
                    out.write(json.dumps(record) + "\n")
                    out.flush()
                    if sql_conn is not None:
                        try:
                            _upsert_property(sql_conn, record)
                        except Exception as exc:
                            print(f"    azure failed: {exc}", flush=True)
                    neg_flag = ""
                    if neg_building:
                        neg_flag += " NEG-BUILDING"
                    if neg_neighborhood:
                        neg_flag += " NEG-HOOD"
                    print(f"    ok {title} | neighborhood={neighborhood} | about={len(about_section)}ch{neg_flag}", flush=True)
                except Exception as exc:
                    print(f"    failed: {exc}", flush=True)
                finally:
                    if detail_page is not None:
                        detail_page.close()
        if sql_conn is not None:
            sql_conn.close()
    print(f"saved -> {TEXTS_PATH}", flush=True)


def _bigrams(text: str) -> Counter:
    words = [w for w in re.findall(r"[a-z][a-z'-]+", text.lower()) if w not in STOPWORDS and len(w) > 2]
    return Counter(zip(words, words[1:]))


def analyze(args) -> None:
    try:
        records = _load_records()
    except Exception:
        if not TEXTS_PATH.exists():
            raise SystemExit(f"no texts file: {TEXTS_PATH} (run scrape first)")
        records = [json.loads(line) for line in TEXTS_PATH.read_text().splitlines() if line.strip()]
    fixed = 0
    for r in records:
        if (r.get("neighborhood") or "").strip().lower() in ("", "unknown", "san francisco"):
            hood = _extract_neighborhood_name(r.get("full_text") or "", "")
            if hood != "Unknown" and hood.lower() != "san francisco":
                r["neighborhood"] = hood
                fixed += 1
            else:
                r["neighborhood"] = "Unknown"
    if fixed:
        print(f"re-derived neighborhood from full_text for {fixed} properties")
    records = [r for r in records if (r.get("neighborhood") or "").lower() not in EXCLUDED_NEIGHBORHOODS]
    before = len(records)
    if args.min_units:
        records = [r for r in records if r.get("units") is None or r["units"] >= args.min_units]
        if before != len(records):
            print(f"filtered to >= {args.min_units} units: {before} -> {len(records)} properties")
    by_hood = defaultdict(list)
    for r in records:
        by_hood[r.get("neighborhood") or "Unknown"].append(r)

    rows = []
    print(f"\n{len(records)} properties across {len(by_hood)} neighborhoods\n")
    for hood, recs in sorted(by_hood.items(), key=lambda kv: -len(kv[1])):
        counts = Counter()
        bigrams = Counter()
        for r in recs:
            text = f"{r.get('about') or ''} {r.get('neighborhood_text') or ''}"
            low = text.lower()
            for theme, pat in THEMES.items():
                if re.search(pat, low):
                    counts[theme] += 1
            bigrams.update(_bigrams(r.get("about") or ""))
        n = len(recs)
        top = counts.most_common(6)
        print(f"== {hood} ({n} properties) ==")
        for theme, c in top:
            pct = round(100 * c / n)
            print(f"   {theme:26s} {c:3d}/{n}  ({pct}%)")
            rows.append({"neighborhood": hood, "properties": n, "theme": theme, "count": c, "pct": pct})
        top_bg = [(f"{a} {b}", c) for (a, b), c in bigrams.most_common(8)]
        if top_bg:
            print("   top about-bigrams: " + ", ".join(f"{w} ({c})" for w, c in top_bg[:8]))
        for r in recs:
            if r.get("negative_building") or r.get("negative_neighborhood"):
                print(f"   NEGATIVE: {r.get('name')}")
                if r.get("negative_building_evidence"):
                    print(f"      building: {r['negative_building_evidence'][:250]}")
                if r.get("negative_neighborhood_evidence"):
                    print(f"      neighborhood: {r['negative_neighborhood_evidence'][:250]}")
        print()

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["neighborhood", "properties", "theme", "count", "pct"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"csv -> {CSV_PATH}", flush=True)

    neg_rows = []
    for r in records:
        for category, key in (("building", "negative_building_evidence"), ("neighborhood", "negative_neighborhood_evidence")):
            if r.get(key):
                neg_rows.append({
                    "neighborhood": r.get("neighborhood") or "Unknown",
                    "property": r.get("name"),
                    "category": category,
                    "evidence": r[key],
                    "url": r.get("url"),
                })
    with NEG_CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["neighborhood", "property", "category", "evidence", "url"])
        writer.writeheader()
        writer.writerows(neg_rows)
    print(f"negative evidence ({len(neg_rows)} hits) -> {NEG_CSV_PATH}", flush=True)

    try:
        conn = _get_db()
        conn.execute(f"DELETE FROM {THEMES_TABLE}")
        conn.executemany(
            f"INSERT INTO {THEMES_TABLE} (neighborhood, property_count, theme, mention_count, pct) VALUES (?, ?, ?, ?, ?)",
            [(r["neighborhood"], r["properties"], r["theme"], r["count"], r["pct"]) for r in rows],
        )
        conn.commit()
        conn.close()
        print(f"theme stats -> {DB_PATH} ({THEMES_TABLE}, {len(rows)} rows)", flush=True)
    except Exception as exc:
        print(f"SQLite theme write failed: {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="SF promo-theme explorer")
    sub = parser.add_subparsers(dest="mode", required=True)
    sp = sub.add_parser("scrape")
    sp.add_argument("--url", default=DEFAULT_URL)
    sp.add_argument("--max", type=int, default=20)
    sp.add_argument("--skip", type=int, default=0)
    sp.add_argument("--headless", action="store_true")
    sp.add_argument("--cdp-url", default=None)
    sp.add_argument("--sleep-min", type=float, default=8)
    sp.add_argument("--sleep-max", type=float, default=20)
    sp.add_argument("--min-units", type=int, default=0)
    ap = sub.add_parser("analyze")
    ap.add_argument("--min-units", type=int, default=80)
    args = parser.parse_args()
    if args.mode == "scrape":
        scrape(args)
    else:
        analyze(args)


if __name__ == "__main__":
    main()
