"""Re-extract a single UID through Claude with the updated prompt, then re-score.
Optionally follows 'View in browser' links if --follow-links flag is passed."""
import pymssql, os, json, sys
sys.path.insert(0, os.path.dirname(__file__))
from dateutil import parser as dateutil_parser
from email_watcher import (extract_deal_metadata, compute_bluerock_score, resolve_msa,
                           web_search_avg_rent, _scrape_view_links, _html_to_text,
                           imap_connect, imap_fetch_full)

follow_links = "--follow-links" in sys.argv
uid = int([a for a in sys.argv[1:] if not a.startswith("-")][0])
c = pymssql.connect(os.getenv('AZURE_SQL_SERVER'), os.getenv('AZURE_SQL_USER'),
    os.getenv('AZURE_SQL_PASSWORD'), os.getenv('AZURE_SQL_DATABASE'))
cur = c.cursor()
cur.execute('SELECT email_subject, scraped_text, ocr_text FROM raw_deal_emails WHERE uid=%s', (uid,))
row = cur.fetchone()
if not row:
    print(f"uid={uid} not found"); sys.exit(1)

subj, scraped, ocr = row

# If follow-links, re-fetch the email to get HTML for link extraction
link_text = ""
if follow_links:
    print("Fetching email via IMAP for link-following...")
    M = imap_connect()
    email_data = imap_fetch_full(M, uid)
    html = email_data.get("html", "")
    if html:
        link_text = _scrape_view_links(html)
        if link_text:
            print(f"  -> Link-follow: scraped {len(link_text)} chars")
        else:
            print("  -> No 'View in browser' links found in HTML")
    M.logout()

full_text = (scraped or '')
if ocr:
    full_text += '\n\n--- OCR FROM IMAGES ---\n' + ocr
if link_text:
    full_text += '\n\n--- SCRAPED FROM VIEW LINK ---\n' + link_text
print(f"Re-extracting uid={uid}: {subj[:60]}")
print(f"  text length: {len(full_text)} chars")

fields = extract_deal_metadata(subj, full_text)
print(f"  deal_name: {fields.get('deal_name')}")
print(f"  notes: {fields.get('notes')}")
print(f"  positive_signals: {fields.get('positive_signals')}")

# Normalize asset_class
product = str(fields.get("product") or "").upper()
ac = str(fields.get("asset_class") or "").lower()
if product in ("MF", "TH", "BFR") or any(
    kw in ac for kw in ("multifamily", "residential", "apartment", "garden", "luxury",
                         "class a", "class b", "class c", "bfr", "build for rent", "townhome")):
    fields["asset_class"] = "Multifamily"

# MSA
if fields.get("city") and fields.get("state"):
    msa = resolve_msa(fields["city"], fields["state"])
    if msa:
        fields["msa"] = msa
        print(f"  msa: {msa}")

# Date normalization
raw_date = fields.get("offer_due_date")
if raw_date and isinstance(raw_date, str):
    try:
        fields["offer_due_date"] = dateutil_parser.parse(raw_date, fuzzy=True).strftime("%m-%d-%Y")
    except (ValueError, OverflowError):
        pass

# Web rent fallback
if not fields.get("avg_rent_monthly"):
    prop = fields.get("deal_name") or ""
    city = fields.get("city") or ""
    state = fields.get("state") or ""
    if prop and city and state:
        wr = web_search_avg_rent(prop, city, state)
        if wr:
            fields["avg_rent_monthly"] = wr
            fields["web_avg_rent_monthly"] = wr
            print(f"  web_rent: ${wr:,.0f}/mo")

# Score
scoring = compute_bluerock_score(fields)
fields.update(scoring)
print(f"  score: {fields['score']}")
print(f"  breakdown: {fields['score_breakdown']}")
print(f"  notes_reason: {fields.get('notes_score_reason')}")

new_json = json.dumps(fields, default=str)
cur.execute('UPDATE raw_deal_emails SET extracted_json=%s WHERE uid=%s', (new_json[:20000], uid))
c.commit()
c.close()
print("Done — updated in DB")
