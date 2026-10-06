import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()


def _run(cmd: List[str], *, timeout_s: Optional[int] = None) -> str:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


def list_inbox_envelopes(*, folder: str, page_size: int, config_path: Optional[str]) -> List[Dict[str, Any]]:
    cmd = [
        "himalaya",
        "envelope",
        "list",
        "--folder",
        folder,
        "--page-size",
        str(page_size),
        "--page",
        "1",
        "--output",
        "json",
        "order by date desc",
    ]
    if config_path:
        cmd.extend(["--config", config_path])

    out = _run(cmd, timeout_s=60).strip()
    if not out:
        return []
    data = json.loads(out)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected envelope list JSON type: {type(data)}")
    return data


def read_message_text(*, msg_id: str, folder: str, config_path: Optional[str], timeout_s: int) -> str:
    cmd = ["himalaya", "message", "read", msg_id, "--folder", folder, "--output", "json"]
    if config_path:
        cmd.extend(["--config", config_path])
    out = _run(cmd, timeout_s=timeout_s).strip()
    if not out:
        return ""
    # message read --output json returns a JSON string
    data = json.loads(out)
    if isinstance(data, str):
        return data
    # fallback
    return str(data)


_RE_UNITS = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d{2,4})\s*[- ]?\s*(?:units?|unit)\b", re.IGNORECASE)
_RE_SF = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\s*SF\b", re.IGNORECASE)
_RE_YEAR = re.compile(r"\b(?:YOC|Year Built|Built)\s*[:\-]?\s*(19\d{2}|20\d{2})\b", re.IGNORECASE)
_RE_OCC = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*%\s*(?:occ|occupied|occupancy)\b", re.IGNORECASE)
_RE_RENT_CONTEXT = re.compile(
    r"\b(?:rent|rents|avg\s*rent|average\s*rent|market\s*rent|in\s*place\s*rent)\b[^\n\r$]{0,40}\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_RE_CITY_STATE = re.compile(r"\b([A-Z][A-Za-z .']+),\s*([A-Z]{2})\b")


def _first_match_int(rx: re.Pattern[str], text: str) -> Optional[int]:
    m = rx.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except Exception:
        return None


def _first_match_str(rx: re.Pattern[str], text: str) -> Optional[str]:
    m = rx.search(text)
    if not m:
        return None
    return m.group(1)


def _clean_subject(subject: str) -> str:
    s = subject.strip()
    s = re.sub(r"^(?:\s*(?:Fwd|FW|RE)\s*:\s*)+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s)
    return s


def _subject_parts(subject: str) -> List[str]:
    s = _clean_subject(subject)
    if "|" in s:
        return [p.strip() for p in s.split("|") if p.strip()]
    return [s]


def _extract_units_prefer_subject(subject: str, body: str) -> Optional[int]:
    s = _clean_subject(subject)
    u = _first_match_int(_RE_UNITS, s)
    if u is not None:
        return u
    return _first_match_int(_RE_UNITS, body)


def _extract_year_prefer_subject(subject: str, body: str) -> Optional[int]:
    s = _clean_subject(subject)
    y = _first_match_int(_RE_YEAR, s)
    if y is not None:
        return y
    return _first_match_int(_RE_YEAR, body)


def _extract_market_prefer_subject(subject: str, body: str) -> str:
    for t in (_clean_subject(subject), body[:4000]):
        m = _RE_CITY_STATE.search(t)
        if m:
            city = m.group(1).strip()
            # Avoid junk captures like "Units in Alpharetta, GA"
            city = re.sub(r"^Units\s+in\s+", "", city, flags=re.IGNORECASE).strip()
            return f"{city}, {m.group(2)}"
    return ""


def guess_property_and_market(subject: str, body: str) -> Tuple[str, str]:
    parts = _subject_parts(subject)

    # Prefer a segment that looks like a property name (and not just a call-to-action)
    stopwords = [
        "call for offers",
        "offering memorandum",
        "offering materials",
        "now touring",
        "offers due",
        "new listing",
        "stabilized",
        "value-add",
    ]
    keywords = [
        "apart",
        "commons",
        "village",
        "creek",
        "point",
        "pointe",
        "park",
        "heights",
        "townes",
        "portfolio",
        "belgate",
        "wellen",
        "riverside",
        "kenmore",
        "grove",
        "sands",
        "tortuga",
        "sweetwater",
        "sole",
    ]

    candidates: List[str] = []
    for p in parts:
        pl = p.lower()
        if any(sw in pl for sw in stopwords):
            continue
        if any(k in pl for k in keywords) and len(p) <= 110:
            candidates.append(p)

    prop = candidates[0] if candidates else (parts[-1] if parts else _clean_subject(subject))
    prop = re.sub(r"\s{2,}", " ", prop).strip()
    if len(prop) > 90:
        prop = prop[:89] + "…"

    market = _extract_market_prefer_subject(subject, body)
    return prop or "(unknown)", market


def guess_asset_type(subject: str, body: str) -> str:
    # Use subject first; forwarded chains can contain unrelated terms.
    t_subj = _clean_subject(subject).lower()
    t_body = body.lower()
    t = t_subj + "\n" + t_body

    if "industrial" in t_subj or "industrial" in t_body:
        return "Industrial"
    if "btr" in t_subj or "build-to-rent" in t_subj or "townhome" in t_subj:
        return "BTR"
    if "redevelopment" in t_subj:
        return "Redevelopment"
    if "multifamily" in t_subj or "apart" in t_subj:
        return "Multifamily"

    # Only tag senior if subject indicates it (avoid false positives from forwarded disclaimers)
    if "55+" in t_subj or "active adult" in t_subj or "senior" in t_subj:
        return "Senior / Active Adult"

    # Fallback checks
    if "industrial" in t:
        return "Industrial"
    if "btr" in t or "build-to-rent" in t or "townhome" in t:
        return "BTR"
    if "multifamily" in t or "apart" in t:
        return "Multifamily"
    if "redevelopment" in t:
        return "Redevelopment"
    return "Unknown"


def extract_fields(subject: str, body: str) -> Dict[str, Any]:
    combined = subject + "\n" + body

    units = _extract_units_prefer_subject(subject, body)
    sf = _first_match_str(_RE_SF, combined)
    year = _extract_year_prefer_subject(subject, body)
    occ = _first_match_str(_RE_OCC, combined)

    # Rent: require rent context near the dollar amount and keep it within a sane range.
    rent_val: Optional[str] = None
    m = _RE_RENT_CONTEXT.search(combined)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
        except Exception:
            v = 0
        if 300 <= v <= 15000:
            rent_val = f"${m.group(1)}"

    return {
        "units": units,
        "sf": sf,
        "year_built": year,
        "occupancy": (f"{occ}%" if occ else ""),
        "rent": rent_val or "",
    }


def write_reports(rows: List[Dict[str, Any]], out_csv: str, out_md: str) -> None:
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)

    cols = [
        "id",
        "date",
        "property",
        "market",
        "asset_type",
        "units",
        "year_built",
        "occupancy",
        "rent",
        "has_attachment",
        "status",
        "error",
        "subject",
    ]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("# INBOX Deal/Opportunity Table\n\n")
        f.write(f"Generated: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n\n")
        f.write(f"Rows: {len(rows)}\n\n")
        f.write("| Property | Market | Type | Units | Year | Occ | Rent | Attach? | Status | ID |\n")
        f.write("|---|---|---|---:|---:|---:|---:|---:|---|---:|\n")
        for r in rows:
            attach = "yes" if r.get("has_attachment") else ""
            units = r.get("units") or ""
            year = r.get("year_built") or ""
            occ = r.get("occupancy") or ""
            rent = r.get("rent") or ""
            status = r.get("status") or ""
            f.write(
                f"| {r.get('property','')} | {r.get('market','')} | {r.get('asset_type','')} | {units} | {year} | {occ} | {rent} | {attach} | {status} | {r.get('id','')} |\n"
            )


def extract_fields_llm(*, subject: str, body: str) -> Dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_EXTRACT_MODEL", "claude-sonnet-4-20250514")
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY")

    try:
        from anthropic import Anthropic
    except Exception as e:
        raise RuntimeError(f"Anthropic SDK not available: {e}")

    prompt = (
        "Extract structured deal metadata from this email. "
        "Return ONLY valid JSON with keys: "
        "property, market, asset_type, units, year_built, occupancy, rent, pricing, cap_rate, notes, links. "
        "Use empty string for unknown strings, null for unknown numbers. "
        "Prefer subject line facts if conflicts exist.\n\n"
        f"SUBJECT:\n{subject}\n\nBODY:\n{body}\n"
    )

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = getattr(msg, "content", None) or []
    text_parts: List[str] = []
    for p in parts:
        if getattr(p, "type", None) == "text":
            t = getattr(p, "text", "") or ""
            if t:
                text_parts.append(t)
    raw = "\n".join(text_parts).strip()
    if not raw:
        raise RuntimeError("Empty LLM response")

    try:
        data = json.loads(raw)
    except Exception:
        # try to salvage JSON embedded in markdown
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise
        data = json.loads(m.group(0))

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected LLM JSON type: {type(data)}")

    return {
        "property": str(data.get("property") or ""),
        "market": str(data.get("market") or ""),
        "asset_type": str(data.get("asset_type") or ""),
        "units": data.get("units"),
        "year_built": data.get("year_built"),
        "occupancy": str(data.get("occupancy") or ""),
        "rent": str(data.get("rent") or ""),
        "pricing": str(data.get("pricing") or ""),
        "cap_rate": str(data.get("cap_rate") or ""),
        "notes": str(data.get("notes") or ""),
        "links": data.get("links") or [],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument(
        "--max-emails",
        type=int,
        default=0,
        help="0 = no limit. Process only the N most recent envelopes.",
    )
    ap.add_argument(
        "--timeout-seconds",
        type=int,
        default=25,
        help="Per-message read timeout. If exceeded, the message is skipped and recorded as timeout.",
    )
    ap.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Retries per message read before marking as failed.",
    )
    ap.add_argument(
        "--read-body",
        action="store_true",
        help="If set, fetch and parse message body text for each email. If unset, use subject-only extraction (faster and avoids hangs).",
    )
    ap.add_argument(
        "--extractor",
        choices=["regex", "llm"],
        default="regex",
        help="Field extraction method. 'llm' uses Anthropic (ANTHROPIC_API_KEY + ANTHROPIC_EXTRACT_MODEL).",
    )
    ap.add_argument("--config", default=os.environ.get("HIMALAYA_CONFIG"), help="Optional himalaya config path")
    ap.add_argument("--out-dir", default=os.path.expanduser("~/.openclaw/workspace/reports"))
    args = ap.parse_args()

    envs = list_inbox_envelopes(folder=args.folder, page_size=args.page_size, config_path=args.config)
    if args.max_emails and len(envs) > args.max_emails:
        envs = envs[: args.max_emails]
    rows: List[Dict[str, Any]] = []

    for i, e in enumerate(envs, start=1):
        msg_id = str(e.get("id") or "")
        date = str(e.get("date") or "")
        subject = str(e.get("subject") or "")
        has_attachment = bool(e.get("has_attachment"))
        print(f"[{i}/{len(envs)}] Processing message {msg_id}...", flush=True)

        body = ""
        status = "ok"
        err = ""

        if args.read_body:
            print(f"  fetching body...", flush=True)
            for attempt in range(1, max(1, args.retries) + 1):
                try:
                    body = read_message_text(
                        msg_id=msg_id,
                        folder=args.folder,
                        config_path=args.config,
                        timeout_s=args.timeout_seconds,
                    )
                    break
                except subprocess.TimeoutExpired:
                    status = "timeout"
                    err = f"message read timed out after {args.timeout_seconds}s"
                except Exception as e2:
                    status = "error"
                    err = str(e2)
                if attempt < max(1, args.retries):
                    print(f"  retrying ({attempt}/{args.retries})...", flush=True)

        if args.extractor == "llm":
            try:
                llm = extract_fields_llm(subject=subject, body=body)
                prop = llm.get("property") or guess_property_and_market(subject, body)[0]
                market = llm.get("market") or _extract_market_prefer_subject(subject, body)
                asset_type = llm.get("asset_type") or guess_asset_type(subject, body)
                units = llm.get("units")
                year_built = llm.get("year_built")
                occ = llm.get("occupancy") or ""
                rent = llm.get("rent") or ""
                status = status if status != "ok" else "ok"
            except Exception as e3:
                # fall back to regex extraction
                status = "llm_error" if status == "ok" else status
                err = (err + "; " if err else "") + str(e3)
                prop, market = guess_property_and_market(subject, body)
                asset_type = guess_asset_type(subject, body)
                fields = extract_fields(subject, body)
                units = fields["units"] or ""
                year_built = fields["year_built"] or ""
                occ = fields["occupancy"]
                rent = fields["rent"]
        else:
            prop, market = guess_property_and_market(subject, body)
            asset_type = guess_asset_type(subject, body)
            fields = extract_fields(subject, body)
            units = fields["units"] or ""
            year_built = fields["year_built"] or ""
            occ = fields["occupancy"]
            rent = fields["rent"]

        rows.append(
            {
                "id": msg_id,
                "date": date,
                "property": prop,
                "market": market,
                "asset_type": asset_type,
                "units": units,
                "year_built": year_built,
                "occupancy": occ,
                "rent": rent,
                "has_attachment": has_attachment,
                "status": status,
                "error": err,
                "subject": subject,
            }
        )

    out_csv = os.path.join(args.out_dir, "inbox_deals.csv")
    out_md = os.path.join(args.out_dir, "inbox_deals.md")
    write_reports(rows, out_csv=out_csv, out_md=out_md)

    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_md}")
    print(f"Rows: {len(rows)}")


if __name__ == "__main__":
    main()
