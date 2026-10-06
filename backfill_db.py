#!/usr/bin/env python3
"""
backfill_db.py – Read already-OCR'd messages from INBOX/OCRoutput,
send text to Claude for structured extraction, insert into deals.db.

No re-OCR needed — just reads the plain-text body of each OCRoutput message.

Usage:
    python3 backfill_db.py
"""
import imaplib
import email as email_lib
import json
import os
import re
import sys
from dotenv import load_dotenv

load_dotenv()

# Reuse DB + extraction from email_watcher
from email_watcher import (
    ensure_db, insert_deal, extract_deal_metadata, DB_PATH,
    IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASS,
)

OCR_FOLDER = "INBOX/OCRoutput"


def main():
    print(f"[backfill] Reading from {OCR_FOLDER}", flush=True)
    print(f"[backfill] Writing to {DB_PATH}", flush=True)

    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(IMAP_USER, IMAP_PASS)
    M.select(OCR_FOLDER)

    typ, data = M.uid("SEARCH", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        print("[backfill] No messages in OCRoutput.")
        M.logout()
        return

    uids = data[0].split()
    print(f"[backfill] Found {len(uids)} OCRoutput messages", flush=True)

    conn = ensure_db()

    # Check which source UIDs are already in the DB
    existing = {row[0] for row in conn.execute("SELECT uid FROM deals").fetchall()}

    processed = 0
    skipped = 0

    for uid_bytes in uids:
        uid = uid_bytes.decode()
        typ2, msg_data = M.uid("FETCH", uid, "(RFC822)")
        if typ2 != "OK" or not msg_data or not msg_data[0]:
            continue

        raw = msg_data[0][1]
        msg = email_lib.message_from_bytes(raw)

        # Get source UID from header
        source_uid_str = msg.get("X-OCR-Source-UID", "")
        if not source_uid_str:
            print(f"  folder_uid={uid}: no X-OCR-Source-UID header, skipping", flush=True)
            continue

        source_uid = int(source_uid_str)
        if source_uid in existing:
            skipped += 1
            continue

        # Get subject (strip [OCR] prefix)
        subject = msg.get("Subject", "")
        original_subject = re.sub(r"^\[OCR\]\s*", "", subject)

        # Get the plain-text body (the OCR output)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

        if not body.strip():
            print(f"  uid={source_uid}: empty body, skipping", flush=True)
            continue

        print(f"  uid={source_uid} {original_subject[:70]}...", end="", flush=True)

        # Claude extraction
        try:
            fields = extract_deal_metadata(original_subject, body)
            insert_deal(conn, source_uid, original_subject, body, fields)
            is_deal = fields.get("is_deal", True)
            prop = fields.get("property_name", "?")
            print(f" -> {prop} (deal={is_deal})", flush=True)
            processed += 1
            existing.add(source_uid)
        except Exception as ex:
            print(f" -> FAILED: {ex}", flush=True)

    conn.close()
    M.logout()
    print(f"\n[backfill] Done: {processed} extracted, {skipped} already in DB", flush=True)


if __name__ == "__main__":
    main()
