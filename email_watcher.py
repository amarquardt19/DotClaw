#!/usr/bin/env python3
"""
email_watcher.py – Event-based IMAP IDLE watcher.

For every new INBOX email:
  1. Scrapes HTML → plain text
  2. OCRs all image attachments via Claude vision
  3. Extracts deal metadata via Claude → inserts into deal_metadata.db
  4. Composes an output message (scraped text + OCR results) and APPENDs
     it to the INBOX/OCRoutput folder in Fastmail

Uses IMAP IDLE (RFC 2177) — the server pushes notifications instantly.

Usage:
    python3 email_watcher.py              # IMAP IDLE watcher (runs forever)
    python3 email_watcher.py --once       # process pending then exit
"""
import argparse
import base64
import email as email_lib
import email.mime.text
import email.utils
import imaplib
import json
import os
import re
import select
import sqlite3
import pymssql
import caldav
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dateutil import parser as dateutil_parser
from dotenv import load_dotenv

load_dotenv()

STATE_FILE = Path(__file__).with_name("ocr_watcher_state.json")
OCR_OUTPUT_FOLDER = "INBOX/OCRoutput"
DB_PATH = Path(__file__).parent / "deals.db"

CALDAV_URL = "https://caldav.fastmail.com/dav/calendars"
CALENDAR_NAME = "Offers Due"
_DATE_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")

# IMAP creds from .env
IMAP_HOST = os.getenv("FASTMAIL_IMAP_HOST", "imap.fastmail.com")
IMAP_PORT = int(os.getenv("FASTMAIL_IMAP_PORT", "993"))
IMAP_USER = os.getenv("FASTMAIL_IMAP_USER", "")
IMAP_PASS = os.getenv("FASTMAIL_IMAP_PASSWORD", "")
MAILBOX = os.getenv("FASTMAIL_IMAP_MAILBOX", "INBOX")

# IDLE re-issue interval (RFC recommends < 29 min; we use 5 min)
IDLE_TIMEOUT = 300


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def imap_connect() -> imaplib.IMAP4_SSL:
    """Connect, login, select INBOX."""
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(IMAP_USER, IMAP_PASS)
    M.select(MAILBOX)
    return M


SENDER_FILTER = os.getenv("FILTER_FROM_ADDRESS", "adam@marqhome.com")


def imap_fetch_uids(M: imaplib.IMAP4_SSL) -> List[int]:
    """Return UIDs in the selected mailbox from the configured sender only."""
    typ, data = M.uid("SEARCH", None, f'FROM "{SENDER_FILTER}"')
    if typ != "OK" or not data or not data[0]:
        return []
    return [int(u) for u in data[0].split()]


def _decode_subject(msg) -> str:
    subj_raw = msg.get("Subject", "")
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(subj_raw)))
    except Exception:
        return subj_raw


def imap_fetch_full(M: imaplib.IMAP4_SSL, uid: int) -> Dict[str, Any]:
    """Fetch subject, text body, HTML body, and image attachments for a UID."""
    typ, data = M.uid("FETCH", str(uid), "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        return {"subject": "", "body": "", "html": "", "images": []}

    raw_email = data[0][1]
    msg = email_lib.message_from_bytes(raw_email)
    subject = _decode_subject(msg)

    body = ""
    html_body = ""
    images: List[Tuple[str, bytes, str]] = []  # (filename, data, media_type)

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue

            if ct == "text/plain" and not body:
                charset = part.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
            elif ct == "text/html" and not html_body:
                charset = part.get_content_charset() or "utf-8"
                html_body = payload.decode(charset, errors="replace")
            elif ct.startswith("image/"):
                fname = part.get_filename() or f"image.{ct.split('/')[-1]}"
                images.append((fname, payload, ct))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                body = decoded

    # Collect CID-referenced images into a lookup
    cid_images: Dict[str, Tuple[str, bytes, str]] = {}  # cid -> (fname, data, media_type)
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cid = part.get("Content-ID", "").strip("<> ")
            if ct.startswith("image/") and cid:
                payload = part.get_payload(decode=True)
                if payload:
                    fname = part.get_filename() or f"cid_{cid}.{ct.split('/')[-1]}"
                    cid_images[cid] = (fname, payload, ct)

    # Extract inline images from HTML <img> tags
    inline_images: List[Tuple[str, bytes, str]] = []
    if html_body:
        inline_images = _extract_inline_images(html_body, cid_images)

    # Merge: MIME-attached images + inline images (deduplicate by filename)
    seen_fnames = {f for f, _, _ in images}
    for fname, data, mt in inline_images:
        if fname not in seen_fnames:
            images.append((fname, data, mt))
            seen_fnames.add(fname)

    # Fallback: strip HTML if no plain text
    if not body and html_body:
        body = _html_to_text(html_body)

    from_addr = msg.get("From", "")
    return {"subject": subject, "from": from_addr, "body": body, "html": html_body, "images": images}


# Minimum image size to OCR (skip tracking pixels, spacers, icons < 2KB)
MIN_IMAGE_BYTES = 2000
# Skip images whose URL contains these patterns (tracking/spacer/logo junk)
_SKIP_URL_PATTERNS = re.compile(
    r'(spacer|pixel|tracking|beacon|open\.|unsubscribe|'
    r'logo.*small|1x1|blank\.gif|transparent\.)', re.I
)


def _guess_media_type(url: str, content_type: str = "") -> str:
    """Guess media type from URL extension or Content-Type header."""
    if content_type and content_type.startswith("image/"):
        return content_type.split(";")[0].strip()
    ext = urlparse(url).path.rsplit(".", 1)[-1].lower()
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    }.get(ext, "image/png")


def _unwrap_url(url: str) -> str:
    """Unwrap link-protection proxies (Barracuda, Outlook SafeLinks, Proofpoint)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    # Barracuda linkprotect.cudasvc.com/url?a=<encoded_url>
    if "cudasvc.com" in host:
        params = dict(p.split("=", 1) for p in (parsed.query or "").split("&") if "=" in p)
        if "a" in params:
            from urllib.parse import unquote
            return unquote(params["a"])
    # Outlook SafeLinks
    if "safelinks.protection.outlook.com" in host:
        params = dict(p.split("=", 1) for p in (parsed.query or "").split("&") if "=" in p)
        if "url" in params:
            from urllib.parse import unquote
            return unquote(params["url"])
    # Proofpoint urldefense
    if "urldefense" in host:
        params = dict(p.split("=", 1) for p in (parsed.query or "").split("&") if "=" in p)
        if "u" in params:
            from urllib.parse import unquote
            return unquote(params["u"].replace("-", "%").replace("_", "/"))
    return url


def _download_image(url: str, timeout: int = 15) -> Optional[Tuple[bytes, str]]:
    """Download an image from a URL. Returns (data, media_type) or None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            data = resp.read()
            if len(data) < MIN_IMAGE_BYTES:
                return None
            return data, _guess_media_type(url, ct)
    except Exception:
        return None


def _extract_inline_images(
    html: str, cid_images: Dict[str, Tuple[str, bytes, str]]
) -> List[Tuple[str, bytes, str]]:
    """Parse <img> tags from HTML, download remote images, resolve CID refs."""
    results: List[Tuple[str, bytes, str]] = []
    seen_urls: set = set()

    for match in re.finditer(r'<img[^>]+src=["\']([^"\'>]+)["\']', html, re.I):
        src = match.group(1).strip()
        if not src or src in seen_urls:
            continue
        seen_urls.add(src)

        # Skip tracking pixels / tiny junk
        if _SKIP_URL_PATTERNS.search(src):
            continue

        # CID reference (inline MIME attachment)
        if src.startswith("cid:"):
            cid = src[4:]
            if cid in cid_images:
                results.append(cid_images[cid])
            continue

        # Data URI
        if src.startswith("data:image/"):
            try:
                header, b64_data = src.split(",", 1)
                mt = header.split(";")[0].replace("data:", "")
                img_bytes = base64.b64decode(b64_data)
                if len(img_bytes) >= MIN_IMAGE_BYTES:
                    results.append((f"data_uri.{mt.split('/')[-1]}", img_bytes, mt))
            except Exception:
                pass
            continue

        # Remote URL
        if src.startswith(("http://", "https://")):
            actual_url = _unwrap_url(src)
            if _SKIP_URL_PATTERNS.search(actual_url):
                continue
            result = _download_image(actual_url)
            if result:
                data, mt = result
                fname = urlparse(actual_url).path.rsplit("/", 1)[-1][:60] or "inline_image"
                results.append((fname, data, mt))

    return results


def _html_to_text(html: str) -> str:
    """Simple HTML→text. Strips tags, decodes entities, collapses whitespace."""
    import html as html_mod
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.S | re.I)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>|</p>|</div>|</tr>|</li>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()[:8000]


# ---------------------------------------------------------------------------
# Deterministic vintage extraction from OCR / scraped text
# ---------------------------------------------------------------------------

_VINTAGE_RE = re.compile(
    r'(?:year\s*built|built\s*in|constructed\s*in|vintage|year\s*of\s*construction)'
    r'[\s:|\-]*'
    r'((?:19|20)\d{2})',
    re.IGNORECASE,
)


def _extract_vintage_from_text(text: str) -> Optional[int]:
    """Deterministic regex to find year built from text. Returns 4-digit year or None."""
    if not text:
        return None
    m = _VINTAGE_RE.search(text)
    if m:
        yr = int(m.group(1))
        if 1900 <= yr <= 2030:
            return yr
    return None


# ---------------------------------------------------------------------------
# Deterministic rent extraction from OCR / scraped text
# ---------------------------------------------------------------------------

