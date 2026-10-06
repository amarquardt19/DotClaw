import argparse
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from openpyxl import load_workbook

DEFAULT_URL = "https://www.apartments.com/apartments/san-mateo-county-ca/?bb=0oqxk92g0Ox_p79kO"
DEFAULT_WORKBOOK = str(Path.home() / "Downloads" / "dealsifta apartments.com.xlsx")
DEFAULT_SHEET = None


def _norm_text(val: Any) -> str:
    if val is None:
        return ""
    return " ".join(str(val).split()).strip()


def _build_header_map(ws) -> Dict[str, int]:
    out = {}
    for col in range(1, ws.max_column + 1):
        key = ws.cell(1, col).value
        if key is None:
            continue
        out[str(key).strip().lower()] = col
    return out


def _open_workbook(path: str, sheet_name: Optional[str]):
    wb = load_workbook(path)
    ws = wb[sheet_name] if sheet_name else wb[wb.sheetnames[0]]
    return wb, ws


def _is_property_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.netloc not in {"www.apartments.com", "apartments.com"}:
        return False
    path = (parsed.path or "").strip("/")
    if not path or path.lower().startswith("apartments/"):
        return False
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2 or not all(parts):
        return False
    slug, listing_id = parts
    if not re.fullmatch(r"[a-z0-9-]+", slug, flags=re.I):
        return False
    if not re.fullmatch(r"[a-z0-9]{6,12}", listing_id, flags=re.I):
        return False
    blocked_tokens = {
        "apartments",
        "houses",
        "condos",
        "townhomes",
        "cheap",
        "luxury",
        "pet-friendly",
        "senior",
        "studio",
        "1-bedroom",
        "2-bedroom",
        "3-bedroom",
        "furnished",
        "senior-housing",
        "rental-manager",
        "my-account",
        "moving-center",
        "add-listing",
        "savedsearch",
        "favorites",
        "manage-property",
        "services",
        "grow",
        "pmc",
        "us",
    }
    if slug.lower() in blocked_tokens or listing_id.lower() in blocked_tokens:
        return False
    if re.search(r"county-[a-z]{2}$", slug, re.I):
        return False
    return True


