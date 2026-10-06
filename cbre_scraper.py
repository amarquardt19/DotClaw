"""Scrape CBRE Deal Flow listings and feed them through the pipeline.

Usage:
    python cbre_scraper.py [--max N] [--dry-run]

Flow:
  1. Opens a visible browser — you log in manually
  2. Navigates to filtered multifamily 100+ unit listings
  3. Scrapes each listing's detail page for text
  4. Filters for text-rich listings (skips thin/OCR-only)
  5. Runs through Claude extraction → Bluerock scoring → Telegram bot
  6. Inserts into DB with synthetic UIDs (50000+ range)

The goal is to build CRITICAL MASS for training the Telegram bot
on battleground fields: transaction_type, strategy, renovated,
renovation_year, vintage, notes scoring, score_missing_inputs.
"""

import os, sys, json, time, re, argparse
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from email_watcher import (
    extract_deal_metadata, compute_bluerock_score, resolve_msa, ocr_image,
)

FILTER_URL = "https://www.cbredealflow.com?filter=_mHE8gyQ5z1fPww_PzmBTA"
# Synthetic UIDs start at 50000 to avoid collisions with email UIDs
UID_START = 50000
MIN_TEXT_CHARS = 2000  # Skip listings with less than this much text

# Must contain at least 2 of these to be "text-rich" enough for training
_DEAL_KEYWORDS = [
    "unit", "units", "apartment", "built", "year built", "vintage",
    "renovated", "renovation", "occupancy", "rent", "cap rate",
    "value-add", "value add", "stabilized", "core", "supply",
    "school", "submarket", "msa", "square feet", "sf",
    "price", "guidance", "offering", "investment",
]
_MIN_KEYWORD_HITS = 2


def _has_deal_content(text: str) -> bool:
    """Check if text has enough real deal content (not just boilerplate/images)."""
    if len(text) < MIN_TEXT_CHARS:
        return False
    lower = text.lower()
    hits = sum(1 for kw in _DEAL_KEYWORDS if kw in lower)
    return hits >= _MIN_KEYWORD_HITS


def _next_uid() -> int:
    """Get the next available synthetic UID."""
    try:
        import pymssql
        c = pymssql.connect(
            os.environ["AZURE_SQL_SERVER"], os.environ["AZURE_SQL_USER"],
            os.environ["AZURE_SQL_PASSWORD"], os.environ["AZURE_SQL_DATABASE"],
        )
        cur = c.cursor()
        cur.execute("SELECT MAX(uid) FROM raw_deal_emails WHERE uid >= %s", (UID_START,))
        row = cur.fetchone()
        c.close()
        return (row[0] + 1) if row and row[0] else UID_START
    except Exception:
        return UID_START


def _collect_listing_urls(page) -> list:
    """Scroll through the listing page and collect all detail page URLs + card info."""
    # Scroll down repeatedly to load lazy content (page has 155+ assets)
    prev_urls = 0
    for scroll_round in range(40):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
        cur_urls = page.evaluate("document.querySelectorAll('a[href*=\"landing.aspx\"]').length")
        if cur_urls == prev_urls and scroll_round > 5:
            break  # No new cards loading
        prev_urls = cur_urls
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1)

    # Grab all landing.aspx links with their parent card context
    listings = page.evaluate("""() => {
        const seen = new Set();
        const results = [];
        document.querySelectorAll('a[href*="landing.aspx"]').forEach(a => {
            const url = a.href;
            if (seen.has(url)) return;
            seen.add(url);
            // Walk up to find card container text
            let el = a;
            for (let i = 0; i < 8 && el.parentElement; i++) {
                el = el.parentElement;
                if (el.innerText && el.innerText.length > 40) break;
            }
            const cardText = el ? el.innerText.substring(0, 400) : '';
            results.push({url, cardText});
        });
        return results;
    }""")
    return listings