_RENT_LABEL_RE = re.compile(
    r'(?:leased\s*rent|average\s*rent|avg\.?\s*(?:monthly\s*)?rent|in[- ]place\s*rent'
    r'|current\s*rent|effective\s*rent|rent\s*/\s*unit|rent\s*per\s*unit'
    r'|monthly\s*rent|contract\s*rent)'
    r'[\s\S]{0,150}?'
    r'\$\s*([\d,]+)',
    re.IGNORECASE,
)

_RENT_HEADER_RE = re.compile(
    r'(?:rent|psf)\s*[\|:]\s*(?:psf\s*)?\$\s*([\d,]+)',
    re.IGNORECASE,
)


def _extract_rent_from_text(text: str) -> Optional[float]:
    """Deterministic regex to find the property-level monthly rent from
    OCR or scraped text. Returns a float in the 500-5000 range, or None."""
    if not text:
        return None
    for pattern in (_RENT_LABEL_RE, _RENT_HEADER_RE):
        for m in pattern.finditer(text):
            try:
                val = float(m.group(1).replace(",", ""))
                if 500 <= val <= 5000:
                    return val
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Link-following for thin-OCR emails (teaser emails with "View in browser")
# ---------------------------------------------------------------------------

_VIEW_KW_RE = re.compile(
    r'view\s+(?:in\s+browser|online|website|email)|view\s+(?:the\s+)?full'
    r'|click\s+here\s+to\s+view|having\s+trouble|trouble\s+viewing',
    re.IGNORECASE,
)

_A_TAG_RE = re.compile(
    r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _scrape_view_links(html: str, max_links: int = 2) -> str:
    """Extract 'View in browser'/'View Website' links from HTML, fetch and
    scrape them to plain text. Returns combined scraped text or empty string."""
    if not html:
        return ""
    urls = []
    for m in _A_TAG_RE.finditer(html):
        raw_url = m.group(1).strip()
        inner_text = re.sub(r'<[^>]+>', '', m.group(2))
        if not _VIEW_KW_RE.search(inner_text):
            continue
        if raw_url.startswith("mailto:"):
            continue
        url = _unwrap_url(raw_url)
        if url.startswith("http") and url not in urls:
            urls.append(url)
        if len(urls) >= max_links:
            break
    if not urls:
        return ""

    scraped_parts = []
    for url in urls:
        try:
            page_text = _screenshot_and_ocr(url)
            if page_text and len(page_text) > 100:
                scraped_parts.append(page_text)
        except Exception:
            continue
    return "\n\n".join(scraped_parts)


def _screenshot_and_ocr(url: str) -> str:
    """Use Playwright to render a JS page, take a full-page screenshot,
    and OCR it via Claude Vision. Returns extracted text."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""

    screenshots = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, timeout=20000, wait_until="networkidle")
            time.sleep(1)

            # Scroll and capture full page in chunks to stay under Claude's size limit
            total_height = page.evaluate("document.body.scrollHeight")
            viewport_h = 900
            offset = 0
            while offset < total_height and len(screenshots) < 5:
                page.evaluate(f"window.scrollTo(0, {offset})")
                time.sleep(0.3)
                png = page.screenshot(type="png")
                screenshots.append(png)
                offset += viewport_h

            browser.close()
    except Exception as ex:
        print(f"    -> Playwright screenshot failed: {ex}", flush=True)
        return ""

    if not screenshots:
        return ""

    # OCR each screenshot via Claude Vision
    ocr_parts = []
    for i, png_data in enumerate(screenshots):
        try:
            text = ocr_image(png_data, "image/png", f"page_screenshot_{i}.png")
            if text and text.strip().upper() != "NONE":
                ocr_parts.append(text)
        except Exception:
            continue

    return "\n\n".join(ocr_parts)


# ---------------------------------------------------------------------------
# IMAP IDLE
# ---------------------------------------------------------------------------

def idle_wait(M: imaplib.IMAP4_SSL, timeout: int = IDLE_TIMEOUT) -> bool:
    """Enter IDLE, wait for server push or timeout. Returns True if new mail signalled."""
    tag = M._new_tag().decode()
    M.send(f"{tag} IDLE\r\n".encode())
    # Read continuation response (+ idling)
    resp = M.readline()
    if b"+" not in resp:
        # Server doesn't support IDLE — fall back
        return False

    # Wait for data on the socket (EXISTS notification) or timeout
    sock = M.socket()
    readable, _, _ = select.select([sock], [], [], timeout)

    # Send DONE to exit IDLE regardless
    M.send(b"DONE\r\n")
    # Read tagged response
    while True:
        line = M.readline()
        if line.startswith(tag.encode()):
            break

    if readable:
        return True  # server pushed something
    return False  # just a timeout, re-issue IDLE


# ---------------------------------------------------------------------------
# OCR via Claude Vision
# ---------------------------------------------------------------------------

def _resize_image_if_needed(image_data: bytes, media_type: str, max_b64_bytes: int = 5_000_000) -> tuple:
    """Resize image if base64 encoding would exceed Claude's limit. Returns (data, media_type)."""
    b64_size = len(image_data) * 4 // 3  # approximate base64 size
    if b64_size <= max_b64_bytes:
        return image_data, media_type
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_data))
        # Scale down proportionally to fit within limit
        scale = (max_b64_bytes / b64_size) ** 0.5 * 0.9  # 10% safety margin
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = "JPEG" if media_type in ("image/jpeg", "image/jpg") else "PNG"
        if img.mode == "RGBA" and fmt == "JPEG":
            img = img.convert("RGB")
        img.save(buf, format=fmt, quality=85)
        new_mt = "image/jpeg" if fmt == "JPEG" else "image/png"
        print(f"    OCR: resized {len(image_data)}→{buf.tell()} bytes ({img.width}x{new_h})", flush=True)
        return buf.getvalue(), new_mt
    except Exception as ex:
        print(f"    OCR: resize failed ({ex}), skipping oversized image", flush=True)
        return None, None