def _collect_listing_urls(page) -> List[str]:
    seen = set()
    results: List[str] = []
    stagnant = 0
    prev_count = 0
    for _ in range(30):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
        urls = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('a[href], a.property-link').forEach((a) => {
                    const href = a.href || '';
                    if (!href.includes('apartments.com/')) return;
                    if (href.includes('/apartments/') || href.includes('#')) return;
                    out.push(href);
                });
                return out;
            }"""
        ) or []
        for url in urls:
            if url in seen or not _is_property_url(url):
                continue
            seen.add(url)
            results.append(url)
        cur_count = len(results)
        stagnant = stagnant + 1 if cur_count == prev_count else 0
        prev_count = cur_count
        if stagnant >= 4:
            break
    return results


def _capture_selected_page_text(page) -> str:
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.5)
        page.evaluate(
            """() => {
                if (document.activeElement && document.activeElement.blur) {
                    document.activeElement.blur();
                }
                if (document.body && document.body.focus) {
                    if (!document.body.hasAttribute('tabindex')) {
                        document.body.setAttribute('tabindex', '-1');
                    }
                    document.body.focus();
                }
            }"""
        )
        time.sleep(0.3)
        page.keyboard.press("Meta+A")
        time.sleep(0.5)
        selected = page.evaluate("() => window.getSelection ? window.getSelection().toString() : ''") or ""
        if selected.strip():
            return selected
    except Exception:
        pass
    return page.evaluate("() => document.body && document.body.innerText ? document.body.innerText : ''") or ""


def _clear_selection(page) -> None:
    try:
        page.evaluate(
            """() => {
                const sel = window.getSelection && window.getSelection();
                if (sel && sel.removeAllRanges) {
                    sel.removeAllRanges();
                }
                if (document.activeElement && document.activeElement.blur) {
                    document.activeElement.blur();
                }
            }"""
        )
    except Exception:
        pass


def _is_dark_forest_green(color_value: str) -> bool:
    if not color_value:
        return False
    m = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", color_value)
    if not m:
        return False
    r, g, b = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return g >= 70 and g <= 130 and r <= 25 and b <= 90 and (g - r) >= 45 and (g - b) >= 10


def _school_bucket(text: str) -> str:
    value = (text or "").lower()
    if "elementary" in value:
        return "elementary"
    if "middle" in value or "intermediate" in value or "grades 6-8" in value or "grades k-8" in value or "grades pk-8" in value:
        return "middle"
    if "high" in value:
        return "high"
    return "other"


def _extract_school_flags(page) -> List[Dict[str, Any]]:
    _clear_selection(page)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)
    result = page.evaluate(
        """() => {
            const txt = (el) => (el && el.innerText ? el.innerText.replace(/\s+/g, ' ').trim() : '');
            const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
            const colorOf = (el) => {
                if (!el) return '';
                const style = window.getComputedStyle(el);
                return [style.color, style.backgroundColor, style.borderColor].filter(Boolean).join(' | ');
            };
            const allGsLinks = Array.from(document.querySelectorAll('a[href*="greatschools.org"]')).map((a) => ({
                href: a.href || '',
                text: txt(a),
                aria: a.getAttribute('aria-label') || '',
                title: a.getAttribute('title') || '',
            }));
            const heading = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')).find((el) => /education|schools/i.test(txt(el)));
            if (heading && heading.scrollIntoView) {
                heading.scrollIntoView({block: 'center'});
            }
            const cards = Array.from(document.querySelectorAll('.schoolsSection .card, .schoolsSection .component-frame, .profilev2SchoolsModule .card, .profilev2SchoolsModule .component-frame, [data-test="schoolCard"], .educationCard, .schoolCard'));
            const out = [];
            for (const card of cards.slice(0, 12)) {
                const raw = txt(card);
                if (!/attendance zone/i.test(raw)) continue;
                const badgeCandidates = Array.from(card.querySelectorAll('div, span, p, strong')).filter((el) => /\/10/.test(txt(el)) || /rating/i.test((el.className || '') + ' ' + (el.getAttribute('data-testid') || '')));
                let badgeColor = '';
                let rating = '';
                for (const el of badgeCandidates) {
                    const t = txt(el);
                    const c = colorOf(el);
                    if (!badgeColor && c) badgeColor = c;
                    if (!rating && /\d+(?:\.\d+)?\/10/.test(t)) rating = t.match(/\d+(?:\.\d+)?\/10/)[0];
                }
                const name = txt(card.querySelector('h3, h4, h5, [class*="name"], a'));
                const inCardGs = Array.from(card.querySelectorAll('a[href*="greatschools.org"]')).map((a) => a.href || '').find(Boolean) || '';
                let matchedGs = inCardGs;
                if (!matchedGs && name) {
                    const nameKey = norm(name);
                    const candidate = allGsLinks.find((item) => {
                        const hay = norm([item.text, item.aria, item.title, item.href].join(' '));
                        return nameKey && hay.includes(nameKey);
                    });
                    matchedGs = candidate ? candidate.href : '';
                }
                out.push({
                    raw,
                    name,
                    grades: txt(Array.from(card.querySelectorAll('div, span, p, li')).find((el) => /grades/i.test(txt(el)))),
                    zone: txt(Array.from(card.querySelectorAll('div, span, p, li')).find((el) => /attendance zone/i.test(txt(el)))) || 'Attendance Zone',
                    rating,
                    badge_color: badgeColor,
                    rating_link: matchedGs,
                });
            }
            return out;
        }"""
    ) or []
    return result


def _parse_rating_text(text: str) -> Optional[str]:
    value = _norm_text(text)
    if not value:
        return None
    match = re.search(r"GreatSchools\s+Rating\D{0,20}(10|[1-9](?:\.\d+)?)\s*/\s*10", value, re.I)
    if match:
        return f"{match.group(1)}/10"
    match = re.search(r"\b(10|[1-9](?:\.\d+)?)\s*/\s*10\b", value)
    if match:
        return f"{match.group(1)}/10"
    match = re.search(r"\bRated\s+(10|[1-9](?:\.\d+)?)\s+out\s+of\s+10\b", value, re.I)
    if match:
        return f"{match.group(1)}/10"
    return None


def _resolve_rating_from_link(context, rating_link: str) -> Optional[str]:
    if not rating_link:
        return None
    rating_page = context.new_page()
    try:
        rating_page.goto(rating_link, timeout=30000, wait_until="domcontentloaded")
        time.sleep(3)
        text = rating_page.evaluate(
            """() => {
                const txt = (el) => (el && el.innerText ? el.innerText.replace(/\s+/g, ' ').trim() : '');
                const labeled = Array.from(document.querySelectorAll('body *')).map((el) => txt(el)).filter(Boolean).find((t) => /GreatSchools Rating/i.test(t) && /\d+(?:\.\d+)?\/10/.test(t));
                if (labeled) return labeled;
                const body = document.body && document.body.innerText ? document.body.innerText : '';
                return body;
            }"""
        ) or ""
        return _parse_rating_text(text)
    except Exception:
        return None
    finally:
        rating_page.close()


def _enrich_school_ratings(context, schools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    cache: Dict[str, Optional[str]] = {}
    for school in schools:
        item = dict(school)
        link = _norm_text(item.get("rating_link"))
        resolved = None
        if link:
            if link not in cache:
                cache[link] = _resolve_rating_from_link(context, link)
            resolved = cache[link]
        if resolved:
            item["rating"] = resolved
        enriched.append(item)
    return enriched


def _debug_school_cards(page) -> List[Dict[str, Any]]:
    _clear_selection(page)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)
    result = page.evaluate(
        """() => {
            const txt = (el) => (el && el.innerText ? el.innerText.replace(/\s+/g, ' ').trim() : '');
            const styleOf = (el) => {
                if (!el) return null;
                const s = window.getComputedStyle(el);
                return {
                    text: txt(el),
                    className: el.className || '',
                    tagName: el.tagName || '',
                    color: s.color || '',
                    backgroundColor: s.backgroundColor || '',
                    borderColor: s.borderColor || '',
                    fill: s.fill || '',
                    stroke: s.stroke || '',
                };
            };
            const heading = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')).find((el) => /education|schools/i.test(txt(el)));
            if (heading && heading.scrollIntoView) {
                heading.scrollIntoView({block: 'center'});
            }
            const cards = Array.from(document.querySelectorAll('.schoolsSection .card, .schoolsSection .component-frame, .profilev2SchoolsModule .card, .profilev2SchoolsModule .component-frame, [data-test="schoolCard"], .educationCard, .schoolCard'));
            return cards.slice(0, 6).map((card) => {
                const nodes = Array.from(card.querySelectorAll('*'));
                const interesting = nodes.filter((el) => {
                    const t = txt(el);
                    const cls = (el.className || '') + ' ' + (el.getAttribute('data-testid') || '');
                    return /\/10/.test(t) || /rating|score|badge|chip/i.test(cls);
                }).slice(0, 12).map(styleOf).filter(Boolean);
                return {
                    raw: txt(card),
                    name: txt(card.querySelector('h3, h4, h5, [class*="name"], a')),
                    interesting,
                };
            });
        }"""
    ) or []
    return result


def _extract_school_name(raw: str, fallback: str = "") -> str:
    text = _norm_text(raw)
    patterns = [
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Middle School)",
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Elementary School)",
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Elementary)",
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Intermediate)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _norm_text(match.group(1))
    return _norm_text(fallback).split(" Grades ")[0].strip()


def _summarize_assigned_schools(schools: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    seen = set()
    for school in schools:
        raw = _norm_text(school.get("raw"))
        zone = _norm_text(school.get("zone"))
        if zone != "Attendance Zone" and "attendance zone" not in raw.lower():
            continue
        name = _extract_school_name(raw, _norm_text(school.get("name")))
        bucket = _school_bucket(" ".join([name, _norm_text(school.get("grades")), raw]))
        if bucket not in {"elementary", "middle"}:
            continue
        key = (name.lower(), bucket)
        if not name or key in seen:
            continue
        seen.add(key)
        rating = _norm_text(school.get("rating"))
        part = f"attendance_zone_{bucket}={name}"
        if rating:
            part += f"; rating={rating}"
        parts.append(part)
    return " | ".join(parts)


def _upsert_row(ws, header_map: Dict[str, int], source_url: str, notes_raw: str, assigned_schools: str) -> None:
    source_col = header_map.get("source_mailbox")
    notes_raw_col = header_map.get("notes_raw")
    assigned_col = header_map.get("assigned schools")
    if not source_col or not notes_raw_col or not assigned_col:
        raise RuntimeError("Workbook must contain 'source_mailbox', 'notes_raw', and 'assigned schools' columns")
    target_row = None
    for row_idx in range(2, ws.max_row + 1):
        existing_source = _norm_text(ws.cell(row_idx, source_col).value)
        if existing_source == source_url:
            target_row = row_idx
            break
    if target_row is None:
        target_row = ws.max_row + 1
        ws.cell(target_row, source_col).value = source_url
    ws.cell(target_row, notes_raw_col).value = notes_raw[:30000] if notes_raw else None
    ws.cell(target_row, assigned_col).value = assigned_schools or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Load Apartments.com raw text and attendance-zone school dark-green flags")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--workbook", default=DEFAULT_WORKBOOK)
    parser.add_argument("--sheet", default=DEFAULT_SHEET)
    parser.add_argument("--max", type=int, default=20)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug-school-dom", action="store_true")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Playwright not installed")

    if not Path(args.workbook).exists():
        raise SystemExit(f"Workbook not found: {args.workbook}")

    wb, ws = _open_workbook(args.workbook, args.sheet)
    header_map = _build_header_map(ws)

    with sync_playwright() as p:
        close_browser = False
        if args.cdp_url:
            browser = p.chromium.connect_over_cdp(args.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1600, "height": 1200})
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context(viewport={"width": 1600, "height": 1200})
            page = context.new_page()
            close_browser = True

        if _is_property_url(args.url):
            listings = [args.url]
        else:
            page.goto(args.url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(5)
            listings = _collect_listing_urls(page)

        sliced = listings[args.skip:args.skip + args.max]
        print(f"Found {len(listings)} listings, processing {len(sliced)}", flush=True)
        written = 0
        skipped = 0
        for idx, url in enumerate(sliced, start=1):
            detail_page = context.new_page()
            try:
                print(f"[{idx}/{len(sliced)}] {url}", flush=True)
                detail_page.goto(url, timeout=30000, wait_until="domcontentloaded")
                time.sleep(4)
                notes_raw = _capture_selected_page_text(detail_page)
                if args.debug_school_dom:
                    debug_cards = _debug_school_cards(detail_page)
                    print(f"    school_dom_debug={debug_cards}", flush=True)
                school_cards = _extract_school_flags(detail_page)
                school_cards = _enrich_school_ratings(context, school_cards)
                assigned_schools = _summarize_assigned_schools(school_cards)
                if not notes_raw.strip():
                    print("    skip empty notes_raw", flush=True)
                    skipped += 1
                    continue
                if not args.dry_run:
                    _upsert_row(ws, header_map, url, notes_raw, assigned_schools)
                print(f"    ok notes_raw_len={len(notes_raw)} assigned_schools={assigned_schools or 'None'}", flush=True)
                written += 1
            except Exception as exc:
                print(f"    failed: {exc}", flush=True)
                skipped += 1
            finally:
                detail_page.close()

        if close_browser:
            browser.close()

    if not args.dry_run:
        wb.save(args.workbook)
    print(f"Done. written={written} skipped={skipped} dry_run={args.dry_run}", flush=True)


if __name__ == "__main__":
    main()
