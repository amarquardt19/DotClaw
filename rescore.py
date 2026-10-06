import pymssql, os, json, sys
sys.path.insert(0, os.path.dirname(__file__))
from dateutil import parser as dateutil_parser
from email_watcher import compute_bluerock_score, _extract_rent_from_text

# Fields to track for corrections log
_TRACKED = [
    "transaction_type", "asset_class", "is_residential",
    "residential_gated", "residential_gating_reason",
    "residential_geo_score", "residential_units_score",
    "residential_price_score", "residential_rent_score",
    "residential_vintage_score", "residential_returns_score",
    "residential_strategy_score", "residential_notes_boost",
    "notes_score_reason", "score", "score_breakdown",
]
_CONTEXT_KEYS = ["deal_name", "state", "msa", "notes", "email_subject", "asset_class", "units", "vintage"]

c = pymssql.connect(os.getenv('AZURE_SQL_SERVER'), os.getenv('AZURE_SQL_USER'),
    os.getenv('AZURE_SQL_PASSWORD'), os.getenv('AZURE_SQL_DATABASE'))
cur = c.cursor()
cur.execute('SELECT uid, extracted_json, scraped_text, ocr_text FROM raw_deal_emails WHERE pre_gated = 0')
for uid, ej, scraped_text, ocr_text in cur.fetchall():
    if not ej:
        continue
    fields = json.loads(ej)
    if not fields.get('is_deal', True):
        continue
    # Normalize offer_due_date
    raw_date = fields.get("offer_due_date")
    if raw_date and isinstance(raw_date, str):
        try:
            parsed_dt = dateutil_parser.parse(raw_date, fuzzy=True)
            fields["offer_due_date"] = parsed_dt.strftime("%m-%d-%Y")
        except (ValueError, OverflowError):
            pass
    # Deterministic rent fallback from OCR/scraped text
    if not fields.get("avg_rent_monthly") and not fields.get("rent"):
        combined = (scraped_text or "") + " " + (ocr_text or "")
        regex_rent = _extract_rent_from_text(combined)
        if regex_rent:
            fields["avg_rent_monthly"] = regex_rent
    # Normalize asset_class (Senior Housing detection)
    product = str(fields.get("product") or "").upper()
    ac = str(fields.get("asset_class") or "").lower()
    deal_lower = str(fields.get("deal_name") or "").lower()
    notes_lc = str(fields.get("notes") or "").lower()
    subj_lc = str(fields.get("email_subject") or fields.get("subject") or "").lower()
    text_blob = f"{ac} {deal_lower} {notes_lc} {subj_lc}"
    _SENIOR_KW = [
        "55+", "55 +", "age-restricted", "age restricted",
        "active adult", "senior living", "senior housing",
        "assisted living", "independent living",
        "memory care", "ccrc", "continuing care",
        "over 55", "62+", "62 +",
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
    # Snapshot old values before scoring
    old_vals = {k: fields.get(k) for k in _TRACKED}
    scoring = compute_bluerock_score(fields)
    fields.update(scoring)
    # Detect diffs and log corrections
    context = json.dumps({k: fields.get(k) for k in _CONTEXT_KEYS}, default=str)
    for fld in _TRACKED:
        ov, nv = str(old_vals.get(fld) or ''), str(fields.get(fld) or '')
        if ov != nv:
            cur.execute(
                'INSERT INTO scoring_corrections (uid, deal_name, field_name, old_value, new_value, deal_context) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (uid, fields.get('deal_name', '')[:500], fld, ov[:500], nv[:500], context[:4000])
            )
    new_json = json.dumps(fields, default=str)
    cur.execute('UPDATE raw_deal_emails SET extracted_json = %s WHERE uid = %s', (new_json[:20000], uid))
    print(f"uid={uid} | asset_class={fields.get('asset_class')} | score={fields.get('score')} | notes_reason={fields.get('notes_score_reason')}")
c.commit()
c.close()
print('Done')