def ocr_image(image_data: bytes, media_type: str, filename: str = "") -> str:
    """OCR an image using Claude vision. Returns extracted text."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_OCR_MODEL", "claude-sonnet-4-20250514")
    if not api_key:
        return "[OCR skipped: no ANTHROPIC_API_KEY]"

    # Resize if needed to stay under Claude's 5MB base64 limit
    image_data, media_type = _resize_image_if_needed(image_data, media_type)
    if image_data is None:
        return ""

    from anthropic import Anthropic

    b64 = base64.b64encode(image_data).decode("ascii")
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Extract ONLY the text visible in this image. Return each line of text "
                        "on its own line. Do NOT describe the image or its contents — only "
                        "return text that is literally written/printed in the image. "
                        "PAY VERY CLOSE ATTENTION to numbers — year built, unit counts, "
                        "dollar amounts, rents, percentages, and square footage must be "
                        "transcribed exactly as shown. Double-check every digit. "
                        "If there is no text at all, respond with exactly: NONE"
                    ),
                },
            ],
        }],
    )
    parts = getattr(msg, "content", None) or []
    return "\n".join(
        getattr(p, "text", "") for p in parts if getattr(p, "type", None) == "text"
    ).strip()


# ---------------------------------------------------------------------------
# Compose & APPEND OCR output message to INBOX/OCRoutput
# ---------------------------------------------------------------------------

def compose_ocr_message(*, subject: str, uid: int, scraped_text: str,
                        ocr_results: List[Tuple[str, str]]) -> str:
    """Build a plain-text RFC822 message for the OCR output folder."""
    body_parts = []
    body_parts.append(f"=== OCR + Scrape Output for UID {uid} ===")
    body_parts.append(f"Original Subject: {subject}")
    body_parts.append("")

    body_parts.append("--- SCRAPED HTML TEXT ---")
    body_parts.append(scraped_text[:15000] if scraped_text else "(no text content)")
    body_parts.append("")

    if ocr_results:
        for fname, ocr_text in ocr_results:
            body_parts.append(f"--- OCR: {fname} ---")
            body_parts.append(ocr_text if ocr_text else "(no text extracted)")
            body_parts.append("")
    else:
        body_parts.append("--- OCR ---")
        body_parts.append("(no images found in email)")
        body_parts.append("")

    full_body = "\n".join(body_parts)

    msg = email.mime.text.MIMEText(full_body, "plain", "utf-8")
    msg["Subject"] = f"[OCR] {subject}"
    msg["From"] = IMAP_USER
    msg["To"] = IMAP_USER
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["X-OCR-Source-UID"] = str(uid)

    return msg.as_string()


def _ocr_already_exists(M: imaplib.IMAP4_SSL, source_uid: int) -> bool:
    """Check if an OCR output message already exists for this source UID."""
    try:
        prev_mailbox = MAILBOX
        M.select(OCR_OUTPUT_FOLDER)
        typ, data = M.uid("SEARCH", None, f'HEADER X-OCR-Source-UID "{source_uid}"')
        M.select(prev_mailbox)
        if typ == "OK" and data and data[0]:
            return True
    except Exception:
        try:
            M.select(MAILBOX)
        except Exception:
            pass
    return False


def append_to_ocr_folder(M: imaplib.IMAP4_SSL, rfc822_msg: str) -> None:
    """APPEND a composed message to the OCRoutput folder."""
    typ, _ = M.append(
        OCR_OUTPUT_FOLDER,
        "(\\Seen)",
        imaplib.Time2Internaldate(time.time()),
        rfc822_msg.encode("utf-8"),
    )
    if typ != "OK":
        raise RuntimeError(f"IMAP APPEND to {OCR_OUTPUT_FOLDER} failed: {typ}")


# ---------------------------------------------------------------------------
# Pre-OCR gating — skip OCR if plain text clearly shows deal is gated
# ---------------------------------------------------------------------------

_UNITS_RE = re.compile(
    r'(\d{1,4})\s*[-–]?\s*(?:\w+\s+)?(?:units?|apartments?|homes?|doors?)\b',
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r'\$\s*(\d+(?:\.\d+)?)\s*(m|mm|mil|million)\b', re.IGNORECASE,
)
_PRICE_FULL_RE = re.compile(
    r'\$\s*([\d,]+(?:\.\d+)?)\b',
)


def _pre_ocr_gating_check(subject: str, plain_text: str) -> Tuple[bool, str]:
    """Check if a deal is clearly gated from subject + plain text alone.

    Returns (is_gated, reason) where reason is empty string if not gated.
    Gating rules:
      - units < 80  → gated
      - price < $30M → gated
    """
    combined = f"{subject}\n{plain_text}"

    # --- Extract units (take largest match) ---
    units = None
    for m in _UNITS_RE.finditer(combined):
        try:
            val = int(m.group(1))
            if 1 <= val <= 9999:
                units = max(units or 0, val)
        except ValueError:
            pass

    # --- Extract price ---
    price = None
    for m in _PRICE_RE.finditer(combined):
        try:
            num = float(m.group(1))
            suffix = m.group(2).lower()
            if suffix in ("m", "mm", "mil", "million"):
                price = num * 1_000_000
                break
        except ValueError:
            pass
    if price is None:
        for m in _PRICE_FULL_RE.finditer(combined):
            try:
                val = float(m.group(1).replace(",", ""))
                if val >= 1_000_000:
                    price = val
                    break
            except ValueError:
                pass

    # --- Apply gating (require BOTH to gate — either alone is too noisy) ---
    gated_units = units is not None and units < 80
    gated_price = price is not None and price < 30_000_000

    if gated_units and gated_price:
        return True, "units<80 & price<$30M"
    return False, ""


# ---------------------------------------------------------------------------
# Azure SQL DB
# ---------------------------------------------------------------------------

def ensure_db():
    """Connect to Azure SQL Database."""
    conn = pymssql.connect(
        server=os.getenv('AZURE_SQL_SERVER'),
        user=os.getenv('AZURE_SQL_USER'),
        password=os.getenv('AZURE_SQL_PASSWORD'),
        database=os.getenv('AZURE_SQL_DATABASE')
    )
    return conn


def insert_deal(conn, uid: int, subject: str,
                raw_text: str, fields: Dict[str, Any]) -> None:
    # If no offers_due_date and it's a non-dated email type, use email_type as fallback
    offers_due = str(fields.get("offers_due_date") or "").strip()
    email_type = str(fields.get("email_type") or "").strip()
    if not offers_due and email_type:
        offers_due = email_type

    cursor = conn.cursor()
    # MERGE for upsert (INSERT or UPDATE)
    cursor.execute(
        """MERGE deals AS target
           USING (SELECT %s AS uid) AS source ON target.uid = source.uid
           WHEN MATCHED THEN UPDATE SET
               subject=%s, received_date=%s, property_name=%s, address=%s,
               city=%s, state=%s, msa=%s, asset_type=%s, email_type=%s,
               offers_due_date=%s, units=%s, year_built=%s, avg_unit_size=%s,
               occupancy=%s, avg_rent=%s, rent_per_sf=%s, asking_price=%s,
               cap_rate=%s, price_per_unit=%s, broker_name=%s, broker_company=%s,
               highlights=%s, raw_ocr_text=%s, is_deal=%s
           WHEN NOT MATCHED THEN INSERT
               (uid, subject, received_date, property_name, address,
                city, state, msa, asset_type, email_type, offers_due_date,
                units, year_built, avg_unit_size, occupancy, avg_rent, rent_per_sf,
                asking_price, cap_rate, price_per_unit, broker_name, broker_company,
                highlights, raw_ocr_text, is_deal)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
        (
            uid,
            # UPDATE values
            subject,
            str(fields.get("received_date") or ""),
            str(fields.get("property_name") or ""),
            str(fields.get("address") or ""),
            str(fields.get("city") or ""),
            str(fields.get("state") or ""),
            str(fields.get("msa") or ""),
            str(fields.get("asset_type") or ""),
            email_type,
            offers_due,
            _safe_int(fields.get("units")),
            str(fields.get("year_built") or "") or None,
            _safe_float(fields.get("avg_unit_size")),
            _safe_float(fields.get("occupancy")),
            _safe_float(fields.get("avg_rent")),
            _safe_float(fields.get("rent_per_sf")),
            str(fields.get("asking_price") or ""),
            _safe_float(fields.get("cap_rate")),
            _safe_float(fields.get("price_per_unit")),
            str(fields.get("broker_name") or ""),
            str(fields.get("broker_company") or ""),
            str(fields.get("highlights") or ""),
            raw_text[:20000],
            1 if fields.get("is_deal", True) else 0,
            # INSERT values (same order)
            uid,
            subject,
            str(fields.get("received_date") or ""),
            str(fields.get("property_name") or ""),
            str(fields.get("address") or ""),
            str(fields.get("city") or ""),
            str(fields.get("state") or ""),
            str(fields.get("msa") or ""),
            str(fields.get("asset_type") or ""),
            email_type,
            offers_due,
            _safe_int(fields.get("units")),
            str(fields.get("year_built") or "") or None,
            _safe_float(fields.get("avg_unit_size")),
            _safe_float(fields.get("occupancy")),
            _safe_float(fields.get("avg_rent")),
            _safe_float(fields.get("rent_per_sf")),
            str(fields.get("asking_price") or ""),
            _safe_float(fields.get("cap_rate")),
            _safe_float(fields.get("price_per_unit")),
            str(fields.get("broker_name") or ""),
            str(fields.get("broker_company") or ""),
            str(fields.get("highlights") or ""),
            raw_text[:20000],
            1 if fields.get("is_deal", True) else 0,
        ),
    )
    conn.commit()


def _safe_bool(val) -> Optional[int]:
    """Convert bool/truthy to BIT (1/0) for SQL Server."""
    if val is None or val == "" or val == "null":
        return None
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, (int, float)):
        return 1 if val else 0
    s = str(val).lower().strip()
    if s in ("true", "yes", "1"):
        return 1
    if s in ("false", "no", "0"):
        return 0
    return None


def _safe_str(val, max_len: int = 0) -> Optional[str]:
    """Convert to string or None. Truncate if max_len > 0."""
    if val is None or val == "" or val == "null":
        return None
    s = str(val).strip()
    if not s:
        return None
    if max_len and len(s) > max_len:
        s = s[:max_len]
    return s


# -- Column spec for deal_intake (order matches CREATE TABLE) --
_INTAKE_COLS = [
    "uid", "score", "residential_interest_score", "industrial_interest_score",
    "asset_class", "product", "deal_name", "city", "state", "units",
    "guidance_price", "offer_due_date", "transaction_type", "sponsor", "broker",
    "rent", "occupancy", "vintage", "notes",
    "broker_name", "broker_firm", "broker_email", "broker_phone",
    "email_received_utc", "email_from", "email_subject", "source_mailbox",
    "downloaded_files_count",
    "address", "msa", "rent_psf", "avg_rent_monthly", "rent_units",
    "unit_size", "buildings", "building_size", "price_per_sf",
    "in_place_cap_rate", "lease_type", "annual_escalations",
    "going_in_cash_yield", "discount_to_replacement",
    "seller", "renovated", "renovation_year", "tenants", "walt",
    "flood_zone", "site_coverage_ratio", "credit_tenant",
    "clear_height", "ceiling_height", "message_id",
    "is_residential", "is_industrial", "residential_gated",
    "residential_gating_reason",
    "residential_geo_score", "residential_units_score",
    "residential_price_score", "residential_rent_score",
    "residential_vintage_score", "residential_returns_score",
    "residential_strategy_score", "residential_notes_boost",
    "score_version", "score_missing_inputs", "score_breakdown",
    "raw_ocr_text",
]


def _intake_val(key: str, fields: Dict[str, Any], uid: int,
                raw_text: str) -> Any:
    """Return the properly-typed value for one deal_intake column."""
    if key == "uid":
        return uid
    if key == "raw_ocr_text":
        return raw_text[:20000] if raw_text else None

    v = fields.get(key)

    # float columns
    if key in (
        "score", "residential_interest_score", "industrial_interest_score",
        "guidance_price", "occupancy", "rent_psf", "avg_rent_monthly",
        "unit_size", "building_size", "price_per_sf", "in_place_cap_rate",
        "annual_escalations", "going_in_cash_yield", "discount_to_replacement",
        "walt", "site_coverage_ratio", "clear_height", "ceiling_height",
        "residential_geo_score", "residential_units_score",
        "residential_price_score", "residential_rent_score",
        "residential_vintage_score", "residential_returns_score",
        "residential_strategy_score", "residential_notes_boost",
    ):
        return _safe_float(v)

    # int columns
    if key in (
        "units", "vintage", "renovation_year", "buildings",
        "downloaded_files_count",
    ):
        return _safe_int(v)

    # bit/bool columns
    if key in (
        "renovated", "flood_zone", "credit_tenant",
        "is_residential", "is_industrial", "residential_gated",
    ):
        return _safe_bool(v)

    # tenants array → JSON string
    if key == "tenants":
        if isinstance(v, list):
            return json.dumps(v)
        return _safe_str(v)

    # everything else is a string
    return _safe_str(v)


