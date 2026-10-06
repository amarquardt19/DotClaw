"""
Telegram scoring QA bot — reviews each scored deal, guesses what might
be wrong, learns from user corrections.

Flow:
  1. email_watcher scores a deal
  2. This bot sends the deal summary + AI guesses to Telegram
  3. User replies with corrections or ✓
  4. Bot reasons about WHY its rules were wrong
  5. User grades the reasoning 👍/👎
  6. Corrections + grades accumulate as training data

The bot gets "smarter" by including past corrections as few-shot
examples when analyzing new deals.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# QA bot has its own Telegram bot (@DealSifta_bot), separate from @emiliodealiobot
TELEGRAM_BOT_TOKEN = os.getenv("QA_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("QA_BOT_CHAT_ID", "0"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_EXTRACT_MODEL", "claude-sonnet-4-20250514")

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _tg_escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{ch}" if ch in special else ch for ch in str(text))


def _tg_send(text: str, reply_to: Optional[int] = None) -> int:
    """Send a MarkdownV2 message. Returns message_id."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                return result["result"]["message_id"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[qa-bot] Telegram send error {e.code}: {body}", flush=True)
    except Exception as e:
        print(f"[qa-bot] Telegram send error: {e}", flush=True)
    return 0


def _tg_get_updates(offset: int = 0, timeout: int = 0) -> list:
    """Get updates from Telegram (long-poll if timeout > 0)."""
    url = (f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
           f"?offset={offset}&timeout={timeout}")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                return result.get("result", [])
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Claude reasoning
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a SCORING AUDITOR for a real estate deal pipeline.

You KNOW the Bluerock scoring rules — they are defined below as GOSPEL. You do not
guess rules, learn rules, or infer rules from corrections. You ALREADY KNOW THEM.

YOUR ROLE:
1. CHECK the Python pipeline's output against the KNOWN RULES below
2. FLAG any battleground field where Python got it wrong per the rules
3. READ the deal text carefully — if the text contains info that Python missed
   (e.g., text says "built in 1998" but vintage is None), flag it

You are focused on 10 BATTLEGROUND FIELDS only:
  asset_class, product, transaction_type, strategy, units, vintage,
  renovated, renovation_year, notes_scoring, score_missing_inputs

═══════════════════════════════════════════════════════════════
KNOWN SCORING RULES (GOSPEL — do not deviate)
═══════════════════════════════════════════════════════════════

TRANSACTION TYPE / STRATEGY:
  - transaction_type examples: "Value-add", "Core", "Core+", "Development",
    "Lease-up", "Mark-to-Market", "Stabilized", "Reposition", "Heavy lift"
  - Python trusts Claude's value ONLY if it matches a known strategy keyword.
    Generic values like "sale" or "acquisition" are NOT strategies.
  - If Claude's value isn't recognized, Python scans notes + subject for keywords:
    Mark-to-Market (checked first): "mark-to-market", "statutory rent premium",
      "below" + "submarket", "untapped value"
    Development: "shovel ready", "entitled to build", "ground-up", "forward purchase"
    Lease-up: "lease-up", "recently delivered", "delivered vacant"
    Value-add: "value-add", "renovation opportunity", "classic condition",
      "reposition", "interior upgrade", "partially renovated", "unrenovated",
      "deferred maintenance", "renovation roadmap", "renovation upside"
    Core: "stabilized", "core plus", "fully occupied", "fully leased"

STRATEGY SCORING (from resolved transaction_type):
  0.5 pts — Development / Lease-up / Heavy lift
  0.3 pts — Core / Stabilized / Core+
  0.2 pts — Value-add / Mark-to-Market / Reposition
  0.0 pts — missing or unclear

UNITS:
  2.0 pts if 100-500 units
  1.0 pts if 80-99 or 501-600 units
  0.0 pts if missing or outside range
  Extract as integer from text.

VINTAGE (year built):
  1.0 pts if >= 2000
  0.5 pts if 1980-1999
  0.0 pts if pre-1980 or missing
  Extract as 4-digit year. A property with a vintage is an existing asset, NOT development.

RENOVATED / RENOVATION_YEAR:
  renovated = true ONLY if email indicates renovation completed
  renovation_year = include ONLY if explicitly stated (e.g., "renovated in 2023")
  Reno boost: +0.25 if renovated=true AND renovation_year >= 2022
  Vintage score capped at 1.0 max after boost

NOTES SCORING (qualitative boost, +0 to +2):
  Count these positive signals if EXPLICITLY stated in text:
    1. Strong school district
    2. Limited new supply / barriers to entry
    3. Assumable debt
    4. Discount to replacement (only if >= 15% explicitly stated)
    5. Below-market rents / major mark-to-market opportunity
    6. Institutional sponsor / institutional-quality sponsorship
  Negative blockers (override to 0): crime, collections issues, insurance shock,
    HOA litigation, unmitigated regulatory risk
  Scoring: 0 signals=0pts, 1 signal=1pt, 2+ signals=2pts

ASSET CLASS:
  The broad classification: "Multifamily", "Senior Housing", "Student Housing",
  "Industrial", "Office", etc. Must be extracted from text.
  Residential = any of: multifamily, residential, apartment, garden, luxury,
  class a/b/c, bfr, build for rent, townhome, senior housing, student housing.