def _scrape_detail_page(page, url: str) -> dict:
    """Load a listing detail page, extract plain text + OCR screenshot."""
    try:
        # Intercept network responses to capture API data (bypasses font-mapped DOM text)
        api_responses = []
        def _capture_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct and response.status == 200:
                    body = response.text()
                    if len(body) > 50 and any(kw in body.lower() for kw in ["year built", "yearbuilt", "total units", "totalunits", "unit count", "rentable"]):
                        api_responses.append(body)
            except Exception:
                pass
        page.on("response", _capture_response)

        page.goto(url, timeout=15000, wait_until="domcontentloaded")
        time.sleep(6)  # Extra wait for dynamic content (property summary tables etc.)

        # Scroll down to trigger lazy-loaded content
        for _ in range(8):
            page.evaluate("window.scrollBy(0, 600)")
            time.sleep(0.6)
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(2)
        page.remove_listener("response", _capture_response)

        # Append any captured API data (most reliable source — exact values)
        api_data_text = ""
        if api_responses:
            api_data_text = "\n".join(api_responses)
            print(f"    API data captured: {len(api_data_text)} chars from {len(api_responses)} responses", flush=True)

        # Plain text extraction — use both innerText and textContent
        text = page.evaluate("() => document.body.innerText") or ""
        # textContent captures hidden/styled text that innerText misses
        text_content = page.evaluate("() => document.body.textContent") or ""
        if len(text_content) > len(text) + 200:
            text = text + "\n\nADDITIONAL TEXT CONTENT:\n" + text_content
        # Also grab text from all table cells, spans, etc.
        extra = page.evaluate("""
            () => {
                const els = document.querySelectorAll('td, th, .property-summary, [class*=summary], [class*=detail], [class*=stat], [class*=metric], dt, dd, label, span');
                return Array.from(els).map(e => e.textContent.trim()).filter(t => t.length > 0 && t.length < 200).join(' | ');
            }
        """) or ""
        if extra:
            text = text + "\n\nEXTRACTED TABLE/STAT DATA:\n" + extra

        # Targeted property summary extraction — get aria-labels, alt text, and
        # any element with numeric content near property summary keywords
        prop_summary = page.evaluate("""
            () => {
                const results = [];
                // Method 1: Find elements by aria-label or title attributes
                document.querySelectorAll('[aria-label], [title], [data-value], [alt]').forEach(el => {
                    const label = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('data-value') || el.getAttribute('alt') || '';
                    if (label.trim()) results.push(label.trim());
                });
                // Method 2: Find all text nodes and their computed styles
                // (catches text rendered via CSS or positioned absolutely)
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while (node = walker.nextNode()) {
                    const t = node.textContent.trim();
                    if (t && /\\d{4}/.test(t) && t.length < 50) {
                        const parent = node.parentElement;
                        if (parent) {
                            const style = window.getComputedStyle(parent);
                            if (style.display !== 'none' && style.visibility !== 'hidden') {
                                results.push(t);
                            }
                        }
                    }
                }
                return [...new Set(results)].join(' | ');
            }
        """) or ""
        if prop_summary:
            text = text + "\n\nPROPERTY SUMMARY VALUES:\n" + prop_summary
            print(f"    Property summary extract: {len(prop_summary)} chars", flush=True)
        title = page.title() or ""

        # Targeted OCR: find styled property data sections by background color.
        # Font remapping garbles BOTH values AND labels, so we can't search by text.
        # Instead, find colored-background containers of the right size — these are
        # the property snapshot / investment highlights / key metrics sections.
        snapshot_ocr = ""
        try:
            styled_boxes = page.evaluate("""
                () => {
                    const found = [];
                    const seen = new Set();
                    const all = document.querySelectorAll('section, div, table, aside, article');
                    for (const el of all) {
                        const rect = el.getBoundingClientRect();
                        // Right size for a property data table
                        if (rect.height < 200 || rect.height > 800) continue;
                        if (rect.width < 300 || rect.width > 900) continue;
                        if (rect.y + rect.height < 0) continue;

                        const style = window.getComputedStyle(el);
                        const bg = style.backgroundColor || '';
                        // Must have a colored background (not white, not transparent)
                        if (!bg || bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') continue;
                        // Parse RGB to check it's not white/near-white
                        const rgb = bg.match(/\\d+/g);
                        if (!rgb || rgb.length < 3) continue;
                        const [r, g, b] = rgb.map(Number);
                        if (r > 240 && g > 240 && b > 240) continue; // skip white/near-white
                        if (r === 0 && g === 0 && b === 0) continue; // skip pure black

                        // Deduplicate overlapping boxes
                        const key = Math.round(rect.y / 80);
                        if (seen.has(key)) continue;
                        seen.add(key);
                        found.push({
                            x: rect.x, y: rect.y + window.scrollY,
                            width: rect.width, height: rect.height,
                            bg: bg
                        });
                    }
                    // Sort by vertical position (top of page first)
                    return found.sort((a,b) => a.y - b.y).slice(0, 3);
                }
            """)
            if styled_boxes:
                snap_parts = []
                for i, box in enumerate(styled_boxes):
                    page.evaluate(f"window.scrollTo(0, {max(0, box['y'] - 50)})")
                    time.sleep(0.5)
                    scroll_y = page.evaluate("window.scrollY")
                    clip = {
                        "x": max(0, box["x"] - 10),
                        "y": max(0, box["y"] - scroll_y - 10),
                        "width": min(box["width"] + 20, 900),
                        "height": min(box["height"] + 20, 700),
                    }
                    snap_bytes = page.screenshot(type="jpeg", quality=95, clip=clip)
                    snap_text = ocr_image(snap_bytes, "image/jpeg",
                                          f"cbre_snapshot_{i}.jpg")
                    if snap_text and snap_text.strip().upper() != "NONE":
                        snap_parts.append(snap_text)
                        print(f"    Snapshot OCR [bg={box['bg']}, {int(box['width'])}x{int(box['height'])}]: {len(snap_text)} chars", flush=True)
                if snap_parts:
                    snapshot_ocr = "\n".join(snap_parts)
                    text = text + "\n\nPROPERTY SNAPSHOT OCR (high-res — trust over DOM text):\n" + snapshot_ocr
            else:
                print(f"    Snapshot OCR: no styled sections found", flush=True)
        except Exception as e:
            print(f"    Snapshot OCR failed: {e}", flush=True)

        # Chunked screenshot → OCR (split page into ~1500px sections)
        ocr_text = ""
        try:
            page_height = page.evaluate("() => document.body.scrollHeight")
            vp_height = 1080  # must match viewport height — no gaps
            chunk_h = vp_height
            ocr_parts = []
            n_chunks = max(1, (page_height + chunk_h - 1) // chunk_h)
            print(f"    Page height: {page_height}px -> {n_chunks} OCR chunks", flush=True)
            for ci in range(n_chunks):
                y_offset = ci * chunk_h
                page.evaluate(f"window.scrollTo(0, {y_offset})")
                time.sleep(0.8)
                chunk_bytes = page.screenshot(type="jpeg", quality=90)
                chunk_text = ocr_image(chunk_bytes, "image/jpeg", f"cbre_{title[:20]}_chunk{ci}.jpg")
                if chunk_text and chunk_text.strip().upper() != "NONE":
                    ocr_parts.append(chunk_text)
                    print(f"    OCR chunk {ci+1}/{n_chunks}: {len(chunk_text)} chars", flush=True)
            ocr_text = "\n\n".join(ocr_parts)
            if ocr_text:
                print(f"    OCR total: {len(ocr_text)} chars", flush=True)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception as e:
            print(f"    OCR failed: {e}", flush=True)

        # Append API-intercepted data as highest-priority source
        if api_data_text:
            text = text + "\n\nAPI DATA (EXACT VALUES — trust over DOM text):\n" + api_data_text
        return {"text": text, "ocr_text": ocr_text, "title": title, "url": url}
    except Exception as e:
        print(f"    -> Failed: {e}", flush=True)
        return {"text": "", "ocr_text": "", "title": "", "url": url}


def _extract_title_from_card(card_text: str) -> str:
    """Pull a deal name from the card summary text."""
    skip = {"multifamily", "details", "contacts", "available",
            "multifamily | united states", "sign in", "don't have an account? sign up"}
    for line in card_text.split("\n"):
        line = line.strip()
        if len(line) > 3 and line.lower() not in skip:
            return line
    return "Unknown"


def _process_listing(uid: int, listing_text: str, title: str, url: str,
                     dry_run: bool = False) -> dict:
    """Run a scraped listing through the full pipeline."""
    subject = f"CBRE Deal Flow: {title}" if title else "CBRE Deal Flow Listing"

    print(f"\n  [{uid}] Extracting: {title[:60]}...", flush=True)
    print(f"       Text: {len(listing_text)} chars", flush=True)

    # Debug: check what's in the text
    text_lower = listing_text.lower()
    for phrase in ["year built", "vintage", "value-add", "value add", "built in", "2000"]:
        if phrase in text_lower:
            # Find context around it
            idx = text_lower.index(phrase)
            snippet = listing_text[max(0, idx-20):idx+40].replace("\n", " ")
            print(f"       TEXT MATCH: '{phrase}' -> ...{snippet}...", flush=True)

    # Claude extraction
    fields = extract_deal_metadata(subject, listing_text)
    is_deal = fields.get("is_deal", True)
    deal_name = fields.get("deal_name") or fields.get("property_name") or title or "?"

    if not is_deal:
        print(f"       -> Not a deal, skipping", flush=True)
        return fields

    print(f"       -> Deal: {deal_name}", flush=True)

    # Deterministic vintage fallback when Claude misses it
    if not fields.get("vintage"):
        from email_watcher import _extract_vintage_from_text
        regex_vintage = _extract_vintage_from_text(listing_text)
        if regex_vintage:
            fields["vintage"] = regex_vintage
            print(f"       -> Regex vintage: {regex_vintage}", flush=True)

    # Deterministic rent fallback when Claude misses it
    if not fields.get("avg_rent_monthly") and not fields.get("rent"):
        from email_watcher import _extract_rent_from_text
        regex_rent = _extract_rent_from_text(listing_text)
        if regex_rent:
            fields["avg_rent_monthly"] = regex_rent
            print(f"       -> Regex rent: ${regex_rent:,.0f}/mo", flush=True)

    # Debug: show battleground extractions
    print(f"       -> EXTRACTED: vintage={fields.get('vintage')} | units={fields.get('units')} | "
          f"tt={fields.get('transaction_type')} | rent={fields.get('avg_rent_monthly')} | "
          f"renovated={fields.get('renovated')} | reno_yr={fields.get('renovation_year')}", flush=True)

    # Normalize asset_class
    product = str(fields.get("product") or "").upper()
    ac = str(fields.get("asset_class") or "").lower()
    deal_lower = str(fields.get("deal_name") or "").lower()
    notes_lc = str(fields.get("notes") or "").lower()
    text_blob = f"{ac} {deal_lower} {notes_lc}"

    _SENIOR_KW = [
        "55+", "55 +", "age-restricted", "age restricted",
        "active adult", "senior living", "senior housing",
        "assisted living", "independent living",
        "memory care", "ccrc", "continuing care",
    ]
    is_senior = any(kw in text_blob for kw in _SENIOR_KW)

    if product in ("MF", "TH", "BFR") or any(
        kw in ac for kw in ("multifamily", "residential", "apartment",
                            "garden", "luxury", "class a", "class b",
                            "class c", "bfr", "build for rent",
                            "townhome", "senior", "student")
    ) or is_senior:
        if is_senior or "senior" in ac:
            fields["asset_class"] = "Senior Housing"
        elif "student" in ac or "student" in deal_lower:
            fields["asset_class"] = "Student Housing"
        else:
            fields["asset_class"] = "Multifamily"

    # MSA lookup
    if fields.get("city") and fields.get("state"):
        try:
            msa = resolve_msa(fields["city"], fields["state"])
            if msa:
                fields["msa"] = msa
                print(f"       -> MSA: {msa}", flush=True)
        except Exception:
            pass

    # Stash full text so scoring can scan it (not just Claude's notes summary)
    fields["_full_text"] = listing_text

    # Score
    scoring = compute_bluerock_score(fields, training_mode=True)
    fields.update(scoring)
    print(f"       -> Score: {fields.get('score')} ({fields.get('score_breakdown', '')[:80]})", flush=True)

    if dry_run:
        print(f"       -> DRY RUN: skipping Telegram", flush=True)
        return fields

    # Send to Telegram bot for review (no DB — this is for bot training only)
    fields["uid"] = uid
    fields["source"] = "cbre_dealflow"
    try:
        from scoring_qa_bot import send_deal_review
        send_deal_review(fields, scoring)
        print(f"       -> Telegram: sent for review", flush=True)
    except Exception as e:
        print(f"       -> Telegram: {e}", flush=True)

    return fields


def main():
    parser = argparse.ArgumentParser(description="Scrape CBRE Deal Flow listings")
    parser.add_argument("--max", type=int, default=40, help="Max listings to process")
    parser.add_argument("--skip", type=int, default=0, help="Skip first N listings (resume)")
    parser.add_argument("--dry-run", action="store_true", help="Extract only, skip Telegram")
    parser.add_argument("--one", action="store_true", help="One at a time — wait for Enter between deals")
    parser.add_argument("--url", type=str, default=FILTER_URL, help="CBRE Deal Flow filter URL")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install")
        sys.exit(1)

    uid = _next_uid()
    print(f"Starting UID: {uid}")
    processed = 0
    skipped = 0

    with sync_playwright() as p:
        # No login needed — site is publicly accessible
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1920, "height": 1080},
                                        device_scale_factor=2)
        page = context.new_page()

        # Go directly to filtered listings
        print(f"\nNavigating to filtered listings...", flush=True)
        page.goto(args.url, timeout=20000, wait_until="domcontentloaded")
        time.sleep(5)

        # Collect all listing detail URLs from the page
        print("Collecting listing URLs...", flush=True)
        listings = _collect_listing_urls(page)
        total_cards = len(listings)
        print(f"Found {total_cards} listing detail URLs", flush=True)

        if not total_cards:
            print("No listings found.", flush=True)
            browser.close()
            return

        n_to_process = min(total_cards, args.max)
        for c in listings[:5]:
            title = _extract_title_from_card(c["cardText"])
            print(f"  - {title[:50]} -> {c['url'][:70]}", flush=True)

        # Scrape each detail page (skip already-processed)
        if args.skip:
            print(f"Skipping first {args.skip} listings...", flush=True)
            uid += args.skip
        for i, listing in enumerate(listings[args.skip:args.skip + n_to_process]):
            card_title = _extract_title_from_card(listing["cardText"])
            print(f"\n{'─' * 60}", flush=True)
            print(f"[{i+1}/{n_to_process}] {card_title[:60]}", flush=True)

            # Fresh context per page — avoids cached custom fonts that remap chars
            fresh_ctx = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=2,
            )
            fresh_page = fresh_ctx.new_page()
            detail = _scrape_detail_page(fresh_page, listing["url"])
            plain_text = detail["text"]
            ocr_text = detail.get("ocr_text", "")

            print(f"    Plain text: {len(plain_text)} chars | OCR: {len(ocr_text)} chars", flush=True)

            # Combine all text sources
            card_info = listing.get("cardText", "")
            parts = []
            if card_info:
                parts.append(f"LISTING CARD: {card_info}")
            if plain_text:
                parts.append(f"DETAIL PAGE TEXT:\n{plain_text}")
            if ocr_text:
                parts.append(f"OCR FROM SCREENSHOT:\n{ocr_text}")
            text = "\n\n".join(parts)

            if len(text) < 300:
                print(f"    -> No content even with OCR, skipping", flush=True)
                skipped += 1
                continue

            title = card_title or detail.get("title", f"Listing #{i+1}")
            fields = _process_listing(
                uid, text, title,
                listing["url"], dry_run=args.dry_run,
            )

            if fields.get("is_deal", True):
                uid += 1
                processed += 1

            # One-at-a-time mode: browser stays on detail page while user reviews
            if args.one and not args.dry_run:
                remaining = n_to_process - (i + 1)
                if remaining > 0:
                    print(f"\n    ⏸  Browser on detail page — review in Telegram, press ENTER for next ({remaining} remaining)...", flush=True)
                else:
                    print(f"\n    ⏸  Last deal — review in Telegram, press ENTER to finish...", flush=True)
                input()

            # Close detail page context (after review if --one)
            fresh_ctx.close()

            # Rate limit
            time.sleep(1)

        browser.close()

    print(f"\n{'=' * 60}")
    print(f"  CBRE Scraper Complete")
    print(f"  Processed: {processed} deals")
    print(f"  Skipped: {skipped} (too little text)")
    print(f"  Total cards found: {total_cards if 'total_cards' in dir() else 0}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