def insert_raw_deal_email(conn, uid: int, subject: str, email_from: str,
                          scraped_text: str, ocr_text: str,
                          extracted_json: str, fields: Dict[str, Any],
                          pre_gated: bool, gate_reason: str) -> None:
    """Upsert one row into raw_deal_emails (raw layer for dbt)."""
    cursor = conn.cursor()
    cursor.execute(
        """MERGE raw_deal_emails AS target
           USING (SELECT %s AS uid) AS source ON target.uid = source.uid
           WHEN MATCHED THEN UPDATE SET
               email_subject=%s, email_from=%s, email_received_utc=%s,
               message_id=%s, source_mailbox=%s,
               scraped_text=%s, ocr_text=%s, extracted_json=%s,
               resolved_msa=%s, web_avg_rent_monthly=%s,
               pre_gated=%s, gate_reason=%s
           WHEN NOT MATCHED THEN INSERT
               (uid, email_subject, email_from, email_received_utc,
                message_id, source_mailbox,
                scraped_text, ocr_text, extracted_json,
                resolved_msa, web_avg_rent_monthly,
                pre_gated, gate_reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
        (
            uid,
            # UPDATE values
            subject, email_from,
            _safe_str(fields.get("email_received_utc")),
            _safe_str(fields.get("message_id")),
            MAILBOX,
            (scraped_text or "")[:20000],
            (ocr_text or "")[:20000],
            extracted_json[:20000] if extracted_json else None,
            _safe_str(fields.get("msa")),
            _safe_float(fields.get("web_avg_rent_monthly")),
            1 if pre_gated else 0,
            gate_reason or None,
            # INSERT values
            uid, subject, email_from,
            _safe_str(fields.get("email_received_utc")),
            _safe_str(fields.get("message_id")),
            MAILBOX,
            (scraped_text or "")[:20000],
            (ocr_text or "")[:20000],
            extracted_json[:20000] if extracted_json else None,
            _safe_str(fields.get("msa")),
            _safe_float(fields.get("web_avg_rent_monthly")),
            1 if pre_gated else 0,
            gate_reason or None,
        ),
    )
    conn.commit()


def insert_deal_intake(conn, uid: int, raw_text: str,
                       fields: Dict[str, Any]) -> None:
    """Upsert one row into the deal_intake table."""
    vals = tuple(_intake_val(c, fields, uid, raw_text) for c in _INTAKE_COLS)

    set_clause = ", ".join(f"{c}=%s" for c in _INTAKE_COLS[1:])  # skip uid
    col_list = ", ".join(_INTAKE_COLS)
    placeholders = ", ".join(["%s"] * len(_INTAKE_COLS))

    cursor = conn.cursor()
    cursor.execute(
        f"""MERGE deal_intake AS target
            USING (SELECT %s AS uid) AS source ON target.uid = source.uid
            WHEN MATCHED THEN UPDATE SET {set_clause}
            WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({placeholders});""",
        (uid,) + vals[1:] + vals,  # uid for USING, update vals, insert vals
    )
    conn.commit()


def _safe_float(val) -> Optional[float]:
    if val is None or val == "" or val == "null":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        cleaned = str(val).replace("$", "").replace(",", "").replace("%", "").split("/")[0].strip()
        return float(cleaned) if cleaned else None
    except Exception:
        return None


def _safe_int(val) -> Optional[int]:
    if val is None or val == "" or val == "null":
        return None
    if isinstance(val, int):
        return val
    try:
        return int(str(val).replace(",", "").strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Claude structured metadata extraction
# ---------------------------------------------------------------------------

BLUEROCK_SYSTEM_PROMPT = """\
You are an information extraction agent for institutional real-estate deal emails.

Goal: Given ONE inbound email (broker/sponsor teaser) plus any attached or linked \
PDFs/images, extract deal attributes and return a single valid JSON object.

Hard output rules:
- Output MUST be valid JSON only. No markdown, no commentary.
- Include ONLY fields that are explicitly present or can be derived with high \
confidence from the email and provided files.
- If a field is missing or ambiguous, omit the key entirely (do not \
use null, "", "unknown", or "N/A").
- Prefer numeric types when possible (integers/floats). Use booleans for Y/N fields.
- Do not hallucinate. Do not estimate. Do not calculate financial metrics that \
are not explicitly provided.
- Single flat JSON object only (no nested objects).
- If the email is clearly NOT a real CRE deal (spam, newsletters, onboarding), \
return {"is_deal": false} and nothing else.

Normalization rules:
- Email body takes precedence over attachments unless attachments clearly provide \
more precise data.
- Address split: address, city, state (2-letter US state if applicable).
- product: only for residential deals and MUST be one of: "MF", "TH", "BFR". \
Map "multifamily"->MF, "townhome(s)"->TH, "build for rent"->BFR. If not \
applicable, omit.
- Percent fields: represent as numeric percent (e.g., 5.25 for 5.25%).
- Currency fields: represent as numbers in USD (e.g., 85000000). If only "$85M" \
or "$85MM", convert to 85000000.
- Building size in SF (square feet) as a number when possible.
- Heights in feet as numbers when possible.
- Occupancy as numeric percent (e.g., 94.0).
- Units as integer.
- Vintage as 4-digit year.
- Renovated: true ONLY if the email indicates renovation completed; otherwise omit.
- renovation_year: include ONLY if explicitly stated.
- Tenants: always return an array of tenant names IF present. If single tenant, \
still return an array. If no tenants provided, omit the tenants key entirely.
- annual_escalations: numeric percent.
- offer_due_date: if you can confidently normalize, output ISO 8601 \
(YYYY-MM-DD or YYYY-MM-DDThh:mm:ss±hh:mm). Otherwise output the original \
string exactly as written.
- going_in_cash_yield: include ONLY if explicitly stated. Do NOT compute or infer.
- discount_to_replacement: include ONLY if explicitly stated. Do NOT compute or infer.
- avg_rent_monthly: include ONLY if explicitly stated as an average monthly rent \
per unit. Do NOT infer from rent ranges.
- rent_units: include ONLY if avg_rent_monthly or rent_psf is present; otherwise omit. \
"monthly_per_unit" if avg_rent_monthly is present, "psf" if rent_psf is present \
and avg_rent_monthly is not, "unknown" only if rent is present but units cannot \
be determined.
- notes: MUST include key investment thesis highlights, market positioning, \
and competitive advantages described in the email. Extract bullet points from \
the deal narrative such as supply constraints, demographic strengths, rent \
growth drivers, location advantages, renovation potential, etc. Separate \
bullets with " | " only. No line breaks. Single-line string. \
Do NOT leave notes empty if the email describes deal merits.

Field definitions: WALT = Weighted Average Lease Term (years), MSA = Metropolitan \
Statistical Area.

Fields to extract (only include if relevant):
is_deal, deal_name, address, city, state, product (MF|TH|BFR), asset_class, \
transaction_type, units, unit_size, vintage, renovation_year, in_place_cap_rate, \
broker_name (up to 2 names, comma-separated, e.g. "Scott Ramey, John Smith"), \
broker_firm (brokerage company, e.g. "Newmark", "Berkadia", "CBRE"), \
broker_email (up to 2 emails, comma-separated), \
broker_phone (up to 2 phones, comma-separated), \
seller, guidance_price, buildings, clear_height, building_size, \
price_per_sf, credit_tenant, flood_zone, site_coverage_ratio, tenants, walt, \
msa, ceiling_height, occupancy, rent, rent_psf, avg_rent_monthly, rent_units, \
lease_type, annual_escalations, going_in_cash_yield, discount_to_replacement, \
offer_due_date, renovated, sponsor, notes.

Qualitative signals (MUST include for deals — used by downstream scoring):
- positive_signals: array of strings. Include ONLY signals that are EXPLICITLY \
stated in the email/OM with clear supporting language. Valid signals: \
"strong school district" (requires: specific school district name, ISD reference, \
or ranking mentioned), "limited new supply" (requires: explicit mention of limited \
land, supply constrained, barrier to entry, minimal future supply, high construction \
costs, or no new competing supply), "assumable debt" (requires: explicit mention of \
assumable loan/debt), "discount to replacement" (requires: stated replacement cost \
comparison), "below-market rents" (requires: EXPLICIT statement that current rents \
are BELOW market/submarket rents, or mark-to-market opportunity stated — do NOT \
flag this just because renovated units get higher rents or there is general rent \
growth), "institutional sponsor" (requires: named institutional sponsor/operator). \
Return empty array [] if none found. When in doubt, leave it OUT.
- negative_signals: array of strings. Include ONLY signals that are EXPLICITLY \
stated. Valid signals: "crime or collections issues", "insurance shock", \
"HOA litigation", "regulatory risk". Return empty array [] if none found.
"""


# ---------------------------------------------------------------------------
# Deterministic Bluerock scoring (v1) — Python, no LLM
# ---------------------------------------------------------------------------

_TIER1 = {"GA", "NC", "SC", "VA", "TX", "FL"}
_TIER2 = {"AL", "TN", "OH", "MI", "MA", "CO", "AZ", "UT", "MD"}
_SUNBELT_ADJ = {"LA", "MS", "OK", "AR"}
_DEV_KW = {"development", "lease-up", "lease up", "under construction",
           "new construction", "delivered vacant", "heavy lift"}
_CORE_KW = {"core", "core+", "core-plus", "stabilized"}
_VA_KW = {"value-add", "value add", "reposition"}
_MTM_KW = {"mark-to-market"}


def compute_bluerock_score(fields: Dict[str, Any], training_mode: bool = False) -> Dict[str, Any]:
    """Compute Bluerock interest score from extracted deal fields.
    Returns a dict of scoring keys to merge into the fields dict.
    training_mode=True bypasses residential gating (for CBRE scraper training)."""

    product = str(fields.get("product") or "").upper()
    asset_class = str(fields.get("asset_class") or "").lower()

    is_residential = product in ("MF", "TH", "BFR") or any(
        kw in asset_class for kw in ("multifamily", "residential", "bfr",
                                      "build for rent", "townhome",
                                      "senior housing", "student housing",
                                      "affordable housing")
    )
    is_industrial = "industrial" in asset_class

    result: Dict[str, Any] = {
        "is_residential": is_residential,
        "is_industrial": is_industrial,
        "score_version": "BR_SCORE_v1_RES",
    }

    # --- Industrial placeholder ---
    result["industrial_interest_score"] = 0.0

    if not is_residential:
        result.update({
            "residential_gated": False,
            "residential_interest_score": 0.0,
            "residential_geo_score": 0.0,
            "residential_units_score": 0.0,
            "residential_price_score": 0.0,
            "residential_rent_score": 0.0,
            "residential_vintage_score": 0.0,
            "residential_returns_score": 0.0,
            "residential_strategy_score": 0.0,
            "residential_notes_boost": 0.0,
            "notes_score_reason": None,
            "score": 0.0,
        })
        _build_breakdown(result)
        return result

    # --- Gating ---
    units = _safe_int(fields.get("units"))
    price = _safe_float(fields.get("guidance_price"))

    gated_units = units is not None and units < 80
    gated_price = price is not None and price < 30_000_000
    gated = (gated_units or gated_price) and not training_mode

    result["residential_gated"] = gated
    if gated:
        reasons = []
        if gated_units:
            reasons.append("units<80")
        if gated_price:
            reasons.append("price<$30M")
        result["residential_gating_reason"] = "&".join(reasons)

    # --- Transaction type inference ---
    # Trust Claude's value ONLY if it's a recognized strategy keyword.
    # Generic values like "sale", "acquisition" are not strategies.
    # If not recognized, infer from notes + email subject.
    _KNOWN_STRATEGIES = {"value-add", "value add", "core", "core+", "core-plus",
                         "stabilized", "development", "lease-up", "lease up",
                         "reposition", "heavy lift", "mark-to-market",
                         "distressed"}
    tt_raw = str(fields.get("transaction_type") or "").strip()
    notes_lower = str(fields.get("notes") or "").lower()
    subject_lower = str(fields.get("email_subject") or
                        fields.get("subject") or "").lower()
    full_text_lower = str(fields.get("_full_text") or "").lower()
    text_to_scan = notes_lower + " " + subject_lower + " " + full_text_lower

    # Sanity check: a property with a vintage is an existing asset, not development
    has_vintage = _safe_int(fields.get("vintage")) is not None
    trust_claude = (tt_raw.lower() in _KNOWN_STRATEGIES
                    and not (tt_raw.lower() == "development" and has_vintage))

    # Mark-to-Market checked first — specific keywords override Claude
    # "below" + "submarket" requires proximity (same sentence) to avoid false positives
    # e.g. "supply well below historic peaks" + "submarket" elsewhere ≠ MTM
    def _below_submarket_proximity(txt, max_dist=80):
        import re
        for m in re.finditer(r"below", txt):
            chunk = txt[max(0, m.start() - max_dist):m.end() + max_dist]
            if "submarket" in chunk:
                return True
        return False
    _mtm_hit = ("mark-to-market" in text_to_scan
                or "statutory rent premium" in text_to_scan
                or "statutory premium" in text_to_scan
                or _below_submarket_proximity(text_to_scan)
                or "untapped value" in text_to_scan)
    if _mtm_hit:
        resolved_tt = "Mark-to-Market"
    elif trust_claude:
        resolved_tt = tt_raw
    elif any(kw in text_to_scan for kw in [
        "shovel ready", "entitled to build", "entitled for",
        "ground-up", "ground up", "to-be-built", "forward purchase",
    ]):
        resolved_tt = "Development"
    elif any(kw in text_to_scan for kw in [
        "lease-up", "lease up", "recently delivered", "delivered vacant",
    ]) and not (has_vintage and any(kw in text_to_scan for kw in [
        "value-add", "value add", "reposition", "renovation",
    ])):
        resolved_tt = "Lease-up"
    elif (any(kw in text_to_scan for kw in [
        "value-add", "value add", "renovation opportunity",
        "classic condition", "reposition",
        "renovation on remaining", "continue renovations",
        "interior upgrade", "partially renovated",
        "unrenovated", "un-renovated", "deferred maintenance",
        "renovation roadmap", "renovation upside",
    ]) or ("remaining" in notes_lower and "unit" in notes_lower and "classic" in notes_lower)
       or "opportunity to continue renovat" in notes_lower
       or ("original" in notes_lower and "renovated" in notes_lower)):
        resolved_tt = "Value-add"
    elif any(kw in text_to_scan for kw in [
        "stabilized", "core plus", "fully occupied", "fully leased",
    ]):
        resolved_tt = "Core"
    elif has_vintage and (_safe_int(fields.get("vintage")) or 0) >= 2020:
        # New construction with no value-add keywords = Core
        resolved_tt = "Core"
    else:
        resolved_tt = None

    result["transaction_type"] = resolved_tt

    # --- Component scores (always computed) ---

    # Geo — MSA override first, then state tiers
    msa_raw = str(fields.get("msa") or "").lower()
    state = str(fields.get("state") or "").upper()
    if "washington" in msa_raw and ("arlington" in msa_raw or "alexandria" in msa_raw):
        geo = 2.0
    elif state in _TIER1:
        geo = 2.0
    elif state in _TIER2:
        geo = 1.0
    elif state in _SUNBELT_ADJ:
        geo = 0.5
    else:
        geo = 0.0
    result["residential_geo_score"] = geo

    # Units
    if units is None:
        units_sc = 0.0
    elif 100 <= units <= 500:
        units_sc = 2.0
    elif 80 <= units <= 99 or 501 <= units <= 600:
        units_sc = 1.0
    else:
        units_sc = 0.0
    result["residential_units_score"] = units_sc

    # Price
    if price is None or price < 30_000_000 or price > 120_000_000:
        price_sc = 0.0
    elif price <= 60_000_000:
        price_sc = round(1.0 + ((price - 30_000_000) / 30_000_000) * 1.0, 2)
    else:
        price_sc = round(2.0 - ((price - 60_000_000) / 60_000_000) * 1.0, 2)
    result["residential_price_score"] = price_sc

    # Rent (fallback to 'rent' field if avg_rent_monthly not set)
    rent = _safe_float(fields.get("avg_rent_monthly")) or _safe_float(fields.get("rent"))
    if rent is not None and 1300 <= rent <= 2500:
        rent_sc = 1.5
    elif rent is not None and 2501 <= rent <= 2800:
        rent_sc = 0.5
    else:
        rent_sc = 0.0
    result["residential_rent_score"] = rent_sc

    # Vintage
    vintage = _safe_int(fields.get("vintage"))
    if vintage is not None and vintage >= 2000:
        vint_sc = 1.0
    elif vintage is not None and 1980 <= vintage <= 1999:
        vint_sc = 0.5
    else:
        vint_sc = 0.0
    # renovation bonus
    reno_boost = 0.0
    if (fields.get("renovated") is True and
            _safe_int(fields.get("renovation_year")) is not None and
            _safe_int(fields.get("renovation_year")) >= 2022):
        reno_boost = 0.25
        vint_sc = min(1.0, vint_sc + reno_boost)
    result["residential_reno_boost"] = reno_boost
    result["residential_vintage_score"] = vint_sc

    # Returns
    ret_sc = 0.0
    gicy = _safe_float(fields.get("going_in_cash_yield"))
    if gicy is not None:
        if gicy >= 5.5:
            ret_sc = 1.5
        elif gicy >= 5.0:
            ret_sc = 1.0
        elif gicy >= 4.5:
            ret_sc = 0.5
    dtr = _safe_float(fields.get("discount_to_replacement"))
    if dtr is not None and dtr >= 15.0:
        ret_sc += 0.5
    ret_sc = min(1.5, ret_sc)
    result["residential_returns_score"] = ret_sc

    # Strategy (uses resolved transaction_type)
    rtt = str(resolved_tt or "").lower()
    if any(kw in rtt for kw in _DEV_KW):
        strat_sc = 0.5
    elif any(kw in rtt for kw in _MTM_KW):
        strat_sc = 0.2
    elif any(kw in rtt for kw in _CORE_KW):
        strat_sc = 0.3
    elif any(kw in rtt for kw in _VA_KW):
        strat_sc = 0.2
    else:
        strat_sc = 0.0
    result["residential_strategy_score"] = strat_sc

    # Notes boost — keyword matching on notes text + Claude positive_signals
    has_negative = (
        "crime" in notes_lower
        or ("collection" in notes_lower and "issue" in notes_lower)
        or "insurance shock" in notes_lower
        or "hoa litigation" in notes_lower
    )
    pos_signals = []

    # Also pull in Claude's positive_signals for combined matching
    # Cross-check "below-market rents" — only trust if text has supporting evidence
    _raw_signals = [s.lower() for s in (fields.get("positive_signals") or []) if isinstance(s, str)]
    _bmr_evidence = ("below" in text_to_scan and ("market" in text_to_scan or "submarket" in text_to_scan)) \
        or "mark-to-market" in text_to_scan or "under-rented" in text_to_scan \
        or "under rented" in text_to_scan or "loss to lease" in text_to_scan
    claude_signals = set(
        s for s in _raw_signals
        if s != "below-market rents" or _bmr_evidence
    )

    # Strong schools — require specific district name, ISD, or ranking context
    # Generic "top local schools" marketing language does NOT count
    def _schools_proximity(txt):
        import re
        # "top" near "school" — require phrases like "top-rated school district", not "top local schools"
        for m in re.finditer(r"top.{0,20}school", txt):
            ctx = m.group()
            if any(kw in ctx for kw in ["district", "rated", "ranked", "10", "performing"]):
                return True
        # "ranked" near "school"
        for m in re.finditer(r"ranked.{0,20}school|school.{0,20}ranked", txt):
            return True
        return False
    if "school district" in text_to_scan \
       or " isd " in text_to_scan or text_to_scan.endswith(" isd") \
       or _schools_proximity(text_to_scan) \
       or "strong school district" in claude_signals:
        pos_signals.append("strong schools")
    if ("limited" in text_to_scan and "supply" in text_to_scan) or \
       ("minimal" in text_to_scan and "supply" in text_to_scan) or \
       ("supply" in text_to_scan and "constrain" in text_to_scan) or \
       "no new supply" in text_to_scan or \
       ("barrier" in text_to_scan and "entry" in text_to_scan) or \
       ("limited" in text_to_scan and "land" in text_to_scan) or \
       ("construction cost" in text_to_scan) or \
       ("zero" in text_to_scan and "construction" in text_to_scan) or \
       ("no" in text_to_scan and "under construction" in text_to_scan) or \
       ("scarcity" in text_to_scan and "develop" in text_to_scan) or \
       "limited new supply" in claude_signals or \
       "limited supply" in claude_signals:
        pos_signals.append("limited supply")
    if "assumable" in text_to_scan or "loan assumption" in text_to_scan or "assumable debt" in claude_signals:
        pos_signals.append("assumable debt")
    dtr = _safe_float(fields.get("discount_to_replacement"))
    if dtr is not None and dtr >= 15.0:
        pos_signals.append("below replacement cost")
    if ("institutional" in text_to_scan and ("sponsor" in text_to_scan or "quality" in text_to_scan)) \
       or "institutional sponsor" in claude_signals:
        pos_signals.append("institutional sponsor")
    if (_mtm_hit
       or "below-market rents" in claude_signals
       or "rent upside" in claude_signals
       or "mark-to-market" in claude_signals):
        pos_signals.append("rent upside")

    if has_negative:
        notes_boost = 0.0
        result["notes_score_reason"] = "blocked by negative signal"
    elif len(pos_signals) >= 3:
        notes_boost = 2.0
        result["notes_score_reason"] = f"2 points notes maximum: {', '.join(pos_signals)}"
    elif len(pos_signals) == 2:
        notes_boost = 2.0
        result["notes_score_reason"] = f"{', '.join(pos_signals)}"
    elif len(pos_signals) == 1:
        notes_boost = 1.0
        result["notes_score_reason"] = f"{', '.join(pos_signals)}"
    else:
        notes_boost = 0.0
        result["notes_score_reason"] = None
    result["residential_notes_boost"] = notes_boost

    # --- Total ---
    if gated:
        result["residential_interest_score"] = 1.0
    else:
        raw_total = (geo + units_sc + price_sc + rent_sc + vint_sc +
                     ret_sc + strat_sc + notes_boost)
        result["residential_interest_score"] = round(min(10.0, raw_total), 2)

    result["score"] = round(
        max(result["residential_interest_score"],
            result["industrial_interest_score"]), 2
    )

    # --- Missing inputs ---
    missing = []
    if units is None:
        missing.append("units")
    if price is None:
        missing.append("guidance_price")
    if rent is None:
        missing.append("avg_rent_monthly")
    if vintage is None:
        missing.append("vintage")
    cap_rate = _safe_float(fields.get("in_place_cap_rate"))
    if cap_rate is None:
        missing.append("in_place_cap_rate")
    if gicy is None:
        missing.append("going_in_cash_yield")
    if missing:
        result["score_missing_inputs"] = " | ".join(missing)

    _build_breakdown(result)
    return result


def _build_breakdown(r: Dict[str, Any]) -> None:
    """Build the score_breakdown audit string."""
    r["score_breakdown"] = (
        f"geo={r.get('residential_geo_score', 0):.2f} | "
        f"units={r.get('residential_units_score', 0):.2f} | "
        f"price={r.get('residential_price_score', 0):.2f} | "
        f"rent={r.get('residential_rent_score', 0):.2f} | "
        f"vintage={r.get('residential_vintage_score', 0):.2f} | "
        f"reno_boost={r.get('residential_reno_boost', 0):.2f} | "
        f"returns={r.get('residential_returns_score', 0):.2f} | "
        f"strategy={r.get('residential_strategy_score', 0):.2f} | "
        f"notes={r.get('residential_notes_boost', 0):.2f} | "
        f"score={r.get('score', 0):.2f}"
    )


def extract_deal_metadata(subject: str, full_text: str) -> Dict[str, Any]:
    """Send scraped+OCR text to Claude and get structured deal fields back
    using the Bluerock scoring spec (v1)."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_EXTRACT_MODEL", "claude-sonnet-4-20250514")
    if not api_key:
        return {"is_deal": False}

    from anthropic import Anthropic

    user_msg = f"SUBJECT: {subject}\n\nCONTENT:\n{full_text[:12000]}\n"

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=3000,
        system=BLUEROCK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    parts = getattr(msg, "content", None) or []
    raw = "\n".join(
        getattr(p, "text", "") for p in parts if getattr(p, "type", None) == "text"
    ).strip()
    if not raw:
        return {"is_deal": False}

    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return {"is_deal": False, "notes": f"parse error: {raw[:200]}"}
        data = json.loads(m.group(0))

    return data if isinstance(data, dict) else {"is_deal": False}