PRODUCT:
  For residential deals ONLY. Must be one of: "MF" (multifamily), "TH" (townhome),
  "BFR" (build for rent). Map from text: "multifamily"->MF, "townhome(s)"->TH,
  "build for rent"->BFR. If not applicable, omit.

SCORE_MISSING_INPUTS:
  Python provides its list of missing inputs. Your job is to evaluate that list:
  - Is Python correct? Did it miss something that IS in the text?
  - Did Python wrongly mark something as present when it's actually missing?
  Do NOT invent your own list from scratch. Start from Python's list and critique it.

═══════════════════════════════════════════════════════════════

WHAT YOU LEARN FROM CORRECTIONS (the ONLY thing):
  - EXTRACTION PATTERNS: when the text contains info that the pipeline missed.
    Example: "Text said 'built in 1998' but vintage was None — Claude missed it."
    Example: "Text mentioned 'limited supply' but notes scoring missed it."
  - You NEVER learn scoring rules from corrections. You already know them.
  - Past corrections are provided below as LEARNED PATTERNS. Use them to spot
    similar extraction failures in new deals.

FORMAT FOR INITIAL REVIEW:
Keep it SHORT. One line per field. No essays, no debating, no rambling.

For each of the 10 battleground fields, output ONE line:
  ✓ if Python is correct
  ✗ if Python is wrong — state the correct value and ONE sentence of evidence

Example format:
  1. ASSET CLASS: Multifamily ✓
  2. TRANSACTION TYPE: Investment Sale ✗ → Value-add (text says "renovation opportunity")
  3. UNITS: 300 ✓
  ...
  8. NOTES SCORING: 1.0 pts ✗ → should be 2.0 (text also mentions "limited land availability")

For notes scoring: just list which signals ARE and ARE NOT present. Do NOT write
paragraphs analyzing individual school names or debating evidence. One line.

After the audit:
  PREDICTED_FIELDS: comma-separated list of wrong fields (or "none")
  FIELD_CHANGES: {"field": "value"} (if any errors)
  For notes_scoring errors: use "positive_signals" key with the correct signal list.

End with: "See any issues?"

