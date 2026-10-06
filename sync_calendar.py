"""Push offers_due_date from Azure SQL deals to Fastmail calendar via CalDAV."""
import pymssql
import caldav
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

CALDAV_URL = "https://caldav.fastmail.com/dav/calendars"
FM_USER = os.getenv("FASTMAIL_IMAP_USER")
FM_PASS = os.getenv("FASTMAIL_IMAP_PASSWORD")
CALENDAR_NAME = "Offers Due"  # Will create if doesn't exist

DATE_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")


def get_deals_with_dates():
    """Fetch deals with actual date-format offers_due_date from Azure SQL."""
    conn = pymssql.connect(
        server=os.getenv("AZURE_SQL_SERVER"),
        user=os.getenv("AZURE_SQL_USER"),
        password=os.getenv("AZURE_SQL_PASSWORD"),
        database=os.getenv("AZURE_SQL_DATABASE"),
    )
    cursor = conn.cursor(as_dict=True)
    cursor.execute("""
        SELECT uid, property_name, city, state, msa, units,
               offers_due_date, broker_company, email_type
        FROM deals
        WHERE is_deal = 1
          AND offers_due_date IS NOT NULL
          AND offers_due_date LIKE '[0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9]'
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def parse_date(date_str):
    """Parse MM-DD-YYYY to datetime."""
    m = DATE_RE.match(date_str)
    if not m:
        return None
    return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))


def get_or_create_calendar(client):
    """Get or create the 'Offers Due' calendar."""
    principal = client.principal()
    calendars = principal.calendars()

    for cal in calendars:
        if cal.name == CALENDAR_NAME:
            print(f"  Using existing calendar: {CALENDAR_NAME}")
            return cal

    # Create new calendar
    cal = principal.make_calendar(name=CALENDAR_NAME)
    print(f"  Created new calendar: {CALENDAR_NAME}")
    return cal


def build_vevent(deal, dt):
    """Build an iCalendar VEVENT string for a deal."""
    uid_val = f"dealsifta-{deal['uid']}@openclaw"
    summary = f"OFFERS DUE: {deal['property_name']}"
    location = f"{deal['city']}, {deal['state']}" if deal['city'] else ""
    units = deal['units'] or "?"
    broker = deal['broker_company'] or "Unknown"
    msa = deal['msa'] or ""

    description = (
        f"Property: {deal['property_name']}\\n"
        f"Location: {deal['city']}, {deal['state']}\\n"
        f"Metro: {msa}\\n"
        f"Units: {units}\\n"
        f"Broker: {broker}\\n"
        f"Email Type: {deal['email_type'] or ''}\\n"
    )

    dtstart = dt.strftime("%Y%m%d")
    dtend = (dt + timedelta(days=1)).strftime("%Y%m%d")
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//DealSifta//Offers Due//EN
BEGIN:VEVENT
UID:{uid_val}
DTSTAMP:{now}
DTSTART;VALUE=DATE:{dtstart}
DTEND;VALUE=DATE:{dtend}
SUMMARY:{summary}
LOCATION:{location}
DESCRIPTION:{description}
STATUS:CONFIRMED
BEGIN:VALARM
TRIGGER:-PT1H
ACTION:DISPLAY
DESCRIPTION:Offers due today: {deal['property_name']}
END:VALARM
END:VEVENT
END:VCALENDAR"""


def main():
    print("Connecting to Fastmail CalDAV...")
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=FM_USER,
        password=FM_PASS,
    )

    calendar = get_or_create_calendar(client)

    # Get existing event UIDs to avoid duplicates
    existing_uids = set()
    try:
        for event in calendar.events():
            ical_text = str(event.data)
            for line in ical_text.splitlines():
                if line.startswith("UID:dealsifta-"):
                    existing_uids.add(line.split(":", 1)[1].strip())
    except Exception:
        pass

    deals = get_deals_with_dates()
    print(f"  Found {len(deals)} deals with offer deadlines")

    added = 0
    skipped = 0
    for deal in deals:
        dt = parse_date(deal["offers_due_date"])
        if not dt:
            continue

        event_uid = f"dealsifta-{deal['uid']}@openclaw"
        if event_uid in existing_uids:
            print(f"  SKIP (exists): {deal['property_name']} — {deal['offers_due_date']}")
            skipped += 1
            continue

        vevent = build_vevent(deal, dt)
        calendar.save_event(vevent)
        print(f"  ADDED: {deal['property_name']} — {deal['offers_due_date']}")
        added += 1

    print(f"\n✓ Done: {added} events added, {skipped} already existed")
    print(f"  Calendar: '{CALENDAR_NAME}' in Fastmail")


if __name__ == "__main__":
    main()