# ---------------------------------------------------------------------------
# MSA resolution: city+state → standardized Census MSA name
# ---------------------------------------------------------------------------

_MSA_CACHE: Dict[str, str] = {}

_MSA_SYSTEM_PROMPT = """\
You are a US geography lookup tool. Given a city and state, return the \
official OMB / Census Bureau Metropolitan Statistical Area (MSA) title.

Output format MUST be exactly: "PrincipalCity-City2-City3, ST" or \
"PrincipalCity-City2-City3, ST-ST" for multi-state MSAs.

Rules:
- Use the FULL official title with all principal cities \
(e.g. "Charlotte-Concord-Gastonia, NC-SC", NOT "Charlotte, NC").
- Use SINGLE hyphens between city names (never double hyphens). \
For example: "Nashville-Davidson-Murfreesboro-Franklin, TN" NOT \
"Nashville-Davidson--Murfreesboro--Franklin, TN".
- Always include the state abbreviation suffix after a comma and space.
- If the city is in a Metropolitan Division, return the parent MSA title.
- If the city is not in any MSA (rural), return "Non-MSA".
- Do NOT guess. If unsure, return "Non-MSA".
- Output ONLY the MSA title string — no quotes, no explanation.
"""


def resolve_msa(city: str, state: str) -> Optional[str]:
    """Look up the standardized MSA for a city+state via a small Claude call.
    Results are cached in-memory so the same combo is never queried twice."""
    if not city or not state:
        return None

    cache_key = f"{city.strip().lower()}|{state.strip().upper()}"
    if cache_key in _MSA_CACHE:
        return _MSA_CACHE[cache_key]

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=120,
        system=_MSA_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"{city}, {state}"}],
    )
    parts = getattr(msg, "content", None) or []
    msa = "\n".join(
        getattr(p, "text", "") for p in parts if getattr(p, "type", None) == "text"
    ).strip()

    if not msa or msa.lower() == "non-msa":
        _MSA_CACHE[cache_key] = "Non-MSA"
        return "Non-MSA"

    _MSA_CACHE[cache_key] = msa
    return msa