STRICT: max 2-3 lines per field. No fluff."""


def _call_claude(system: str, messages: list, max_tokens: int = 1500) -> str:
    """Call Claude with a system prompt and message history."""
    if not ANTHROPIC_API_KEY:
        return "⚠️ No ANTHROPIC_API_KEY — skipping AI review"

    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            return result["content"][0]["text"]
    except Exception as e:
        return f"⚠️ Claude error: {e}"


def _ask_claude(deal_summary: str, past_corrections: str) -> str:
    """Ask Claude to review a scored deal (initial review)."""
    user_msg = deal_summary
    if past_corrections:
        user_msg += f"\n\nPAST CORRECTIONS (learn from these):\n{past_corrections}"
    return _call_claude(_SYSTEM_PROMPT, [{"role": "user", "content": user_msg}])


# ---------------------------------------------------------------------------
# Corrections DB
# ---------------------------------------------------------------------------

def _get_db():
    """Get a pymssql connection."""
    import pymssql
    return pymssql.connect(
        os.environ["AZURE_SQL_SERVER"],
        os.environ["AZURE_SQL_USER"],
        os.environ["AZURE_SQL_PASSWORD"],
        os.environ["AZURE_SQL_DATABASE"],
    )


def _load_past_corrections(limit: int = 20) -> str:
    """Load past corrections as structured LEARNED PATTERNS for Claude."""
    try:
        c = _get_db()
        cur = c.cursor()
        # Get user corrections (skip bot internal grades/self-grades)
        cur.execute(
            "SELECT TOP %d uid, deal_name, field_name, old_value, new_value, "
            "deal_context FROM scoring_corrections "
            "WHERE field_name LIKE 'user_correction_%%' "
            "ORDER BY id DESC" % limit
        )
        rows = cur.fetchall()
        c.close()
        if not rows:
            return ""
        patterns = []
        for uid, name, field, old, new, ctx in rows:
            category = field.replace("user_correction_", "")
            pattern = f"PATTERN: {name} (uid={uid}) — {category}\n"
            pattern += f"  User said: {new}\n"
            if ctx:
                pattern += f"  Deal context: {ctx[:300]}"
            patterns.append(pattern)
        return "\n".join(patterns)
    except Exception:
        return ""


def _save_correction(uid: int, deal_name: str, field: str,
                     old_val: str, new_val: str, context: str):
    """Save a user correction to the DB."""
    try:
        c = _get_db()
        cur = c.cursor()
        cur.execute(
            "INSERT INTO scoring_corrections "
            "(uid, deal_name, field_name, old_value, new_value, deal_context) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, deal_name[:500], field, old_val[:500], new_val[:500], context[:4000])
        )
        c.commit()
        c.close()
    except Exception as e:
        print(f"[qa-bot] DB save error: {e}", flush=True)


def _apply_corrections_to_db(uid: int, deal_name: str,
                             field_changes: Dict[str, Any]) -> str:
    """Apply field corrections to raw_deal_emails, rescore, push to mart_deals."""
    try:
        import pymssql
        c = _get_db()
        cur = c.cursor()
        cur.execute('SELECT extracted_json FROM raw_deal_emails WHERE uid=%s', (uid,))
        row = cur.fetchone()
        if not row:
            c.close()
            return f"uid={uid} not found in raw_deal_emails"

        ej = json.loads(row[0])
        applied = []
        for field, value in field_changes.items():
            old = ej.get(field)
            ej[field] = value
            applied.append(f"{field}: {old} → {value}")

        # Rescore with updated fields — strip stale scoring output first
        _SCORING_OUTPUT_KEYS = {
            "score", "score_breakdown", "score_missing_inputs", "score_version",
            "is_residential", "is_industrial", "residential_gated",
            "residential_gating_reason", "residential_interest_score",
            "residential_geo_score", "residential_units_score",
            "residential_price_score", "residential_rent_score",
            "residential_vintage_score", "residential_returns_score",
            "residential_strategy_score", "residential_notes_boost",
            "residential_reno_boost", "notes_score_reason",
            "industrial_interest_score",
        }
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from email_watcher import compute_bluerock_score
            is_scraper = (uid or 0) >= 50000
            clean_fields = {k: v for k, v in ej.items() if k not in _SCORING_OUTPUT_KEYS}
            scoring = compute_bluerock_score(clean_fields, training_mode=is_scraper)
            # Preserve user-corrected fields — don't let scoring overwrite them
            preserved = {f: ej[f] for f in field_changes if f in ej}
            ej.update(scoring)
            ej.update(preserved)
        except Exception as e:
            print(f"[qa-bot] Rescore failed: {e}", flush=True)

        # Push to raw_deal_emails
        cur.execute('UPDATE raw_deal_emails SET extracted_json=%s WHERE uid=%s',
                    (json.dumps(ej), uid))

        # Push ALL scoring-relevant fields to mart_deals
        cur.execute(
            "UPDATE mart_deals SET score=%s, score_breakdown=%s, "
            "score_missing_inputs=%s, transaction_type=%s, "
            "notes_score_reason=%s, vintage=%s, units=%s, "
            "avg_rent_monthly=%s, guidance_price=%s WHERE uid=%s",
            (ej.get('score'), ej.get('score_breakdown'),
             ej.get('score_missing_inputs'), ej.get('transaction_type'),
             ej.get('notes_score_reason'), ej.get('vintage'),
             ej.get('units'), ej.get('avg_rent_monthly'),
             ej.get('guidance_price'), uid)
        )
        c.commit()
        c.close()
        return "\n".join(applied) + f"\nNew score: {ej.get('score')}"
    except Exception as e:
        return f"DB error: {e}"


def _save_grade(uid: int, deal_name: str, correct: bool,
                category: str = "overall"):
    """Save a grade (👍/👎) for the bot's reasoning.
    
    category: 'extraction', 'scoring', 'missing_inputs', or 'overall'
    """
    try:
        c = _get_db()
        cur = c.cursor()
        cur.execute(
            "INSERT INTO scoring_corrections "
            "(uid, deal_name, field_name, old_value, new_value, deal_context) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, deal_name[:500], f"_bot_grade_{category}",
             "", "correct" if correct else "wrong",
             json.dumps({"graded_at": datetime.utcnow().isoformat(),
                         "category": category}))
        )
        c.commit()
        c.close()
    except Exception as e:
        print(f"[qa-bot] DB grade error: {e}", flush=True)


def _get_accuracy() -> str:
    """Get bot accuracy stats — overall + per-category breakdown."""
    try:
        c = _get_db()
        cur = c.cursor()
        cur.execute(
            "SELECT field_name, new_value, COUNT(*) FROM scoring_corrections "
            "WHERE field_name LIKE '_bot_grade%' GROUP BY field_name, new_value"
        )
        # Build {category: {correct: N, wrong: N}}
        cats: Dict[str, Dict[str, int]] = {}
        for fname, val, cnt in cur.fetchall():
            cat = fname.replace("_bot_grade_", "").replace("_bot_grade", "overall")
            cats.setdefault(cat, {"correct": 0, "wrong": 0})
            cats[cat][val] = cnt
        c.close()

        if not cats:
            return "No grades yet"

        # Overall = sum of all categories
        tot_c = sum(v["correct"] for v in cats.values())
        tot_w = sum(v["wrong"] for v in cats.values())
        tot = tot_c + tot_w
        lines = [f"Overall: {tot_c}/{tot} ({tot_c/tot*100:.0f}%)" if tot else "Overall: 0"]

        for cat in ["extraction", "scoring", "missing_inputs"]:
            if cat in cats:
                cc = cats[cat]["correct"]
                ww = cats[cat]["wrong"]
                tt = cc + ww
                lines.append(f"{cat.replace('_',' ').title()}: {cc}/{tt} ({cc/tt*100:.0f}%)")

        return " | ".join(lines)
    except Exception:
        return "Stats unavailable"


# ---------------------------------------------------------------------------
# Main bot functions
# ---------------------------------------------------------------------------

# Track the last review so replies can reference it — persisted to file
_REVIEW_FILE = Path(__file__).resolve().parent / ".last_review.json"
_last_review: Dict[str, Any] = {}


def _save_review_state():
    """Persist _last_review to disk so it survives restarts."""
    try:
        # Don't persist the full fields dict — just what we need for replies
        to_save = {}
        for k in ["uid", "deal_name", "msg_id", "deal_summary",
                  "ai_review", "_pending_category", "_pending_changes",
                  "_predicted_fields", "_conversation",
                  "bot_proposed_score", "bot_proposed_breakdown"]:
            if k in _last_review:
                to_save[k] = _last_review[k]
        if "fields" in _last_review:
            to_save["fields"] = dict(_last_review["fields"])
        if "scoring" in _last_review:
            to_save["scoring"] = _last_review["scoring"]
        _REVIEW_FILE.write_text(json.dumps(to_save, default=str))
    except Exception as e:
        print(f"[qa-bot] Failed to save review state: {e}", flush=True)


def _load_review_state():
    """Load _last_review from disk on startup."""
    global _last_review
    try:
        if _REVIEW_FILE.exists():
            _last_review = json.loads(_REVIEW_FILE.read_text())
            if _last_review:
                print(f"[qa-bot] Restored review state for uid={_last_review.get('uid')}", flush=True)
    except Exception as e:
        print(f"[qa-bot] Failed to load review state: {e}", flush=True)
        _last_review = {}


def battleground_reasoning(fields: Dict[str, Any], scoring: Dict[str, Any]) -> str:
    """Trace Python's reasoning for each battleground field.
    
    Returns a human-readable explanation the bot can evaluate and learn from.
    """
    lines = []
    notes_lower = str(fields.get("notes") or "").lower()
    subject_lower = str(fields.get("email_subject") or
                        fields.get("subject") or "").lower()
    full_text_lower = str(fields.get("_full_text") or "").lower()
    text_to_scan = notes_lower + " " + subject_lower + " " + full_text_lower

    # --- 0a. ASSET CLASS ---
    ac_raw = str(fields.get("asset_class") or "None")
    lines.append(f"0a. ASSET CLASS: {ac_raw}")
    _RES_KW = ["multifamily", "residential", "apartment", "garden", "luxury",
               "class a", "class b", "class c", "bfr", "build for rent",
               "townhome", "senior housing", "student housing"]
    ac_lower = ac_raw.lower()
    ac_matches = [kw for kw in _RES_KW if kw in ac_lower]
    if ac_matches:
        lines.append(f"   -> Residential (matched: {ac_matches})")
    elif ac_raw != "None":
        lines.append(f"   -> Non-residential or unrecognized: '{ac_raw}'")
    else:
        lines.append(f"   -> Missing from extraction")
    # Check text for asset class hints
    ac_text_hits = [kw for kw in _RES_KW if kw in text_to_scan]
    if ac_text_hits:
        lines.append(f"   -> Asset class keywords in text: {ac_text_hits}")

    # --- 0b. PRODUCT ---
    prod_raw = str(fields.get("product") or "None").upper()
    lines.append(f"0b. PRODUCT: {prod_raw}")
    if prod_raw in ("MF", "TH", "BFR"):
        lines.append(f"   -> Valid product type")
    elif prod_raw == "NONE":
        lines.append(f"   -> Missing — check text for multifamily/townhome/BFR indicators")
    else:
        lines.append(f"   -> Unrecognized product '{prod_raw}', should be MF, TH, or BFR")
    prod_hints = [kw for kw in ["multifamily", "townhome", "build for rent", "build-for-rent", "bfr"]
                  if kw in text_to_scan]
    if prod_hints:
        lines.append(f"   -> Product keywords in text: {prod_hints}")

    # --- 1. TRANSACTION TYPE ---
    tt_raw = str(fields.get("transaction_type") or "").strip()
    resolved_tt = scoring.get("transaction_type", "")
    lines.append(f"1. TRANSACTION TYPE: {resolved_tt or 'None'}")
    lines.append(f"   Claude extracted: '{tt_raw}'")

    _KNOWN = {"value-add", "value add", "core", "core+", "core-plus",
              "stabilized", "development", "lease-up", "lease up",
              "reposition", "heavy lift", "mark-to-market"}
    if tt_raw.lower() in _KNOWN:
        lines.append(f"   -> Claude value IS a known strategy, trusted it")
    else:
        lines.append(f"   -> Claude value NOT in known strategies, fell through to keyword scan")
        # Show what keywords Python looked for
        mtm_kw = ["mark-to-market", "statutory rent premium", "statutory premium", "untapped value"]
        dev_kw = ["shovel ready", "entitled to build", "ground-up", "ground up", "forward purchase"]
        va_kw = ["value-add", "value add", "renovation opportunity", "classic condition",
                 "reposition", "interior upgrade", "partially renovated", "unrenovated",
                 "deferred maintenance", "renovation roadmap", "renovation upside"]
        core_kw = ["stabilized", "core plus", "fully occupied", "fully leased"]

        found = []
        for kw in mtm_kw + dev_kw + va_kw + core_kw:
            if kw in text_to_scan:
                found.append(kw)
        if found:
            lines.append(f"   -> Keywords found in notes/subject: {found}")
        else:
            lines.append(f"   -> No strategy keywords found in notes/subject")

    # --- 2. STRATEGY (same as transaction_type for scoring purposes) ---
    strat_sc = scoring.get("residential_strategy_score", 0)
    lines.append(f"2. STRATEGY SCORE: {strat_sc}")
    rtt = str(resolved_tt or "").lower()
    if "development" in rtt or "lease-up" in rtt or "heavy lift" in rtt:
        lines.append(f"   -> Dev/Lease-up = 0.5 pts")
    elif "mark-to-market" in rtt:
        lines.append(f"   -> Mark-to-Market = 0.2 pts")
    elif "core" in rtt or "stabilized" in rtt:
        lines.append(f"   -> Core/Stabilized = 0.3 pts")
    elif "value" in rtt or "reposition" in rtt:
        lines.append(f"   -> Value-add = 0.2 pts")
    else:
        lines.append(f"   -> No recognized strategy = 0 pts")

    # --- 3. UNITS ---
    units_raw = fields.get("units")
    units_sc = scoring.get("residential_units_score", 0)
    lines.append(f"3. UNITS: {units_raw} -> score={units_sc}")
    if units_raw is None:
        lines.append(f"   -> Missing from extraction, 0 pts")
    elif 100 <= (int(units_raw) if units_raw else 0) <= 500:
        lines.append(f"   -> Sweet spot (100-500) = 2 pts")
    else:
        lines.append(f"   -> Outside sweet spot")

    # --- 4. VINTAGE ---
    vintage = fields.get("vintage")
    vint_sc = scoring.get("residential_vintage_score", 0)
    lines.append(f"4. VINTAGE: {vintage} -> score={vint_sc}")
    if vintage is None:
        lines.append(f"   -> Missing from extraction, 0 pts")
    else:
        v = int(vintage) if vintage else 0
        if v >= 2000:
            lines.append(f"   -> Post-2000 = 1.0 pts")
        elif v >= 1980:
            lines.append(f"   -> 1980-1999 = 0.5 pts")
        else:
            lines.append(f"   -> Pre-1980 = 0 pts")

    # --- 5. RENOVATED ---
    renovated = fields.get("renovated")
    reno_year = fields.get("renovation_year")
    reno_boost = scoring.get("residential_reno_boost", 0)
    lines.append(f"5. RENOVATED: {renovated} | RENOVATION YEAR: {reno_year} -> boost={reno_boost}")
    if renovated is True and reno_year:
        try:
            ry = int(reno_year)
            if ry >= 2022:
                lines.append(f"   -> Recent reno (>= 2022) = +0.25 boost")
            else:
                lines.append(f"   -> Reno year {ry} < 2022, no boost")
        except:
            lines.append(f"   -> Could not parse reno year")
    elif renovated is True:
        lines.append(f"   -> Marked renovated but no year, no boost")
    else:
        lines.append(f"   -> Not marked as renovated")
    # Show what reno-related text exists
    reno_words = [w for w in ["renovated", "renovation", "upgraded", "remodel", "rehab", "interior"]
                  if w in text_to_scan]
    if reno_words:
        lines.append(f"   -> Reno keywords in text: {reno_words}")

    # --- 7. NOTES SCORING ---
    notes_boost = scoring.get("residential_notes_boost", 0)
    notes_reason = scoring.get("notes_score_reason", "")
    claude_signals = fields.get("positive_signals", [])
    lines.append(f"7. NOTES SCORING: {notes_boost} pts")
    lines.append(f"   Reason: {notes_reason or 'no signals detected'}")
    lines.append(f"   Claude positive_signals: {claude_signals}")
    # Show what Python actually triggered (from scoring output, not re-derived)
    all_possible = ["strong schools", "limited supply", "assumable debt",
                    "rent upside", "institutional sponsor", "discount to replacement"]
    reason_lower = notes_reason.lower() if notes_reason else ""
    triggered = [s for s in all_possible if s.replace(" ", "") in reason_lower.replace(" ", "")]
    not_triggered = [s for s in all_possible if s not in triggered]
    lines.append(f"   Triggered: {triggered or 'none'}")
    lines.append(f"   Not triggered: {not_triggered}")
    # Negatives
    neg = []
    if "crime" in notes_lower: neg.append("crime")
    if "collection" in notes_lower and "issue" in notes_lower: neg.append("collection issue")
    if "insurance shock" in notes_lower: neg.append("insurance shock")
    if neg:
        lines.append(f"   NEGATIVE BLOCKERS: {neg}")

    # --- 8. SCORE MISSING INPUTS ---
    missing = scoring.get("score_missing_inputs", "")
    lines.append(f"8. MISSING INPUTS: {missing or 'none'}")
    if missing:
        lines.append(f"   -> These fields were not extracted, costing score points")

    return "\n".join(lines)


def send_deal_review(fields: Dict[str, Any], scoring: Dict[str, Any]) -> int:
    """Send a deal review to Telegram with AI guesses. Returns message_id."""
    global _last_review

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return 0

    uid = fields.get("uid", "?")
    name = fields.get("deal_name", "Unknown")
    city = fields.get("city", "")
    state = fields.get("state", "")
    msa = fields.get("msa", "")
    tt = scoring.get("transaction_type", "")
    score = scoring.get("score", 0)
    breakdown = scoring.get("score_breakdown", "")
    notes_reason = scoring.get("notes_score_reason", "")
    notes = fields.get("notes", "")[:800]
    subject = fields.get("email_subject", fields.get("subject", ""))
    units = fields.get("units")
    vintage = fields.get("vintage")
    rent = fields.get("avg_rent_monthly")
    missing = scoring.get("score_missing_inputs", "")
    pos_signals = fields.get("positive_signals", [])

    # All extracted metadata for review
    price = fields.get("guidance_price")
    cap_rate = fields.get("in_place_cap_rate")
    occupancy = fields.get("occupancy")
    reno_year = fields.get("renovation_year")
    cash_yield = fields.get("going_in_cash_yield")
    dtr = fields.get("discount_to_replacement")
    asset_class = fields.get("asset_class")

    # Build battleground reasoning trace
    bg_reasoning = battleground_reasoning(fields, scoring)

    # Build deal summary — pipeline output + Python reasoning for battleground fields
    deal_summary = (
        f"PIPELINE OUTPUT (fact — do not re-derive):\n"
        f"Deal: {name} (uid={uid})\n"
        f"Subject: {subject}\n"
        f"Location: {city}, {state} | MSA: {msa}\n"
        f"Asset class: {asset_class} | Product: {fields.get('product') or 'None'}\n"
        f"Score: {score} | {breakdown}\n"
        f"\n--- PYTHON REASONING FOR BATTLEGROUND FIELDS ---\n"
        f"Below is exactly HOW the Python pipeline decided each battleground field.\n"
        f"Your job: evaluate this reasoning. Is Python right or wrong? Why?\n\n"
        f"{bg_reasoning}\n"
        f"\n--- DEAL NOTES (Claude summary) ---\n"
        f"{notes}\n"
        f"\n--- FULL SOURCE TEXT (scraped + OCR — this is EXACTLY what Python scored against) ---\n"
        f"{str(fields.get('_full_text') or '')}"
    )

    # Get AI assessment with learned patterns
    past = _load_past_corrections()
    user_msg = deal_summary
    if past:
        user_msg += f"\n\n--- LEARNED PATTERNS (from past corrections) ---\n{past}"
    ai_review = _call_claude(_SYSTEM_PROMPT, [{"role": "user", "content": user_msg}])
    accuracy = _get_accuracy()

    # Parse PREDICTED_FIELDS from AI review
    pf_match = re.search(r"\*{0,2}PREDICTED_FIELDS\*{0,2}:\s*(.+?)$", ai_review, re.MULTILINE)
    predicted_fields = []
    if pf_match:
        raw = pf_match.group(1).strip().lower()
        if raw != "none":
            predicted_fields = [f.strip() for f in raw.split(",") if f.strip()]

    # Parse FIELD_CHANGES from initial review — store as pending so thumbs-up applies them
    initial_fc_match = re.search(r"\*{0,2}FIELD_CHANGES:?\*{0,2}:?\s*(\{.*\})", ai_review)
    initial_pending = {}
    if initial_fc_match:
        try:
            initial_pending = json.loads(initial_fc_match.group(1))
            print(f"[qa-bot] Parsed initial FIELD_CHANGES: {initial_pending}", flush=True)
        except json.JSONDecodeError:
            pass

    # Format Telegram message — present data as fact, show bot's predictions
    if predicted_fields:
        pred_line = f"🔮 Predictions: {_tg_escape(', '.join(predicted_fields))}"
    else:
        pred_line = "🔮 No predictions yet \\— still learning"
    lines = [
        f"🏢 *{_tg_escape(name)}* \\| uid\\={_tg_escape(str(uid))}",
        f"📍 {_tg_escape(city)}, {_tg_escape(state)} \\({_tg_escape(msa)}\\)",
        f"💰 Score: {_tg_escape(str(score))} \\| {_tg_escape(str(tt))} \\| {_tg_escape(str(units))} units",
        f"📊 {_tg_escape(breakdown)}",
        "",
        f"🤖 *Bot Review:*",
        _tg_escape(ai_review),
        "",
        pred_line,
        f"📈 {_tg_escape(accuracy)}",
        "",
        "See any issues\\?",
    ]

    msg_id = _tg_send("\n".join(lines))

    # Compute bot's proposed score (what score would be if bot's changes applied)
    bot_proposed_score = None
    bot_proposed_breakdown = None
    if initial_pending:
        try:
            from email_watcher import compute_bluerock_score
            proposed_fields = dict(fields)
            proposed_fields.update(initial_pending)
            is_scraper = (uid or 0) >= 50000
            proposed_scoring = compute_bluerock_score(proposed_fields, training_mode=is_scraper)
            bot_proposed_score = proposed_scoring.get("score")
            bot_proposed_breakdown = proposed_scoring.get("score_breakdown")
        except Exception as e:
            print(f"[qa-bot] Bot proposed rescore failed: {e}", flush=True)

    _last_review = {
        "uid": uid,
        "deal_name": name,
        "fields": fields,
        "scoring": scoring,
        "msg_id": msg_id,
        "deal_summary": deal_summary,
        "ai_review": ai_review,
        "_predicted_fields": predicted_fields,
        "_pending_changes": initial_pending,
        "bot_proposed_score": bot_proposed_score,
        "bot_proposed_breakdown": bot_proposed_breakdown,
        "_conversation": [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": ai_review},
        ],
    }
    _save_review_state()

    print(f"[qa-bot] Sent review for uid={uid} {name} (msg_id={msg_id})", flush=True)
    return msg_id


def process_reply(text: str):
    """Process a user reply to a deal review.
    
    Routes through Claude to understand intent:
    - GRADE_GOOD / GRADE_BAD → log a grade
    - CORRECTION → log correction, reason about it, ask for grade
    - QUESTION → answer the question using deal context
    """
    global _last_review

    # Always reload from disk — send_deal_review may have been called
    # from email_watcher (a different process)
    _load_review_state()

    if not _last_review:
        _tg_send(_tg_escape("No active deal review to reply to."))
        return

    uid = _last_review["uid"]
    name = _last_review["deal_name"]
    text_lower = text.strip().lower()

    # Quick-match obvious grades
    pending_cat = _last_review.get("_pending_category", "overall")
    if text_lower in ("✅", "👍", "good", "correct", "looks good", "lg", "ok", "y"):
        _save_grade(uid, name, correct=True, category=pending_cat)

        # Compare bot predictions vs actual user corrections
        predicted = set(_last_review.get("_predicted_fields", []))
        pending = _last_review.get("_pending_changes") or {}
        corrected = set(pending.keys())

        pred_correct = predicted & corrected        # bot predicted, user confirmed
        pred_wrong = predicted - corrected          # bot predicted, but pipeline was right (false alarm)
        corr_missed = corrected - predicted         # user corrected, bot didn't predict

        # Capture first-pass score before corrections
        first_scoring = _last_review.get("scoring", {})
        grade_ctx = {
            "predicted_fields": sorted(predicted),
            "corrected_fields": sorted(corrected),
            "num_corrections": len(corrected),
            "field_changes": pending,
            "predictions_correct": sorted(pred_correct),
            "predictions_wrong": sorted(pred_wrong),
            "corrections_missed": sorted(corr_missed),
            "first_pass_score": first_scoring.get("score"),
            "first_pass_breakdown": first_scoring.get("score_breakdown"),
            "bot_proposed_score": _last_review.get("bot_proposed_score"),
            "bot_proposed_breakdown": _last_review.get("bot_proposed_breakdown"),
        }

        # Compute final score in-memory (works for ALL deals, even scraper deals not in DB)
        # Strip stale scoring output keys first — fields dict has scoring merged in
        _SCORING_OUTPUT_KEYS = {
            "score", "score_breakdown", "score_missing_inputs", "score_version",
            "is_residential", "is_industrial", "residential_gated",
            "residential_gating_reason", "residential_interest_score",
            "residential_geo_score", "residential_units_score",
            "residential_price_score", "residential_rent_score",
            "residential_vintage_score", "residential_returns_score",
            "residential_strategy_score", "residential_notes_boost",
            "residential_reno_boost", "notes_score_reason",
            "industrial_interest_score",
        }
        if pending:
            try:
                from email_watcher import compute_bluerock_score
                rescored_fields = {k: v for k, v in
                                   ((_last_review.get("fields") or {}).items())
                                   if k not in _SCORING_OUTPUT_KEYS}
                rescored_fields.update(pending)
                is_scraper = (uid or 0) >= 50000
                new_scoring = compute_bluerock_score(rescored_fields, training_mode=is_scraper)
                grade_ctx["final_score"] = new_scoring.get("score")
                grade_ctx["final_breakdown"] = new_scoring.get("score_breakdown")
            except Exception as e:
                print(f"[qa-bot] In-memory rescore failed: {e}", flush=True)

        # Auto-apply pending corrections to the database
        if pending:
            result = _apply_corrections_to_db(uid, name, pending)

            _save_correction(uid, name, "_deal_final_grade", "",
                             json.dumps(sorted(corrected)),
                             json.dumps(grade_ctx))

            # Build prediction accuracy line
            grade_parts = []
            if pred_correct:
                grade_parts.append(f"✅ Predicted correctly: {', '.join(sorted(pred_correct))}")
            if pred_wrong:
                grade_parts.append(f"❌ False alarms: {', '.join(sorted(pred_wrong))}")
            if corr_missed:
                grade_parts.append(f"🔴 Missed: {', '.join(sorted(corr_missed))}")
            if not predicted:
                grade_parts.append("🔮 No predictions made")
            grade_line = "\n".join(grade_parts)

            _tg_send(
                f"✅ Applied corrections to *{_tg_escape(name)}*:\n"
                f"{_tg_escape(result)}\n\n"
                f"🎯 *Prediction Report:*\n{_tg_escape(grade_line)}\n"
                f"📈 {_tg_escape(_get_accuracy())}"
            )
        else:
            # No corrections = pipeline got it right
            _save_correction(uid, name, "_deal_final_grade", "",
                             "[]",
                             json.dumps(grade_ctx))

            grade_parts = []
            if pred_wrong:
                grade_parts.append(f"❌ False alarms: {', '.join(sorted(pred_wrong))}")
            else:
                grade_parts.append("✅ No false predictions")
            grade_line = "\n".join(grade_parts)

            _tg_send(
                f"✅ Noted \\— *{_tg_escape(name)}* looks good\\.\n\n"
                f"🎯 *Prediction Report:*\n{_tg_escape(grade_line)}\n"
                f"📈 {_tg_escape(_get_accuracy())}"
            )
        _last_review = {}
        _save_review_state()
        return

    if text_lower in ("👎", "bad", "wrong", "no", "n"):
        _save_grade(uid, name, correct=False, category=pending_cat)
        acc = _get_accuracy()
        _tg_send(
            f"👎 Got it \\— tell me what's wrong and I'll try to figure out why\\."
            f"\n📈 {_tg_escape(acc)}"
        )
        return

    # Build conversation with full history
    past = _load_past_corrections()
    pending = _last_review.get("_pending_changes", {})
    pending_str = json.dumps(pending) if pending else "none"
    conv_system = (
        _SYSTEM_PROMPT + "\n\n"
        "--- CONVERSATION MODE ---\n"
        "You are now in a conversation with the user about this deal. "
        "The full conversation history is provided as messages. "
        "Remember everything the user has told you in this conversation.\n\n"
        f"CURRENT PENDING CHANGES (will be applied on thumbs-up): {pending_str}\n\n"
        "Determine the user's intent and respond:\n\n"
        "1. If they're asking a QUESTION — answer it using the deal context. "
        "Be specific and thorough.\n\n"
        "2. If they're providing a CORRECTION or REFINING existing corrections — "
        "start your response with exactly 'CORRECTION:' on the first line, then "
        "categorize as EXTRACTION, SCORING, or MISSING_INPUTS.\n"
        "IMPORTANT: ALWAYS output FIELD_CHANGES: with the COMPLETE set of corrections "
        "that should be applied. This REPLACES all pending changes entirely.\n"
        "Valid fields: deal_name, city, state, units, vintage, avg_rent_monthly, "
        "guidance_price, in_place_cap_rate, occupancy, transaction_type, asset_class, product, "
        "going_in_cash_yield, discount_to_replacement, renovation_year, renovated, "
        "positive_signals. Use correct types (numbers as numbers, not strings).\n"
        'Example: FIELD_CHANGES: {"vintage": 2015, "transaction_type": "Core"}\n'
        'If user narrows signals, output only the confirmed ones: '
        'FIELD_CHANGES: {"positive_signals": ["below-market rents"]}\n\n'
        "3. If they're confirming everything is correct (no changes needed) — "
        "respond naturally. Output FIELD_CHANGES: {} to clear pending changes.\n\n"
        "Be concise. No fluff."
    )
    if past:
        conv_system += f"\n\nPAST CORRECTIONS (learn from these):\n{past}"

    # Get conversation history, append new user message
    conversation = list(_last_review.get("_conversation", []))
    conversation.append({"role": "user", "content": text.strip()})

    response = _call_claude(conv_system, conversation)

    # Append assistant response to history and persist EVERY turn
    conversation.append({"role": "assistant", "content": response})
    _last_review["_conversation"] = conversation
    _save_review_state()

    # Parse FIELD_CHANGES from ANY response (not just ones starting with CORRECTION:)
    fc_match = re.search(r"\*{0,2}FIELD_CHANGES:?\*{0,2}:?\s*(\{.*\})", response)
    has_correction = (response.strip().upper().startswith("CORRECTION:") or fc_match)

    if has_correction:
        correction_text = text.strip()
        context = json.dumps(
            {k: _last_review.get("fields", {}).get(k) for k in
             ["deal_name", "state", "msa", "notes", "email_subject",
              "asset_class", "units", "vintage"]},
            default=str
        )

        # Parse category
        category = "overall"
        for cat in ["extraction", "scoring", "missing_inputs"]:
            if cat.upper() in response.upper()[:200]:
                category = cat
                break

        _save_correction(uid, name, f"user_correction_{category}", "",
                         correction_text, context)
        _last_review["_pending_category"] = category

        if fc_match:
            try:
                pending_changes = json.loads(fc_match.group(1))
                # REPLACE pending changes entirely (not merge) so user refinements override
                _last_review["_pending_changes"] = pending_changes
            except json.JSONDecodeError:
                pass

        _save_review_state()

        cat_emoji = {"extraction": "🔍", "scoring": "📊", "missing_inputs": "📭"}
        changes_str = ""
        if _last_review.get("_pending_changes"):
            changes_str = f"\n📦 Pending changes: {_tg_escape(json.dumps(_last_review['_pending_changes']))}"
        lines = [
            f"📝 Correction logged for *{_tg_escape(name)}*",
            f"{cat_emoji.get(category, '📋')} Category: *{_tg_escape(category.replace('_', ' ').title())}*",
            changes_str,
            "",
            _tg_escape(response),
            "",
            "👍 to apply changes or 👎 if wrong",
        ]
        _tg_send("\n".join(lines))
    else:
        # Question or general chat — but still check for FIELD_CHANGES (e.g., clearing pending)
        fc_chat = re.search(r"\*{0,2}FIELD_CHANGES:?\*{0,2}:?\s*(\{.*\})", response)
        if fc_chat:
            try:
                _last_review["_pending_changes"] = json.loads(fc_chat.group(1))
                _save_review_state()
            except json.JSONDecodeError:
                pass
        _tg_send(_tg_escape(response))


# ---------------------------------------------------------------------------
# Polling loop (runs alongside email_watcher)
# ---------------------------------------------------------------------------

def poll_loop():
    """Poll Telegram for replies to deal reviews."""
    _load_review_state()
    print("[qa-bot] Starting Telegram poll loop...", flush=True)
    offset = 0

    # Flush old updates
    old = _tg_get_updates(offset=0, timeout=0)
    if old:
        offset = old[-1]["update_id"] + 1
        print(f"[qa-bot] Skipped {len(old)} old updates", flush=True)

    while True:
        try:
            updates = _tg_get_updates(offset=offset, timeout=30)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = msg.get("chat", {}).get("id", 0)
                text = msg.get("text", "")

                if chat_id != TELEGRAM_CHAT_ID or not text:
                    continue

                if text.startswith("/accuracy"):
                    acc = _get_accuracy()
                    _tg_send(f"📈 Bot accuracy: {_tg_escape(acc)}")
                elif text.startswith("/review"):
                    if _last_review:
                        _tg_send(_tg_escape(
                            f"Active review: uid={_last_review['uid']} "
                            f"{_last_review['deal_name']}"
                        ))
                    else:
                        _tg_send(_tg_escape("No active deal review."))
                else:
                    process_reply(text)
        except KeyboardInterrupt:
            print("[qa-bot] Stopped.", flush=True)
            break
        except Exception as e:
            print(f"[qa-bot] Poll error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    poll_loop()
