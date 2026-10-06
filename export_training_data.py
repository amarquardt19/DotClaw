#!/usr/bin/env python3
"""Export scoring_corrections DB → JSONL training pairs for Bedrock fine-tuning.

Generates two types of training examples:
1. EXTRACTION: deal text → correct field values (with user corrections applied)
2. SCORING: extracted fields → correct transaction_type, positive_signals, strategy

Output: training_data.jsonl  (Anthropic messages format for Bedrock fine-tuning)

Usage:
    python export_training_data.py              # export + stats
    python export_training_data.py --stats      # stats only
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")


def _get_db():
    import pymssql
    return pymssql.connect(
        server=os.getenv("AZURE_SQL_SERVER"),
        user=os.getenv("AZURE_SQL_USER"),
        password=os.getenv("AZURE_SQL_PASSWORD"),
        database=os.getenv("AZURE_SQL_DATABASE"),
    )


EXTRACTION_SYSTEM = (
    "You are a commercial real estate deal extraction assistant. "
    "Given deal text from an offering memorandum or broker email, extract structured fields. "
    "Return valid JSON with these fields: deal_name, city, state, units, vintage, "
    "avg_rent_monthly, guidance_price, in_place_cap_rate, occupancy, transaction_type, "
    "asset_class, product, going_in_cash_yield, discount_to_replacement, "
    "renovation_year, renovated, positive_signals, negative_signals. "
    "For transaction_type, use the ACTUAL strategy (Value-add, Core, Mark-to-Market, "
    "Development, Distressed, etc.), not generic labels like 'Investment Sale'. "
    "For positive_signals, only include signals with explicit text evidence. "
    "Return empty array [] if none found."
)

SCORING_SYSTEM = (
    "You are a multifamily real estate scoring assistant for a value-add focused fund. "
    "Given extracted deal fields, evaluate the deal and identify: "
    "1) The correct transaction_type/strategy "
    "2) Any positive_signals present (strong school district, limited new supply, "
    "assumable debt, discount to replacement, below-market rents, institutional sponsor) "
    "3) Any negative_signals (crime, collection issues, insurance shock, hoa litigation) "
    "Return JSON with corrected fields only."
)


def load_deals():
    """Load all graded deals with their corrections and context from DB."""
    c = _get_db()
    cur = c.cursor()

    cur.execute("""
        SELECT id, deal_name, field_name, old_value, new_value, deal_context
        FROM scoring_corrections
        ORDER BY deal_name, id
    """)

    deals = {}
    for row in cur.fetchall():
        _id, name, field, old_val, new_val, ctx_raw = row
        if name not in deals:
            deals[name] = {"corrections": [], "grade": None, "context": {}}

        ctx = json.loads(ctx_raw) if ctx_raw else {}

        if field == "_deal_final_grade":
            deals[name]["grade"] = ctx
        elif field in ("user_correction_extraction", "user_correction_scoring"):
            # These have deal context (notes, fields, etc.)
            if ctx and len(ctx) > 2:
                deals[name]["context"].update(ctx)
            deals[name]["corrections"].append({
                "field": field,
                "old": old_val,
                "new": new_val,
            })

    c.close()
    return deals


def build_extraction_pair(name, deal):
    """Build an extraction training example: deal text → corrected fields."""
    ctx = deal.get("context", {})
    grade = deal.get("grade", {})
    changes = grade.get("field_changes", {}) if grade else {}

    notes = ctx.get("notes", "")
    if not notes:
        return None  # Can't train extraction without deal text

    # Build the "correct" output by starting with extracted fields and applying corrections
    output_fields = {}
    for k in ["deal_name", "city", "state", "units", "vintage", "avg_rent_monthly",
              "guidance_price", "in_place_cap_rate", "occupancy", "transaction_type",
              "asset_class", "product", "going_in_cash_yield", "discount_to_replacement",
              "renovation_year", "renovated", "positive_signals", "negative_signals"]:
        if k in ctx:
            output_fields[k] = ctx[k]

    # Apply user corrections (ground truth)
    output_fields.update(changes)

    # Build user message (the deal text)
    user_msg = f"Extract deal metadata from this text:\n\n{notes}"
    if ctx.get("email_subject"):
        user_msg = f"Subject: {ctx['email_subject']}\n\n" + user_msg

    return {
        "system": EXTRACTION_SYSTEM,
        "messages": [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": json.dumps(output_fields, indent=2)},
        ],
    }


def build_scoring_pair(name, deal):
    """Build a scoring training example: fields → correct strategy + signals."""
    ctx = deal.get("context", {})
    grade = deal.get("grade", {}) or {}
    changes = grade.get("field_changes", {})

    if not changes:
        return None  # No corrections = nothing to train on (model got it right)

    # Build input: the extracted fields BEFORE correction
    input_fields = {}
    for k in ["deal_name", "state", "msa", "units", "vintage", "asset_class",
              "transaction_type", "avg_rent_monthly", "guidance_price", "occupancy"]:
        if k in ctx:
            input_fields[k] = ctx[k]
    if ctx.get("notes"):
        input_fields["notes_excerpt"] = ctx["notes"][:500]

    user_msg = f"Evaluate this deal and identify any corrections needed:\n\n{json.dumps(input_fields, indent=2)}"

    return {
        "system": SCORING_SYSTEM,
        "messages": [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": json.dumps(changes, indent=2)},
        ],
    }


def export(out_path="training_data.jsonl"):
    """Export all training pairs to JSONL."""
    deals = load_deals()

    pairs = []
    stats = {
        "total_deals": len(deals),
        "deals_with_corrections": 0,
        "deals_with_context": 0,
        "extraction_pairs": 0,
        "scoring_pairs": 0,
        "total_training_examples": 0,
    }

    for name, deal in sorted(deals.items()):
        grade = deal.get("grade", {}) or {}
        changes = grade.get("field_changes", {})

        if changes:
            stats["deals_with_corrections"] += 1
        if deal.get("context"):
            stats["deals_with_context"] += 1

        # Extraction pair
        ext_pair = build_extraction_pair(name, deal)
        if ext_pair:
            pairs.append(ext_pair)
            stats["extraction_pairs"] += 1

        # Scoring pair (only if corrections exist)
        score_pair = build_scoring_pair(name, deal)
        if score_pair:
            pairs.append(score_pair)
            stats["scoring_pairs"] += 1

    stats["total_training_examples"] = len(pairs)

    # Write JSONL
    out = Path(out_path)
    with out.open("w") as f:
        for pair in pairs:
            f.write(json.dumps(pair) + "\n")

    return stats, out


def print_stats(stats):
    """Print training data statistics and progress toward fine-tuning."""
    target = 200
    pct = min(100, int(stats["total_training_examples"] / target * 100))
    bar_len = 30
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    print("\n" + "=" * 50)
    print("  FINE-TUNING TRAINING DATA STATUS")
    print("=" * 50)
    print(f"  Graded deals:          {stats['total_deals']}")
    print(f"  Deals w/ corrections:  {stats['deals_with_corrections']}")
    print(f"  Deals w/ context:      {stats['deals_with_context']}")
    print(f"  Extraction pairs:      {stats['extraction_pairs']}")
    print(f"  Scoring pairs:         {stats['scoring_pairs']}")
    print(f"  Total training pairs:  {stats['total_training_examples']}")
    print()
    print(f"  Progress to fine-tune: [{bar}] {pct}%")
    print(f"  Target: {target} examples | Need: {max(0, target - stats['total_training_examples'])} more")
    print()

    if stats["total_training_examples"] < 50:
        print("  📊 Status: COLLECTING — keep reviewing deals in Telegram")
    elif stats["total_training_examples"] < 100:
        print("  📊 Status: GETTING CLOSE — could attempt a test fine-tune")
    elif stats["total_training_examples"] < 200:
        print("  📊 Status: READY FOR TEST — run first fine-tune on Bedrock")
    else:
        print("  📊 Status: READY FOR PRODUCTION — full fine-tune viable")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export training data for fine-tuning")
    parser.add_argument("--stats", action="store_true", help="Show stats only, don't export")
    parser.add_argument("--out", default="training_data.jsonl", help="Output JSONL path")
    args = parser.parse_args()

    if args.stats:
        deals = load_deals()
        stats = {"total_deals": len(deals), "deals_with_corrections": 0,
                 "deals_with_context": 0, "extraction_pairs": 0,
                 "scoring_pairs": 0, "total_training_examples": 0}
        for name, deal in deals.items():
            g = deal.get("grade", {}) or {}
            if g.get("field_changes"): stats["deals_with_corrections"] += 1
            if deal.get("context"): stats["deals_with_context"] += 1
            if build_extraction_pair(name, deal): stats["extraction_pairs"] += 1
            if build_scoring_pair(name, deal): stats["scoring_pairs"] += 1
        stats["total_training_examples"] = stats["extraction_pairs"] + stats["scoring_pairs"]
        print_stats(stats)
    else:
        stats, out = export(args.out)
        print_stats(stats)
        print(f"  Exported to: {out.resolve()}\n")