# ---------------------------------------------------------------------------
# Web search fallback: avg rent from property listing sites
# ---------------------------------------------------------------------------

_RENT_CACHE: Dict[str, Optional[float]] = {}

_RENT_RE = re.compile(r'\$\s*([\d,]+(?:\.\d+)?)')


def web_search_avg_rent(property_name: str, city: str, state: str) -> Optional[float]:
    """Use Anthropic web search to find average monthly rent for a property.
    Searches apartments.com / property websites. Results cached in-memory."""
    if not property_name or not city or not state:
        return None

    cache_key = f"{property_name.strip().lower()}|{city.strip().lower()}|{state.strip().upper()}"
    if cache_key in _RENT_CACHE:
        return _RENT_CACHE[cache_key]

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)

    query = (
        f"What is the current average monthly rent at {property_name} "
        f"in {city}, {state}? "
        f"Check apartments.com or the property website. "
        f"IMPORTANT: Only use rents for a 10-to-12 month lease term with a "
        f"move-in date within the next 30 days (i.e. the soonest available). "
        f"Do NOT use rents for move-in dates months away — those are often "
        f"artificially lower or higher due to seasonal pricing. "
        f"Return ONLY a single dollar amount like $1,450. "
        f"If you find a range of unit types, return the midpoint across all "
        f"floor plans. If you cannot find 10-to-12 month / near-term rent, return UNKNOWN."
    )

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            tools=[{"type": "web_search_20250305"}],
            messages=[{"role": "user", "content": query}],
        )
    except Exception:
        _RENT_CACHE[cache_key] = None
        return None

    # Extract the text response
    parts = getattr(msg, "content", None) or []
    answer = "\n".join(
        getattr(p, "text", "") for p in parts if getattr(p, "type", None) == "text"
    ).strip()

    if not answer or "UNKNOWN" in answer.upper():
        _RENT_CACHE[cache_key] = None
        return None

    # Parse dollar amount from response
    matches = _RENT_RE.findall(answer)
    if not matches:
        _RENT_CACHE[cache_key] = None
        return None

    try:
        rent = float(matches[0].replace(",", ""))
        # Sanity check: rent should be between $500 and $5000/month
        if 500 <= rent <= 5000:
            _RENT_CACHE[cache_key] = rent
            return rent
    except ValueError:
        pass

    _RENT_CACHE[cache_key] = None
    return None


# ---------------------------------------------------------------------------
# State: track which UIDs we've already processed
# ---------------------------------------------------------------------------

def load_processed_uids() -> set:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("processed_uids", []))
        except Exception:
            pass
    return set()


def save_processed_uids(uids: set) -> None:
    STATE_FILE.write_text(json.dumps({"processed_uids": sorted(uids)}))


# ---------------------------------------------------------------------------
# Calendar push (Fastmail CalDAV)
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _maybe_push_calendar_event(uid: int, fields: Dict[str, Any]) -> None:
    """If the deal has a real date offer_due_date, push it to Fastmail calendar."""
    if not fields or not fields.get("is_deal", True):
        return
    offers_due = str(
        fields.get("offer_due_date") or fields.get("offers_due_date") or ""
    ).strip()

    from datetime import datetime, timedelta

    # Try ISO 8601 first (YYYY-MM-DD), then legacy MM-DD-YYYY
    m = _ISO_DATE_RE.match(offers_due)
    if m:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        m = _DATE_RE.match(offers_due)
        if not m:
            return  # not a real date
        dt = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    event_uid = f"dealsifta-{uid}@openclaw"

    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=IMAP_USER,
        password=IMAP_PASS,
    )
    principal = client.principal()

    # Find or create the calendar
    cal = None
    for c in principal.calendars():
        if c.name == CALENDAR_NAME:
            cal = c
            break
    if cal is None:
        cal = principal.make_calendar(name=CALENDAR_NAME)

    # Check for existing event to avoid duplicates
    try:
        for event in cal.events():
            if f"UID:{event_uid}" in str(event.data):
                print(f"    -> Calendar: already exists, skipped", flush=True)
                return
    except Exception:
        pass

    prop = str(fields.get("deal_name") or fields.get("property_name") or "Unknown")
    city = str(fields.get("city") or "")
    state = str(fields.get("state") or "")
    msa = str(fields.get("msa") or "")
    units = fields.get("units") or "?"
    broker = str(fields.get("broker") or fields.get("broker_company") or "")
    location = f"{city}, {state}" if city else ""

    dtstart = dt.strftime("%Y%m%d")
    dtend = (dt + timedelta(days=1)).strftime("%Y%m%d")
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    vevent = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//DealSifta//Offers Due//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{event_uid}\r\n"
        f"DTSTAMP:{now}\r\n"
        f"DTSTART;VALUE=DATE:{dtstart}\r\n"
        f"DTEND;VALUE=DATE:{dtend}\r\n"
        f"SUMMARY:OFFERS DUE: {prop}\r\n"
        f"LOCATION:{location}\r\n"
        f"DESCRIPTION:Property: {prop}\\nLocation: {location}\\n"
        f"Metro: {msa}\\nUnits: {units}\\nBroker: {broker}\r\n"
        "STATUS:CONFIRMED\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT1H\r\n"
        "ACTION:DISPLAY\r\n"
        f"DESCRIPTION:Offers due today: {prop}\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )
    cal.save_event(vevent)
    print(f"    -> Calendar: added {offers_due} — {prop}", flush=True)


# ---------------------------------------------------------------------------
# Core: process any new UIDs
# ---------------------------------------------------------------------------

def process_new_emails(M: imaplib.IMAP4_SSL) -> int:
    """For each new INBOX email: scrape HTML + OCR images → APPEND to OCRoutput."""
    known = load_processed_uids()

    all_uids = imap_fetch_uids(M)
    new_uids = [u for u in all_uids if u not in known]

    if not new_uids:
        return 0

    print(f"[watcher] {len(new_uids)} new email(s)", flush=True)
    processed = 0

    for uid in new_uids:
        email_data = imap_fetch_full(M, uid)
        subject = email_data["subject"]
        email_from = email_data.get("from", "")
        body = email_data["body"]
        html = email_data["html"]
        images = email_data["images"]
        _ts = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n{'='*72}", flush=True)
        print(f"[{_ts}] uid={uid} | {subject[:80]}", flush=True)
        print(f"{'='*72}", flush=True)

        # --- Scrape HTML to text ---
        scraped_text = body
        if not scraped_text and html:
            scraped_text = _html_to_text(html)

        # --- Pre-OCR gating: skip OCR if plain text shows deal is gated ---
        pre_gated, gate_reason = _pre_ocr_gating_check(subject, scraped_text)
        if pre_gated:
            print(f"    -> PRE-OCR GATED ({gate_reason}), skipping OCR", flush=True)

        # --- OCR all image attachments (skip if pre-gated) ---
        ocr_results: List[Tuple[str, str]] = []
        if not pre_gated:
            for fname, img_data, media_type in images:
                print(f"    OCR: {fname} ({len(img_data)} bytes)", flush=True)
                try:
                    ocr_text = ocr_image(img_data, media_type, fname)
                    if ocr_text and ocr_text.strip().upper() != "NONE":
                        ocr_results.append((fname, ocr_text))
                        print(f"    OCR done: {len(ocr_text)} chars", flush=True)
                    else:
                        print(f"    OCR: no text in image, skipped", flush=True)
                except Exception as ex:
                    print(f"    OCR failed: {ex}", flush=True)

        # --- Link-following fallback: if OCR yielded thin results, scrape
        #     "View in browser" / "View Website" links for the full narrative ---
        link_text = ""
        if not pre_gated and html:
            total_ocr_chars = sum(len(txt) for _, txt in ocr_results)
            if total_ocr_chars < 200:
                link_text = _scrape_view_links(html)
                if link_text:
                    print(f"    -> Link-follow: scraped {len(link_text)} chars from view link", flush=True)

        # --- Combine all text for extraction ---
        full_text = scraped_text
        if ocr_results:
            ocr_combined = "\n\n".join(f"[{fn}]:\n{txt}" for fn, txt in ocr_results)
            full_text = f"{scraped_text}\n\n--- OCR FROM IMAGES ---\n{ocr_combined}"
        if link_text:
            full_text = f"{full_text}\n\n--- SCRAPED FROM VIEW LINK ---\n{link_text}"

        # --- Claude structured extraction → Azure SQL ---
        fields = {}
        try:
            fields = extract_deal_metadata(subject, full_text)
            is_deal = fields.get("is_deal", True)
            deal_name = fields.get("deal_name") or fields.get("property_name") or "?"
            print(f"    -> Extract: {deal_name} (deal={is_deal})", flush=True)

            # --- Normalize asset_class for residential deals ---
            if is_deal:
                product = str(fields.get("product") or "").upper()
                ac = str(fields.get("asset_class") or "").lower()
                deal_lower = str(fields.get("deal_name") or "").lower()
                notes_lc = str(fields.get("notes") or "").lower()
                subj_lc = str(fields.get("email_subject") or
                              fields.get("subject") or "").lower()
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

            # --- Normalize broker_firm: M&M → IPA ---
            bf = str(fields.get("broker_firm") or "").strip().lower()
            if "marcus" in bf and "millichap" in bf:
                fields["broker_firm"] = "Institutional Property Advisors"

            # --- Normalize offer_due_date → MM-DD-YYYY ---
            raw_date = fields.get("offer_due_date")
            if raw_date and isinstance(raw_date, str):
                try:
                    parsed_dt = dateutil_parser.parse(raw_date, fuzzy=True)
                    fields["offer_due_date"] = parsed_dt.strftime("%m-%d-%Y")
                except (ValueError, OverflowError):
                    pass  # keep raw string if unparseable

            # --- Standardized MSA lookup from city+state ---
            if is_deal and fields.get("city") and fields.get("state"):
                try:
                    msa = resolve_msa(fields["city"], fields["state"])
                    if msa:
                        fields["msa"] = msa
                        print(f"    -> MSA: {msa}", flush=True)
                except Exception as ex:
                    print(f"    -> MSA lookup failed: {ex}", flush=True)

            # --- Deterministic vintage fallback from OCR/scraped text ---
            if is_deal and not fields.get("vintage"):
                combined_text = (full_text or "") + " " + (ocr_text or "")
                regex_vintage = _extract_vintage_from_text(combined_text)
                if regex_vintage:
                    fields["vintage"] = regex_vintage
                    print(f"    -> Regex vintage: {regex_vintage}", flush=True)

            # --- Deterministic rent fallback from OCR/scraped text ---
            if is_deal and not fields.get("avg_rent_monthly") and not fields.get("rent"):
                combined_text = (full_text or "") + " " + (ocr_text or "")
                regex_rent = _extract_rent_from_text(combined_text)
                if regex_rent:
                    fields["avg_rent_monthly"] = regex_rent
                    print(f"    -> Regex rent: ${regex_rent:,.0f}/mo", flush=True)

            # --- Web search fallback for missing avg_rent_monthly ---
            if is_deal and not fields.get("avg_rent_monthly") and not fields.get("rent"):
                prop = fields.get("deal_name") or fields.get("property_name")
                c = fields.get("city")
                s = fields.get("state")
                if prop and c and s:
                    try:
                        web_rent = web_search_avg_rent(prop, c, s)
                        if web_rent:
                            fields["avg_rent_monthly"] = web_rent
                            fields["web_avg_rent_monthly"] = web_rent
                            print(f"    -> Web rent: ${web_rent:,.0f}/mo", flush=True)
                        else:
                            print(f"    -> Web rent: not found", flush=True)
                    except Exception as ex:
                        print(f"    -> Web rent search failed: {ex}", flush=True)

            # --- Deterministic Bluerock scoring (Python) ---
            if is_deal:
                fields["_full_text"] = full_text
                scoring = compute_bluerock_score(fields)
                fields.update(scoring)
                print(f"    -> Score: {fields['score']} ({fields['score_breakdown']})", flush=True)

                # Send to Telegram QA bot (non-blocking)
                try:
                    from scoring_qa_bot import send_deal_review
                    fields["uid"] = uid
                    send_deal_review(fields, scoring)
                except Exception as ex:
                    print(f"    -> QA bot: {ex}", flush=True)

            conn = ensure_db()
            # Insert into raw_deal_emails (raw layer for dbt)
            try:
                ocr_combined = "\n\n".join(f"[{fn}]:\n{txt}" for fn, txt in ocr_results) if ocr_results else None
                insert_raw_deal_email(
                    conn, uid, subject, email_from,
                    scraped_text, ocr_combined,
                    json.dumps(fields, default=str),
                    fields, pre_gated, gate_reason,
                )
                print(f"    -> raw_deal_emails: OK", flush=True)
            except Exception as ex:
                print(f"    -> raw_deal_emails insert failed: {ex}", flush=True)
            # Insert into new deal_intake table (Bluerock spec)
            try:
                insert_deal_intake(conn, uid, full_text, fields)
                print(f"    -> deal_intake: OK", flush=True)
            except Exception as ex:
                print(f"    -> deal_intake insert failed: {ex}", flush=True)
            # Also insert into legacy deals table for backward compat
            try:
                # Map new field names to old schema
                legacy = dict(fields)
                legacy.setdefault("property_name", deal_name)
                legacy.setdefault("asset_type", fields.get("asset_class"))
                legacy.setdefault("asking_price", fields.get("guidance_price"))
                legacy.setdefault("year_built", fields.get("vintage"))
                legacy.setdefault("avg_unit_size", fields.get("unit_size"))
                legacy.setdefault("avg_rent", fields.get("avg_rent_monthly"))
                legacy.setdefault("cap_rate", fields.get("in_place_cap_rate"))
                legacy.setdefault("broker_name", fields.get("broker"))
                legacy.setdefault("highlights", fields.get("notes"))
                legacy.setdefault("offers_due_date", fields.get("offer_due_date"))
                insert_deal(conn, uid, subject, full_text, legacy)
            except Exception as ex:
                print(f"    -> legacy deals insert failed: {ex}", flush=True)
            conn.close()
        except Exception as ex:
            print(f"    -> DB extraction failed: {ex}", flush=True)

        # --- Push to Fastmail calendar if offers_due_date is a real date ---
        try:
            _maybe_push_calendar_event(uid, fields)
        except Exception as ex:
            print(f"    -> Calendar push failed: {ex}", flush=True)

        # --- APPEND OCR output to INBOX/OCRoutput (with dedup) ---
        ocr_append_ok = False
        if _ocr_already_exists(M, uid):
            print(f"    -> OCRoutput: already exists, skipped", flush=True)
            ocr_append_ok = True
        else:
            try:
                rfc822 = compose_ocr_message(
                    subject=subject, uid=uid,
                    scraped_text=scraped_text, ocr_results=ocr_results,
                )
                append_to_ocr_folder(M, rfc822)
                M.select(MAILBOX)
                print(f"    -> OCRoutput: appended", flush=True)
                ocr_append_ok = True
            except Exception as ex:
                print(f"    -> OCRoutput append failed: {ex}", flush=True)
                try:
                    M.select(MAILBOX)
                except Exception:
                    pass

        # Mark as processed only if OCR output succeeded
        if ocr_append_ok:
            known.add(uid)
            save_processed_uids(known)
        processed += 1

        # Append one-liner to run_history.log
        try:
            _ts = time.strftime('%Y-%m-%d %H:%M:%S')
            if pre_gated:
                _line = f"[{_ts}] uid={uid} | GATED ({gate_reason}) | {subject[:60]}"
            elif 'fields' in dir() and fields:
                _dn = fields.get('deal_name') or '?'
                _ac = fields.get('asset_class') or '?'
                _tt = fields.get('transaction_type') or '-'
                _sc = fields.get('score', '?')
                _nr = fields.get('notes_score_reason') or '-'
                _sb = fields.get('score_breakdown') or ''
                _line = f"[{_ts}] uid={uid} | {_dn} | {_ac} | {_tt} | score={_sc} | {_sb} | notes: {_nr}"
            else:
                _line = f"[{_ts}] uid={uid} | ERROR | {subject[:60]}"
            _hist = os.path.join(os.path.dirname(__file__), "run_history.log")
            with open(_hist, "a") as fh:
                fh.write(_line + "\n")
        except Exception:
            pass

    if processed:
        print(f"[watcher] Processed {processed} email(s)", flush=True)
    return processed


# ---------------------------------------------------------------------------
# dbt pipeline trigger
# ---------------------------------------------------------------------------

DBT_PROJECT_DIR = os.path.join(os.path.dirname(__file__), "..", "deal-pipeline")
DBT_VENV_BIN = os.path.join(DBT_PROJECT_DIR, ".venv", "bin", "dbt")
DBT_PROFILES_DIR = os.path.join(DBT_PROJECT_DIR, "cli")


def run_dbt_pipeline():
    """Run dbt pipeline to refresh mart_deals after new emails are processed."""
    import subprocess
    if not os.path.isfile(DBT_VENV_BIN):
        print(f"[dbt] dbt binary not found at {DBT_VENV_BIN}, skipping", flush=True)
        return
    env = os.environ.copy()
    env.setdefault("AZURE_SQL_USER", os.getenv("AZURE_SQL_USER", ""))
    env.setdefault("AZURE_SQL_PASSWORD", os.getenv("AZURE_SQL_PASSWORD", ""))
    try:
        print("[dbt] Running dbt pipeline...", flush=True)
        result = subprocess.run(
            [DBT_VENV_BIN, "run", "--profiles-dir", DBT_PROFILES_DIR, "--project-dir", DBT_PROJECT_DIR],
            capture_output=True, text=True, timeout=120, env=env,
        )
        # Print summary line
        for line in result.stdout.splitlines():
            if "PASS=" in line or "ERROR" in line or "Completed" in line:
                print(f"[dbt] {line.strip()}", flush=True)
        if result.returncode != 0:
            print(f"[dbt] dbt run failed (exit {result.returncode})", flush=True)
            for line in result.stderr.splitlines()[-5:]:
                print(f"[dbt]   {line}", flush=True)
        else:
            print("[dbt] mart_deals refreshed.", flush=True)
    except subprocess.TimeoutExpired:
        print("[dbt] dbt run timed out after 120s", flush=True)
    except Exception as ex:
        print(f"[dbt] dbt run failed: {ex}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Event-based IMAP IDLE watcher → deal_metadata.db")
    ap.add_argument("--once", action="store_true", help="Process pending then exit (no IDLE loop)")
    args = ap.parse_args()

    print(f"[watcher] OCR output → {OCR_OUTPUT_FOLDER}", flush=True)
    print(f"[watcher] Azure SQL → {os.getenv('AZURE_SQL_SERVER')}/{os.getenv('AZURE_SQL_DATABASE')}", flush=True)
    print(f"[watcher] Connecting to {IMAP_HOST}:{IMAP_PORT} as {IMAP_USER}", flush=True)

    M = imap_connect()
    print(f"[watcher] Connected. Mailbox: {MAILBOX}", flush=True)

    # Always process anything pending first
    n = process_new_emails(M)
    if n:
        run_dbt_pipeline()

    if args.once:
        M.logout()
        print("[watcher] Done (--once).", flush=True)
        return

    print(f"[watcher] Entering IMAP IDLE loop (Ctrl+C to stop)", flush=True)
    try:
        while True:
            try:
                got_event = idle_wait(M, timeout=IDLE_TIMEOUT)
                if got_event:
                    print("[watcher] IDLE event — checking for new mail...", flush=True)
                    # Re-select to refresh UID state
                    M.select(MAILBOX)
                    n = process_new_emails(M)
                    if n:
                        run_dbt_pipeline()
                    # Keep draining — emails may have arrived during processing
                    while True:
                        M.select(MAILBOX)
                        n2 = process_new_emails(M)
                        if not n2:
                            break
                        print(f"[watcher] Found {n2} more email(s) queued during processing", flush=True)
                        run_dbt_pipeline()
                else:
                    # Timeout — re-issue IDLE (keeps connection alive)
                    M.noop()
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as ex:
                print(f"[watcher] Connection lost ({ex}), reconnecting...", flush=True)
                time.sleep(5)
                try:
                    M = imap_connect()
                    print("[watcher] Reconnected.", flush=True)
                    n = process_new_emails(M)
                    if n:
                        run_dbt_pipeline()
                except Exception as ex2:
                    print(f"[watcher] Reconnect failed: {ex2}", flush=True)
                    time.sleep(30)
    except KeyboardInterrupt:
        print("\n[watcher] Shutting down.", flush=True)

    try:
        M.logout()
    except Exception:
        pass


if __name__ == "__main__":
    main()
