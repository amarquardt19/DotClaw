import argparse
import base64
import csv
import json
import math
import os
import random
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from openpyxl import load_workbook

DEFAULT_URL = "https://www.apartments.com/apartments/san-mateo-county-ca/?bb=vy6sx02v1O9_g34p8B"
DEFAULT_WORKBOOK = str(Path.home() / "Downloads" / "Dealsifta Apartments Resaved.xlsx")
DEFAULT_SHEET = "XL dealsifta (2)"
DEFAULT_COUNTY = "San Mateo County"
DEFAULT_MSA = "San Jose-Sunnyvale-Santa Clara, CA"
UID_START = 900000
MIN_UNITS = 80

SM_COUNTY_CITIES = {
    "atherton", "belmont", "brisbane", "burlingame", "colma", "daly city",
    "east palo alto", "foster city", "half moon bay", "hillsborough",
    "menlo park", "millbrae", "pacifica", "portola valley", "redwood city",
    "san bruno", "san carlos", "san mateo", "south san francisco", "woodside",
    "emerald lake hills", "highlands-baywood park", "north fair oaks",
}

INSTITUTIONAL_MGMT_OVERRIDES = (
    "ryan tower",
    "888 san mateo",
    "woodland park",
    "springline",
    "villages in pacifica",
    "burlingame towers",
)

NON_INSTITUTIONAL_MGMT_OVERRIDES = (
    "the heltsley",
)

_AFFORDABLE_SENIOR_RE = re.compile(
    r"affordable (?:housing |apartment )?communit|"
    r"(?:is|are) an? affordable|"
    r"income[- ]restricted (?:household|unit|apartment|rental)|"
    r"area median income|\bami\b|income limits? apply|"
    r"low[- ]income housing tax credit|lihtc|section 42|"
    r"senior (?:community|living|building|apartments?)(?!\s*managers?)|age[- ]restricted|active adult community|"
    r"\b(?:55|62)\+ (?:community|living|apartments?)|,\s*(?:55|62)\+\b",
    re.I,
)

APARTMENT_SIGNAL_COLUMNS = [
    "signal_near_employer_or_bsu",
    "major_employer_name",
    "signal_strong_school_district",
    "signal_limited_new_supply",
    "signal_assumable_debt",
    "signal_discount_to_replacement_explicit",
    "signal_mark_to_market_below_market_rents",
    "signal_no_institutional_mgmt",
    "signal_forecasted_job_growth",
    "signal_fortune_500_presence",
    "signal_near_metro_transit",
    "signal_near_downtown_retail_hub",
    "signal_close_to_highway_entrance",
    "signal_next_to_park",
    "signal_views",
    "signal_no_in_unit_laundry",
    "signal_negative_building",
    "signal_negative_neighborhood",
    "signal_negative_building_or_neighborhood",
]

APARTMENT_DB_COLUMNS = [
    "uid", "is_residential", "residential_gated", "residential_gating_reason",
    "score", "score_breakdown", "notes_score_reason_structured", "score_missing_inputs",
    "deal_name", "address", "city", "state", "county", "msa", "product",
    "asset_class", "transaction_type", "units", "guidance_price", "vintage",
    "renovation_year", "renovated", "x", "example_rent_one_bed", "rent_psf",
    "rent_units", "in_place_cap_rate", "going_in_cash_yield",
    "discount_to_replacement", "example_one_bed_unit_size", "buildings",
    "building_size", "price_per_sf", "offer_due_date", "broker_name",
    "broker_firm", "broker_email", "broker_phone", "seller", "sponsor",
    "notes_score_reason_text", "notes", "email_subject", "email_from",
    "email_received_utc", "source_mailbox", "neighborhood_notes",
    "assigned_schools", "transportation_notes", "inserted_at",
    "score_component_vector", "geo_score", "units_score", "price_score",
    "rent_score", "vintage_score", "reno_boost_score", "returns_score",
    "strategy_score", "metro_score", "notes_boost_score", "total_score",
    "notes_raw", "neighborhood", "parking_score", *APARTMENT_SIGNAL_COLUMNS, "qualitative_signal_evidence",
    "negative_signal_evidence", "qualitative_signal_source", "qualitative_boost_total",
    "property_site_url", "property_site_text", "signal_evidence",
]

APARTMENT_EXCEL_EXTRA_COLUMNS = [
    "geo_score", "units_score", "price_score", "rent_score", "vintage_score",
    "reno_boost_score", "returns_score", "strategy_score", "metro_score",
    "notes_boost_score", "total_score", *APARTMENT_SIGNAL_COLUMNS,
    "qualitative_signal_evidence", "negative_signal_evidence", "qualitative_signal_source", "qualitative_boost_total",
]

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "" or val == "null":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        cleaned = str(val).replace("$", "").replace(",", "").replace("%", "").split("/")[0].strip()
        return float(cleaned) if cleaned else None
    except Exception:
        return None


def _safe_int(val: Any) -> Optional[int]:
    if val is None or val == "" or val == "null":
        return None
    if isinstance(val, int):
        return val
    try:
        return int(str(val).replace(",", "").strip())
    except Exception:
        return None


def _norm_text(val: Any) -> str:
    if val is None:
        return ""
    return re.sub(r"\s+", " ", str(val)).strip()


def _resolve_msa(city: Optional[str], state: Optional[str], county: Optional[str]) -> Optional[str]:
    city_key = _norm_text(city).lower()
    county_key = _norm_text(county).lower()
    san_mateo_cities = {
        "atherton", "belmont", "brisbane", "burlingame", "colma", "daly city",
        "east palo alto", "foster city", "half moon bay", "hillsborough",
        "menlo park", "millbrae", "pacifica", "portola valley", "redwood city",
        "san bruno", "san carlos", "san mateo", "south san francisco", "woodside",
    }
    if county_key == "san mateo county" or city_key in san_mateo_cities:
        return DEFAULT_MSA
    return None


def _infer_transaction_type(fields: Dict[str, Any]) -> Optional[str]:
    raw_value = _norm_text(fields.get("transaction_type")).lower()
    known = {
        "lease-up": "Lease-up",
        "lease up": "Lease-up",
        "development": "Development",
        "new construction": "Lease-up",
        "core": "Core",
        "stabilized": "Core",
        "core+": "Core+",
        "core-plus": "Core+",
        "value-add": "Value-add",
        "value add": "Value-add",
        "mark-to-market": "Mark-to-Market",
        "reposition": "Value-add",
    }
    text = " ".join(_norm_text(fields.get(key)).lower() for key in [
        "notes", "notes_raw", "email_subject", "neighborhood notes",
        "transportation notes", "_full_text",
    ])
    vintage = _safe_int(fields.get("vintage"))

    if re.search(r"mark[- ]to[- ]market|below[- ]market rents|under[- ]rented|loss to lease", text):
        return "Mark-to-Market"
    if vintage is not None:
        if vintage >= 2026:
            return "Lease-up"
        if vintage >= 2016:
            return "Core"
        if vintage >= 2006:
            return "Core+"
        return "Value-add"
    if raw_value in known:
        return known[raw_value]
    if re.search(r"lease[- ]up|delivered vacant|new construction|brand new", text):
        return "Lease-up"
    if re.search(r"value[- ]add|reposition|renovation opportunity|deferred maintenance|unrenovated", text):
        return "Value-add"
    return None


_DOWNTOWN_TERM_RE = re.compile(r"\b(?:downtown|town center|main street|retail hub|shopping district|commercial corridor)\b", re.I)
_DOWNTOWN_CUE_RE = re.compile(
    r"\b(?:walk(?:ing|able)?|steps?|stroll|blocks?|minutes?|heart of|in (?:the )?heart|adjacent|near|nearby|"
    r"doorstep|just (?:outside|steps?|blocks?|minutes?)|edge of|nestled|bordering|overlooking|pedestrian|"
    r"center of|in (?:the )?downtown|across from)\b",
    re.I,
)
_DOWNTOWN_NEGATION_RE = re.compile(
    r"\b(?:outside (?:the )?neighborhood|venture|drive to|driving to|commute to|far from|away from|"
    r"short drive|quick drive|hop in the car|leave the neighborhood)\b",
    re.I,
)


def _downtown_adjacent(text: str) -> Optional[str]:
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        if not _DOWNTOWN_TERM_RE.search(sentence):
            continue
        if _DOWNTOWN_NEGATION_RE.search(sentence):
            continue
        if _DOWNTOWN_CUE_RE.search(sentence):
            return _norm_text(sentence)[:220]
    return None


def _looks_address_branded(title: str, address: str, vintage: Optional[int]) -> bool:
    if not title or not address or vintage is None or vintage < 2010:
        return False
    title_n = _norm_text(title).lower()
    address_n = _norm_text(address).lower()
    num_match = re.match(r"(\d{2,5})\b", address_n)
    if not num_match:
        return False
    street_match = re.match(r"\d{2,5}\s+((?:n|s|e|w|north|south|east|west)\s+)?([a-z0-9'.-]+(?:\s+[a-z0-9'.-]+){0,3})\s+(?:st|street|ave|avenue|blvd|boulevard|dr|drive|rd|road|way)\b", address_n)
    if not street_match:
        return False
    street_core = street_match.group(2).strip()
    num = num_match.group(1)
    return num in title_n and street_core in title_n


def _review_is_recent(text: str, anchor: str, months: int = 18) -> bool:
    if not text or not anchor:
        return False
    idx = text.lower().find(anchor.lower())
    if idx < 0:
        return False
    window = text[max(0, idx - 1200): min(len(text), idx + len(anchor) + 220)]
    matches = list(re.finditer(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|sept(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2},\s*\d{4}\b", window, re.I))
    if not matches:
        matches = list(re.finditer(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|sept(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{4}\b", window, re.I))
    if not matches:
        return False
    now = datetime.utcnow()
    cutoff_months = now.year * 12 + now.month - months
    for m in matches:
        raw = m.group(0)
        parsed = None
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if not parsed:
            continue
        parsed_months = parsed.year * 12 + parsed.month
        if parsed_months >= cutoff_months:
            return True
    return False


def _review_helpful_count(text: str, anchor: str) -> int:
    if not text or not anchor:
        return 0
    idx = text.lower().find(anchor.lower())
    if idx < 0:
        window = text[:1600]
    else:
        window = text[max(0, idx - 800): min(len(text), idx + len(anchor) + 800)]
    patterns = [
        r"(\d{1,3})\s+people\s+found\s+this\s+helpful",
        r"(\d{1,3})\s+found\s+this\s+helpful",
        r"(\d{1,3})\s+helpful\s+votes?",
        r"(\d{1,3})\s+likes?",
        r"vote\s+review\s+helpful\s+(\d{1,3})",
        r"helpful\s+(\d{1,3})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, window, re.I)
        if m:
            return int(m.group(1))
    return 0


def _downtown_from_reviews(text: str) -> Optional[str]:
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        if not _DOWNTOWN_TERM_RE.search(sentence):
            continue
        if not re.search(r"review|reviews|residents?|commenters?|people say|locals?", sentence, re.I):
            continue
        if not _review_is_recent(text, sentence):
            continue
        if re.search(r"near|close to|walk to|steps from|minutes from|adjacent|heart of", sentence, re.I):
            return _norm_text(sentence)[:220]
    return None


_REV_POS_VERB_RE = re.compile(
    r"\b(?:love|loved|amazing|beautiful|stunning|gorgeous|great|incredible|nice|"
    r"i can|we can|you can|we have|we love|has got|there'?s|easy|close|"
    r"walk to|walking distance|steps? (?:from|to)|right by|right next|"
    r"minutes? (?:from|to)|quick|convenient|access to)\b",
    re.I,
)


def _reviews_positive_signal(text: str, term_re: "re.Pattern[str]") -> Optional[str]:
    """Fire when a review-scoped sentence references `term_re` with a positive/personal cue.
    Reviews are long-lived evidence for structural facts (views, parks, downtown, transit, freeway),
    so no recency gate — unlike complaint-based negatives."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        if not term_re.search(sentence):
            continue
        if not _REV_POS_VERB_RE.search(sentence):
            continue
        if re.search(r"complain|terrible|awful|bad|rude|dirty|mold|noise|unsafe", sentence, re.I):
            continue
        return _norm_text(sentence)[:220]
    return None


_RE_REV_VIEW_RE = re.compile(r"\bviews?|bay view|city view|skyline|water view|mountain view|rooftop\b", re.I)
_RE_REV_PARK_RE = re.compile(r"\bpark\b|trail|lake|waterfront|boardwalk|dog park|green space", re.I)
_RE_REV_HWY_RE = re.compile(r"\bfreeway\b|\bhighway\b|(?:easy|quick|close) (?:\w+\s?){0,3}access|commut(?:e|ing)\b", re.I)
_RE_REV_TRANSIT_RE = re.compile(r"\bmuni\b|\bbart\b|\bcaltrain\b|\bbus\b|\btrain station\b|\blight rail\b|\bfcommute\b|transit\b", re.I)


def _strong_negative_location_signal(text: str) -> Tuple[bool, bool, Optional[str], Optional[str]]:
    review_cue_re = re.compile(r"review|reviews|residents?|commenters?|people say|locals?|property manager responded", re.I)
    mgmt_only_re = re.compile(
        r"management|manager|leasing office|customer service|staff|maintenance|tenants? do not matter|"
        r"safety is not a priority|harassed|teenagers? roaming|rude|unresponsive|bad service",
        re.I,
    )
    building_issue_re = re.compile(
        r"insulation|drafty|drafts|cold all the time|freezing inside|windows? .* (?:leak|broken|won't seal)|"
        r"balcony .* insulation|"
        r"\bmold\b(?!ing)|mildew|water leak|leaks? when it rains|roof leak|water damage|"
        r"rodents?|roach(?:es)? infestation|bed ?bugs?|pest (?:problem|issues?|infestation)|"
        r"foundation (?:cracks?|damage|issues?|leaks?|problems?|repairs?)|structural (?:damage|issues?|problems?|cracks?)|settling cracks|"
        r"cracks? in (?:the )?(?:walls?|ceiling|foundation)|no heat|no hot water|heater broken|hot water heater|hvac broken|ac broken|"
        r"hazardous (?:environment|conditions?)|windows? (?:fell|falling|fell off|fell out)|(?:fell|falling) off (?:the )?building|"
        r"burned (?:people|residents?)|too much chlorine|"
        r"plumbing issues?|sewage (?:leak|backup|overflow|smell|odor|flood)|pipes? burst|elevators?[^.]{0,60}(?:broken|out of service|not working|down for)",
        re.I,
    )
    strong_building_teaser_re = re.compile(
        r"insulation .* non[- ]?(?:existent|existant)|cold all the time|cover my windows? .* insulation|"
        r"balcony .* insulation|management .* insulation",
        re.I,
    )
    severe_noise_building_re = re.compile(
        r"(?:train|caltrain|railroad|freeway|highway|airport|airplane|flight path)\s+noise|"
        r"noise from (?:the )?(?:train|caltrain|freeway|highway|airport|planes?)",
        re.I,
    )
    severe_noise_ambient_re = re.compile(
        r"very noisy|extremely noisy|extreme noise|constant noise|noise all night|can't sleep|"
        r"not a moment of peace",
        re.I,
    )
    severe_safety_re = re.compile(
        r"robber(?:y|ies)|armed robbery|shooting|gunshots|stabbing|assault|violent crime|"
        r"car break[- ]ins?|break[- ]ins?|unsafe area|dangerous area|high crime area|"
        r"crime ridden|criminal activity|homeless encampment|people screaming|"
        r"screaming (?:people|matches?|in the street)|safety concerns?|false sense of security|"
        r"security[^.]{0,40}(?:laughable|a joke|inadequate|ignored|nonexistent)|never seen security|no security personnel",
        re.I,
    )
    flood_re = re.compile(r"flood(?:ed|ing|s)?\b", re.I)

    def flood_hit(sentence: str) -> bool:
        for m in flood_re.finditer(sentence):
            ctx = sentence[max(0, m.start() - 40):m.end() + 60]
            if not re.search(r"\blight\b", ctx, re.I):
                return True
        return False

    building_evidence = None
    neighborhood_evidence = None
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        s = sentence.strip()
        if not s:
            continue
        if mgmt_only_re.search(s) and not (building_issue_re.search(s) or severe_noise_building_re.search(s) or severe_noise_ambient_re.search(s) or severe_safety_re.search(s)):
            continue
        is_review = review_cue_re.search(text) or re.search(r"review|resident", s, re.I)
        if not is_review:
            continue
        helpful_count = _review_helpful_count(text, s)
        now = datetime.utcnow()
        months_since_jan_2024 = now.year * 12 + now.month - (2024 * 12 + 1)
        recent_building = _review_is_recent(text, s) or helpful_count >= 10
        recent_neighborhood = _review_is_recent(text, s, months=months_since_jan_2024) or helpful_count >= 10
        building_pattern_match = (
            building_issue_re.search(s)
            or strong_building_teaser_re.search(s)
            or (severe_noise_building_re.search(s) if recent_building else None)
        )
        if not building_evidence and (building_pattern_match or flood_hit(s)):
            start = building_pattern_match.start() if building_pattern_match else 0
            building_evidence = _norm_text(s[start:])[:400]
            if helpful_count > 0:
                building_evidence = f"{building_evidence} [helpful={helpful_count}]"
        safety_match = (
            (severe_safety_re.search(s) or severe_noise_ambient_re.search(s))
            if recent_neighborhood else None
        )
        if safety_match and not neighborhood_evidence:
            neighborhood_evidence = _norm_text(s[safety_match.start():])[:400]
            if helpful_count > 0:
                neighborhood_evidence = f"{neighborhood_evidence} [helpful={helpful_count}]"
    return bool(building_evidence or neighborhood_evidence), bool(building_evidence), building_evidence, neighborhood_evidence


def _downtown_adjacent_llm(text: str) -> Optional[bool]:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text or "") if _DOWNTOWN_TERM_RE.search(s)]
    if not sentences:
        return False
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    excerpt = "\n".join(f"- {s}" for s in sentences[:12])
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=(
                "You classify apartment listing copy. Given sentences mentioning downtown/town center/main street "
                "from a property's marketing, decide whether the property is being promoted as downtown-ADJACENT "
                "(walkable, steps, minutes away, heart of, located in) versus downtown being merely a city fact or "
                "a drive-away destination mentioned for flavor. Answer only 'yes' or 'no'."
            ),
            messages=[{"role": "user", "content": excerpt}],
        )
        answer = "".join(getattr(p, "text", "") for p in (getattr(msg, "content", None) or [])).strip().lower()
        if answer.startswith("yes"):
            return True
        if answer.startswith("no"):
            return False
    except Exception:
        return None
    return None


HIGHWAY_CORRIDORS = {
    "i-280": [
        (37.690, -122.470), (37.665, -122.455), (37.640, -122.431),
        (37.615, -122.428), (37.605, -122.427), (37.590, -122.414),
        (37.570, -122.390), (37.555, -122.370), (37.545, -122.352),
        (37.535, -122.335), (37.520, -122.315), (37.500, -122.295),
        (37.480, -122.280), (37.465, -122.275),
        # SF extension: Daly City -> Bernal -> Potrero -> 6th/King terminus
        (37.708, -122.460), (37.721, -122.447), (37.733, -122.421),
        (37.745, -122.409), (37.756, -122.399), (37.766, -122.394),
        (37.776, -122.393),
    ],
    "us-101": [
        (37.683, -122.404), (37.655, -122.405), (37.630, -122.404),
        (37.606, -122.393), (37.590, -122.370), (37.574, -122.352),
        (37.560, -122.332), (37.543, -122.312), (37.528, -122.293),
        (37.512, -122.270), (37.495, -122.245), (37.480, -122.222),
        # SF extension: Bayview -> Potrero -> SoMa -> Van Ness/Mission terminus
        (37.700, -122.405), (37.715, -122.402), (37.730, -122.399),
        (37.745, -122.397), (37.757, -122.396), (37.766, -122.401),
        (37.772, -122.409),
    ],
    "i-80-sf": [
        # Bay Bridge -> SoMa -> merges into US-101
        (37.798, -122.372), (37.790, -122.386), (37.783, -122.392),
        (37.775, -122.397), (37.769, -122.401),
    ],
}


def _point_to_segment_mi(plat: float, plng: float, a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat0 = math.radians((a[0] + b[0] + plat) / 3)
    ax, ay = math.radians(a[1]) * math.cos(lat0), math.radians(a[0])
    bx, by = math.radians(b[1]) * math.cos(lat0), math.radians(b[0])
    px, py = math.radians(plng) * math.cos(lat0), math.radians(plat)
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    ab_len2 = abx * abx + aby * aby
    t = 0.0 if ab_len2 == 0 else max(0.0, min(1.0, (apx * abx + apy * aby) / ab_len2))
    dx, dy = px - (ax + t * abx), py - (ay + t * aby)
    return 3958.8 * math.sqrt(dx * dx + dy * dy)


def _near_highway_corridor(lat: float, lng: float, threshold_mi: float = 1.0) -> bool:
    for points in HIGHWAY_CORRIDORS.values():
        for i in range(len(points) - 1):
            if _point_to_segment_mi(lat, lng, points[i], points[i + 1]) <= threshold_mi:
                return True
    return False


_VIEWS_RE = re.compile(
    r"\b(?:bay|water|ocean|lagoon|marina|sf bay|san francisco bay)\s+views?\b|"
    r"\bviews?\s+of\s+(?:the\s+)?(?:bay|water|ocean|lagoon|marina)\b|"
    r"\bwaterfront\b|\bfootsteps from the\s+(?:pacific\s+)?ocean\b|\bocean breeze\b|"
    r"\bby the ocean\b|\bsteps (?:to|from) the (?:beach|ocean)\b|\bbeachfront\b|\boceanside\b|"
    r"\bskyline views?\b|\bpanoramic views?\b|\bsweeping views?\b|"
    r"\bviews?\s+of\s+(?:the\s+)?(?:city|downtown|skyline)\b|\bcity skyline\b|"
    r"\bviews\b(?=[^.]{0,100}floor[- ]to[- ]ceiling windows)|"
    r"floor[- ]to[- ]ceiling windows[^.]{0,100}\bviews\b",
    re.I,
)
_VIEWS_FOOTER_RE = re.compile(r"^(?:waterfront|beachfront|oceanside)$", re.I)
_VIEWS_FOOTER_TAIL_RE = re.compile(r"^\s+(?:apartments?|rentals|condos|homes|living|housing)\b", re.I)
_VIEWS_ROOFTOP_RE = re.compile(r"rooftop|roof\s*deck|sky\s*deck|sun\s*deck|\bdeck\b|terrace|skyline", re.I)


def _views_signal_hit(text: str) -> Optional[str]:
    for m in _VIEWS_RE.finditer(text or ""):
        if _VIEWS_FOOTER_RE.match(m.group(0)) and _VIEWS_FOOTER_TAIL_RE.match(text[m.end():m.end() + 40]):
            continue
        if _VIEWS_ROOFTOP_RE.search(text[max(0, m.start() - 80):m.start()]):
            continue
        s = max(0, m.start() - 60)
        return _norm_text(text[s:m.end() + 60])
    return None


EMPLOYER_BSU_CSV = Path.home() / "Downloads" / "boise_properties_employer_and_bsu_distances.csv"
_EMPLOYER_BSU_LOOKUP: Optional[Dict[str, tuple]] = None


def _employer_bsu_lookup() -> Dict[str, tuple]:
    """address -> (score, employer_label). score: 1.0 emp+BSU, 0.75 emp only, 0.5 BSU only."""
    global _EMPLOYER_BSU_LOOKUP
    if _EMPLOYER_BSU_LOOKUP is not None:
        return _EMPLOYER_BSU_LOOKUP
    table: Dict[str, tuple] = {}
    if EMPLOYER_BSU_CSV.exists():
        with open(EMPLOYER_BSU_CSV, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not r.get("property_name"):
                    continue
                try:
                    emp_mi = float(r["driving_distance_to_nearest_employer_mi"]) if r["driving_distance_to_nearest_employer_mi"] else None
                    bsu_mi = float(r["driving_distance_to_boise_state_mi"]) if r["driving_distance_to_boise_state_mi"] else None
                except (ValueError, KeyError):
                    continue
                near_emp = emp_mi is not None and emp_mi < 2.5
                near_bsu = bsu_mi is not None and bsu_mi <= 1.0
                if near_emp and near_bsu:
                    s, label = 1.0, f"{r['nearest_employer_excluding_boise_state']} + Boise State"
                elif near_emp:
                    s, label = 0.75, r["nearest_employer_excluding_boise_state"]
                elif near_bsu:
                    s, label = 0.5, "Boise State"
                else:
                    s, label = 0.0, None
                table[_norm_text(r["address"]).lower()] = (s, label)
    _EMPLOYER_BSU_LOOKUP = table
    return table


def _score_qualitative_signals(fields: Dict[str, Any], transaction_type: Optional[str]) -> Dict[str, Any]:
    positive_signals = {
        _norm_text(signal).lower()
        for signal in (fields.get("positive_signals") or [])
        if isinstance(signal, str)
    }
    text = " ".join(_norm_text(fields.get(key)).lower() for key in [
        "notes", "notes_raw", "email_subject", "neighborhood notes",
        "assigned schools", "transportation notes", "sponsor", "_full_text",
    ])
    title = _norm_text(fields.get("deal_name"))
    address = _norm_text(fields.get("address"))
    vintage = _safe_int(fields.get("vintage"))
    schools_text = _norm_text(fields.get("assigned schools")).lower()
    transport_text = _norm_text(fields.get("transportation notes")).lower()
    neighborhood_text = _norm_text(fields.get("neighborhood notes")).lower()

    signals: Dict[str, float] = {}
    signal_evidence: Dict[str, str] = {}

    if fields.get("signal_no_institutional_mgmt"):
        nimg_ev = _norm_text(fields.get("_no_institutional_mgmt_evidence"))
        if nimg_ev:
            signal_evidence["no institutional mgmt"] = nimg_ev[:250]

    def add(name: str, points: float = 1.0, evidence: Optional[str] = None) -> None:
        signals[name] = max(points, signals.get(name, 0.0))
        if evidence and name not in signal_evidence:
            signal_evidence[name] = _norm_text(evidence)[:250]

    elementary_dark_green = "attendance_zone_elementary_dark_green=yes" in schools_text
    middle_dark_green = "attendance_zone_middle_dark_green=yes" in schools_text
    if elementary_dark_green and middle_dark_green:
        add("strong school district")

    if re.search(
        r"limited (new )?supply|supply constrained|supply-constrained|barriers? to entry|minimal future supply|no new supply|high construction costs|limited land",
        text,
    ) or "limited supply" in positive_signals or "limited new supply" in positive_signals:
        add("limited new supply")

    if re.search(r"assumable debt|assumable loan|loan assumption", text):
        add("assumable debt")

    dtr = _safe_float(fields.get("discount_to_replacement"))
    if dtr is not None and dtr >= 15.0 or re.search(r"discount to replacement|below replacement cost", text):
        add("discount to replacement")

    if (
        transaction_type == "Mark-to-Market"
        or re.search(r"mark[- ]to[- ]market|below[- ]market rents|under[- ]rented|loss to lease|rent upside", text)
        or "rent upside" in positive_signals
    ):
        add("mark-to-market / below-market rents")

    if re.search(r"job growth|jobs growth|employment growth|new jobs|jobs (are )?forecast|forecasted jobs|jobs coming|major employer expansion", text):
        add("forecasted job growth")

    if re.search(r"fortune\s*500|fortune500", text):
        add("Fortune 500 presence")

    emp_lookup_score, emp_lookup_label = _employer_bsu_lookup().get(address.lower(), (0.0, None))
    if emp_lookup_score > 0:
        add("near major employer / Boise State", emp_lookup_score, f"proximity table: {emp_lookup_label}")

    if "transit access" in positive_signals:
        transit_ev = _norm_text(fields.get("transportation notes"))[:200] or "transit access (notes)"
        add("near metro train line/station", 1.0, f"transportation notes: {transit_ev}")

    downtown_ev = _downtown_adjacent(neighborhood_text) or _downtown_from_reviews(text)
    if (
        "downtown" in positive_signals
        or downtown_ev
        or (_norm_text(fields.get("city")).lower() != "san francisco" and _looks_address_branded(title, address, vintage))
    ):
        if not downtown_ev:
            downtown_ev = "downtown noted in listing notes" if "downtown" in positive_signals else f"address-branded name: {title} / {address}"
        add("near downtown corridor / retail hub", 1.0, downtown_ev)

    highway_ev = None
    highway_re = re.compile(r"highway entrance|freeway entrance|on[- ]ramp|off[- ]ramp|interchange|highway access|freeway access|easy access (?:to|near)[^.\n]{0,40}(?:freeway|highway)|(?:freeways?|highways?)\s+(?:92|101|280|380)\b|us-101|i-280|i-380|sr-92|\bthe (?:101|280|380|92)\b", re.I)
    hm = highway_re.search(text + " " + transport_text + " " + neighborhood_text)
    if "highway access" in positive_signals or hm:
        if hm:
            s = max(0, hm.start() - 60)
            highway_ev = _norm_text((text + " " + transport_text + " " + neighborhood_text)[s:hm.end() + 60])
        else:
            highway_ev = "highway access noted in transportation notes"
        add("close to highway entrance", 0.5, highway_ev)

    if "close to highway entrance" not in signals:
        lat = _safe_float(fields.get("_lat"))
        lng = _safe_float(fields.get("_lng"))
        if lat is not None and lng is not None and _near_highway_corridor(lat, lng):
            add("close to highway entrance", 0.5, f"coords within 1.0mi of highway corridor ({lat},{lng})")

    views_ev = _views_signal_hit(text + " " + neighborhood_text)
    if views_ev:
        add("bay/water views", 0.5, views_ev)

    park_re = re.compile(r"(?:directly )?across (?:the street )?from [a-z .&'-]{2,40}park\b|adjacent to [a-z .&'-]{2,40}park\b|backs? up to [a-z .&'-]{2,40}park\b|next (?:door )?to [a-z .&'-]{2,40}park\b|overlook(?:s|ing)? [a-z .&'-]{2,40}park\b|faces? [a-z .&'-]{2,40}park\b", re.I)
    pm = park_re.search(text + " " + neighborhood_text)
    if pm:
        s = max(0, pm.start() - 60)
        add("next to park", 0.5, _norm_text((text + " " + neighborhood_text)[s:pm.end() + 60]))

    if "close to highway entrance" in signals and "near metro train line/station" in signals:
        signals["close to highway entrance"] = 0.75
        signals["near metro train line/station"] = 0.75
    elif "close to highway entrance" in signals:
        signals["close to highway entrance"] = 1.0

    if "near metro train line/station" not in signals:
        rv = _reviews_positive_signal(text + " " + neighborhood_text + " " + transport_text, _RE_REV_TRANSIT_RE)
        if rv:
            add("near metro train line/station", 0.75, f"review: {rv}")

    if "close to highway entrance" not in signals:
        rv = _reviews_positive_signal(text + " " + neighborhood_text + " " + transport_text, _RE_REV_HWY_RE)
        if rv:
            add("close to highway entrance", 0.75, f"review: {rv}")

    if "next to park" not in signals:
        rv = _reviews_positive_signal(text + " " + neighborhood_text, _RE_REV_PARK_RE)
        if rv:
            add("next to park", 0.5, f"review: {rv}")

    if "bay/water views" not in signals:
        rv = _reviews_positive_signal(text + " " + neighborhood_text, _RE_REV_VIEW_RE)
        if rv:
            add("bay/water views", 0.5, f"review: {rv}")

    no_in_unit_laundry = bool(re.search(
        r"does not (?:provide|have|offer) in[- \u2010\u2011]unit laundry|no in[- \u2010\u2011]unit laundry|"
        r"without in[- \u2010\u2011]unit laundry|doesn'?t (?:have|offer|provide) in[- \u2010\u2011]unit laundry|"
        r"no washer[/ ]?(?:or|and)? ?dryer in[- \u2010\u2011]unit|not equipped with (?:a )?washer|"
        r"shared laundry facilit|laundry facilities are available for shared|coin[- \u2010\u2011]operated laundry",
        text,
    ))

    negative_match = re.search(
        r"collections? issues?|insurance shock|hoa litigation|unmitigated regulatory risk|rent control risk",
        text,
    )
    strong_location_negative, strong_building_negative, building_negative_evidence, neighborhood_negative_evidence = _strong_negative_location_signal(text)
    strong_neighborhood_negative = bool(neighborhood_negative_evidence)
    has_negative = bool(negative_match) or strong_location_negative
    negative_signal_evidence = None
    if negative_match:
        s = max(0, negative_match.start() - 80)
        negative_signal_evidence = f"explicit negative risk: {_norm_text(text[s:negative_match.end() + 80])}"
        print(f"    DEBUG negative-signal match: ...{_norm_text(text[s:negative_match.end() + 80])}...", flush=True)
    elif strong_location_negative:
        evidence = building_negative_evidence or neighborhood_negative_evidence or 'strong review/location risk detected'
        category = 'building' if building_negative_evidence else 'neighborhood'
        negative_signal_evidence = f"negative {category}: {evidence}"
        print(f"    DEBUG negative-signal match ({category}): {evidence}", flush=True)
    positive_boost = round(min(3.0, sum(signals.values())), 2)
    negative_penalty = 0.0
    negative_reasons = []
    if negative_match:
        negative_penalty -= 0.75
        negative_reasons.append("explicit negative risk (-0.75)")
    if strong_building_negative and strong_neighborhood_negative:
        negative_penalty -= 0.75
        negative_penalty -= 0.75
        negative_reasons.append("negative building (-0.75)")
        negative_reasons.append("negative neighborhood (-0.75)")
    elif strong_building_negative:
        negative_penalty -= 1.0
        negative_reasons.append("negative building (-1.0)")
    elif strong_neighborhood_negative:
        negative_penalty -= 1.0
        negative_reasons.append("negative neighborhood (-1.0)")
    boost = round(max(-2.0, min(3.0, positive_boost + negative_penalty)), 2)
    reason_parts = []
    if signals:
        reason_parts.append(" | ".join(f"{label} (+{pts:g})" for label, pts in signals.items()))
    if negative_reasons:
        reason_parts.append(" | ".join(negative_reasons))
    reason = " | ".join(reason_parts) if reason_parts else None

    return {
        "signal_strong_school_district": "strong school district" in signals,
        "signal_limited_new_supply": "limited new supply" in signals,
        "signal_assumable_debt": "assumable debt" in signals,
        "signal_discount_to_replacement_explicit": "discount to replacement" in signals,
        "signal_mark_to_market_below_market_rents": "mark-to-market / below-market rents" in signals,
        "signal_no_institutional_mgmt": bool(fields.get("signal_no_institutional_mgmt")),
        "signal_forecasted_job_growth": "forecasted job growth" in signals,
        "signal_fortune_500_presence": "Fortune 500 presence" in signals,
        "signal_near_metro_transit": signals.get("near metro train line/station", 0.0),
        "signal_near_downtown_retail_hub": "near downtown corridor / retail hub" in signals,
        "signal_close_to_highway_entrance": signals.get("close to highway entrance", 0.0),
        "signal_next_to_park": signals.get("next to park", 0.0),
        "signal_views": 0.5 if "bay/water views" in signals else 0.0,
        "signal_no_in_unit_laundry": 1.0 if no_in_unit_laundry else 0.0,
        "signal_negative_building": (0.75 if strong_neighborhood_negative else 1.0) if strong_building_negative else 0.0,
        "signal_negative_neighborhood": (0.75 if strong_building_negative else 1.0) if strong_neighborhood_negative else 0.0,
        "signal_negative_building_or_neighborhood": 1.0 if strong_location_negative else 0.0,
        "qualitative_signal_evidence": " | ".join(signals.keys()) or None,
        "signal_evidence": ", ".join(
            f"{name}: {signal_evidence[name]}" for name in [
                "near metro train line/station", "near downtown corridor / retail hub",
                "close to highway entrance", "next to park", "no institutional mgmt", "bay/water views",
            ] if name in signal_evidence
        ) or None,
        "negative_signal_evidence": negative_signal_evidence,
        "qualitative_signal_source": "rule_based",
        "qualitative_boost_total": boost,
        "residential_notes_boost": boost,
        "notes_score_reason": reason,
        "signal_near_employer_or_bsu": emp_lookup_score,
        "major_employer_name": emp_lookup_label,
    }


def _compute_apartment_score(fields: Dict[str, Any]) -> Dict[str, Any]:
    product = _norm_text(fields.get("product")).upper()
    asset_class = _norm_text(fields.get("asset_class")).lower()
    is_residential = product in {"MF", "TH", "BFR"} or any(
        token in asset_class for token in ["multifamily", "residential", "bfr", "build for rent", "townhome"]
    )
    is_industrial = "industrial" in asset_class

    units = _safe_int(fields.get("units"))
    price = _safe_float(fields.get("guidance_price"))
    rent = _safe_float(fields.get("avg_rent_monthly")) or _safe_float(fields.get("rent"))
    vintage = _safe_int(fields.get("vintage"))
    transaction_type = _infer_transaction_type(fields)
    msa = _norm_text(fields.get("msa"))
    state = _norm_text(fields.get("state")).upper()

    gated = False
    gating_reason = None

    if not is_residential:
        geo_score = units_score = price_score = rent_score = vintage_score = 0.0
        reno_boost_score = returns_score = strategy_score = metro_score = 0.0
        parking_score = 0.0
        laundry_penalty = 0.0
        qualitative = _score_qualitative_signals(fields, transaction_type)
        qualitative["residential_notes_boost"] = 0.0
        qualitative["qualitative_boost_total"] = 0.0
        residential_score = 0.0
    else:
        if state in {"CA", "WA"}:
            geo_score = 2.0
        elif state in {"ID"}:
            geo_score = 1.5
        elif state in {"CO", "NV", "OR", "UT", "AZ", "TX"}:
            geo_score = 1.0
        else:
            geo_score = 0.0

        county_l = _norm_text(fields.get("county")).lower()
        city_l = _norm_text(fields.get("city")).lower()
        if units is None:
            units_score = 0.0
        elif county_l.startswith("san mateo"):
            units_score = 2.0 if units <= 150 else (1.0 if units <= 250 else 0.0)
        elif city_l == "san francisco" or county_l.startswith("san francisco"):
            units_score = 2.0 if units <= 125 else (1.0 if units <= 225 else 0.0)
        elif city_l in {"boise", "meridian", "eagle", "garden city", "kuna", "nampa"} or county_l.startswith("ada"):
            units_score = 2.0 if units <= 200 else (1.0 if units < 300 else 0.0)
        elif 100 <= units <= 300:
            units_score = 2.0
        elif 60 <= units <= 99 or 301 <= units <= 600:
            units_score = 1.0
        else:
            units_score = 0.0

        if price is None or price < 25_000_000 or price > 120_000_000:
            price_score = 0.0
        elif price <= 60_000_000:
            price_score = round(1.0 + ((price - 30_000_000) / 30_000_000), 2)
        else:
            price_score = round(2.0 - ((price - 60_000_000) / 60_000_000), 2)

        county_l = _norm_text(fields.get("county")).lower()
        if county_l == "ada":
            if rent is not None and 1600 <= rent <= 2800:
                rent_score = 1.0
            elif rent is not None and 1400 <= rent < 1600:
                rent_score = 0.75
            elif rent is not None and rent < 1400:
                rent_score = 0.5
            else:
                rent_score = 0.0
        elif rent is not None and 1300 <= rent <= 2800:
            rent_score = 1.5
        elif rent is not None and 2801 <= rent <= 3200:
            rent_score = 0.5
        elif rent is not None and rent > 3800:
            rent_score = -1.0
        else:
            rent_score = 0.0

        if vintage is not None and vintage >= 2000:
            vintage_score = 1.0
        elif vintage is not None and 1980 <= vintage <= 1999:
            vintage_score = 0.5
        else:
            vintage_score = 0.0
        reno_boost_score = 0.0
        renovation_year = _safe_int(fields.get("renovation_year"))
        if fields.get("renovated") is True and renovation_year is not None and renovation_year >= 2022:
            reno_boost_score = 0.25
            vintage_score = min(1.0, vintage_score + reno_boost_score)
        county = _norm_text(fields.get("county")).lower()
        if vintage is not None and vintage < 1970 and "san mateo" in county:
            vintage_score -= 1.0

        returns_score = 0.0
        going_in_cash_yield = _safe_float(fields.get("going_in_cash_yield"))
        if going_in_cash_yield is not None:
            if going_in_cash_yield >= 5.5:
                returns_score = 1.5
            elif going_in_cash_yield >= 5.0:
                returns_score = 1.0
            elif going_in_cash_yield >= 4.5:
                returns_score = 0.5
        dtr = _safe_float(fields.get("discount_to_replacement"))
        if dtr is not None and dtr >= 15.0:
            returns_score += 0.5
        returns_score = min(1.5, returns_score)

        transaction_key = _norm_text(transaction_type).lower()
        if any(token in transaction_key for token in ["lease-up", "lease up", "development", "new construction", "delivered vacant"]):
            strategy_score = 0.5
        elif any(token in transaction_key for token in ["core", "stabilized"]):
            strategy_score = 0.3
        elif any(token in transaction_key for token in ["value-add", "value add", "mark-to-market", "reposition"]):
            strategy_score = 0.2
        else:
            strategy_score = 0.0

        metro_score = 1.0 if msa.lower() == DEFAULT_MSA.lower() else 0.0
        qualitative = _score_qualitative_signals(fields, transaction_type)
        notes_boost_score = _safe_float(qualitative.get("residential_notes_boost")) or 0.0

        parking_text = " ".join(_norm_text(fields.get(key)) for key in ["notes_raw", "_full_text"])
        parking_score = 0.0
        if re.search(r"parking garage|garage parking|on[- ]site parking|reserved parking|underground parking|assigned parking|covered parking|parking available|parking spots?|parking spaces?|ev parking", parking_text, re.I):
            parking_score = 0.75

        laundry_penalty = 1.0 if qualitative.get("signal_no_in_unit_laundry") else 0.0
        raw_total = (
            geo_score + units_score + price_score + rent_score + vintage_score +
            returns_score + strategy_score + metro_score + notes_boost_score + parking_score
            - laundry_penalty
        )
        residential_score = 1.0 if gated else round(min(10.0, raw_total), 2)

    industrial_score = 0.0 if is_industrial or not is_industrial else 0.0
    score = round(max(residential_score, industrial_score), 2)

    missing = []
    if units is None:
        missing.append("units")
    if price is None:
        missing.append("guidance_price")
    if rent is None:
        missing.append("avg_rent_monthly")
    if vintage is None:
        missing.append("vintage")
    if _safe_float(fields.get("in_place_cap_rate")) is None:
        missing.append("in_place_cap_rate")
    if _safe_float(fields.get("going_in_cash_yield")) is None:
        missing.append("going_in_cash_yield")

    breakdown = (
        f"geo={geo_score:.2f} | units={units_score:.2f} | rent={rent_score:.2f} | vintage={vintage_score:.2f} | "
        f"reno_boost={reno_boost_score:.2f} | strategy={strategy_score:.2f} | metro={metro_score:.2f} | "
        f"notes={notes_boost_score:.2f} | parking={parking_score:.2f} | laundry_penalty={laundry_penalty:.2f} | res_total={residential_score:.2f} | score={score:.2f}"
    )

    result = {
        "is_residential": is_residential,
        "is_industrial": is_industrial,
        "residential_gated": gated,
        "residential_gating_reason": gating_reason,
        "transaction_type": transaction_type,
        "residential_interest_score": residential_score,
        "industrial_interest_score": industrial_score,
        "score": score,
        "score_version": "BR_SCORE_v1_RES",
        "score_breakdown": breakdown,
        "score_component_vector": breakdown,
        "score_missing_inputs": " | ".join(missing) if missing else None,
        "geo_score": geo_score,
        "units_score": units_score,
        "price_score": price_score,
        "rent_score": rent_score,
        "vintage_score": vintage_score,
        "reno_boost_score": reno_boost_score,
        "returns_score": returns_score,
        "strategy_score": strategy_score,
        "metro_score": metro_score,
        "parking_score": parking_score,
        "notes_boost_score": _safe_float(qualitative.get("residential_notes_boost")) or 0.0,
        "total_score": score,
    }
    result.update(qualitative)
    return result


def _extract_neighborhood_name(raw_text: str, neighborhood_section: str) -> str:
    for src in (raw_text, neighborhood_section):
        src = src or ""
        m = re.search(r"about living in ([A-Z][\w' .&-]{2,40}?)\s*(?:\n|Average Prices)", src)
        if m:
            return m.group(1).strip()
        m = re.search(r"base rent averages by bedroom\.?\s*\n?([A-Z][\w' .&-]{2,40}?)\s+San Francisco", src)
        if m:
            return m.group(1).strip()
        m = re.search(r"^([A-Z][\w' .&-]{2,40}?) is a neighborhood", src, re.M)
        if m:
            return m.group(1).strip()
        m = re.search(r"neighborhood of ([A-Z][\w' .&-]{2,40}?)[,.]", src)
        if m:
            return m.group(1).strip()
    return ""


def _extract_city_state(address_line: str) -> Tuple[Optional[str], Optional[str]]:
    if not address_line:
        return None, None
    m = re.search(r",\s*([^,]+),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?$", address_line.strip())
    if not m:
        return None, None
    return m.group(1).strip(), m.group(2).strip()


def _extract_year(text: str) -> Optional[int]:
    matches = re.findall(r"(?:built|year built|built in)\D{0,20}(19\d{2}|20\d{2})", text, flags=re.I)
    years = [int(y) for y in matches if 1900 <= int(y) <= 2035]
    return years[0] if years else None


def _extract_built_and_renovation(text: str) -> Tuple[Optional[int], Optional[int]]:
    pair = re.search(
        r"(?:built|year built|built in)\D{0,20}(19\d{2}|20\d{2})\s*/\s*(19\d{2}|20\d{2})",
        text,
        flags=re.I,
    )
    if pair:
        built_year = int(pair.group(1))
        reno_year = int(pair.group(2))
        if 1900 <= built_year <= 2035 and 1900 <= reno_year <= 2035 and reno_year > built_year:
            return built_year, reno_year
    reno = re.search(r"renovated\s+(?:in\s+)?(?:completed\s+)?(?:in\s+)?(19\d{2}|20\d{2})", text, flags=re.I)
    renovation_year = None
    if reno:
        candidate = int(reno.group(1))
        if 1900 <= candidate <= 2035:
            renovation_year = candidate
    return _extract_year(text), renovation_year


def _no_institutional_mgmt(deal_name: str, address: str, mgmt: Dict[str, Any]) -> Tuple[bool, str]:
    haystack = f"{_norm_text(deal_name)} {_norm_text(address)}".lower()
    if any(token in haystack for token in NON_INSTITUTIONAL_MGMT_OVERRIDES):
        return True, f"manual non-institutional override matched name/address: {deal_name}"
    if any(token in haystack for token in INSTITUTIONAL_MGMT_OVERRIDES):
        return False, f"manual institutional override matched name/address: {deal_name}"
    logo_found = bool(mgmt.get("logo_found"))
    logo_src = _norm_text(mgmt.get("logo_src"))
    link = _norm_text(mgmt.get("link"))
    link_host = (urlparse(link).hostname or "").lower() if link else ""
    if link_host in {"google.com", "www.google.com", "maps.google.com"}:
        link = ""
    if logo_found and logo_src and not logo_src.startswith("data:") and "fallback" not in logo_src.lower():
        return False, f"management company logo present ({logo_src[:80]})"
    if not link:
        return True, "no external management-company link or logo on page (self-managed heuristic)"
    host = (urlparse(link).hostname or "").lower()
    host_slug = re.sub(r"[^a-z0-9]", "", host)
    name_words = re.sub(r"\b(?:the|apartments?|apts?)\b", "", deal_name.lower())
    for word, digit in [("one", "1"), ("two", "2"), ("three", "3"), ("four", "4"), ("five", "5"),
                        ("six", "6"), ("seven", "7"), ("eight", "8"), ("nine", "9"), ("zero", "0")]:
        name_words = re.sub(rf"\b{word}\b", digit, name_words)
    name_slug = re.sub(r"[^a-z0-9]", "", name_words)
    if name_slug and len(name_slug) >= 6 and name_slug in host_slug:
        return True, f"property name appears in mgmt link host ({host}) — self-branded site"
    return False, f"external management link to {host}"


def _is_individual_unit_listing(address: str) -> bool:
    street = _norm_text(address.split(",")[0] if address else "")
    if not street:
        return False
    return bool(re.search(r"\b(?:apt|apartment|unit|ste|suite)\s*#?\s*[A-Za-z0-9-]+\b|#\s*[A-Za-z0-9-]+", street, re.I))


def _extract_units(text: str) -> Optional[int]:
    patterns = [
        r"(\d{2,4})\s+(?:units|unit apartment homes|apartment homes|apartments)",
        r"(?:total\s+units|units)\D{0,20}(\d{2,4})",
        r"(\d{2,4})\s+units\s*/\s*\d+\s+stories",
        r"property information\D{0,80}(\d{2,4})\s+units",
    ]
    street_suffix_re = re.compile(
        r"^\s+[nsew]?\.?\s*[a-z0-9 .]{2,30}\b(?:st|street|ave|avenue|blvd|boulevard|dr|drive|rd|road|way|ln|lane|ct|court|cir|circle|pkwy|ter|terrace|pl|place)\b",
        re.I,
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            units = _safe_int(m.group(1))
            if units and 10 <= units <= 1200:
                if street_suffix_re.match(text[m.end(1):m.end(1) + 45]):
                    continue
                return units
    return None


def _parse_rating(value: str) -> Optional[float]:
    m = re.search(r"(10(?:\.0)?|[0-9](?:\.\d)?)", value or "")
    if not m:
        return None
    try:
        rating = float(m.group(1))
    except ValueError:
        return None
    if 0 <= rating <= 10:
        return rating
    return None


def _pick_best_1br(pricing_rows: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    candidates = []
    unavailable_candidates = []
    saw_unavailable_1br = False
    saw_unquoted_1br = False
    for row in pricing_rows:
        bed_label = _norm_text(row.get("beds") or row.get("label"))
        if not bed_label:
            continue
        if not re.search(r"(^|\b)1\s*(?:bed|br|bedroom)\b|^1x", bed_label.lower()):
            continue
        rent_min = _safe_float(row.get("rent_min"))
        rent_max = _safe_float(row.get("rent_max"))
        size_min = _safe_float(row.get("size_min"))
        size_max = _safe_float(row.get("size_max"))
        if rent_min is None and rent_max is None:
            saw_unquoted_1br = True
            continue
        chosen_rent = rent_min if rent_min is not None else rent_max
        if rent_min is not None and rent_max is not None:
            chosen_rent = rent_min
        chosen_size = size_min if size_min is not None else size_max
        if size_min is not None and size_max is not None:
            chosen_size = size_min
        availability = _norm_text(row.get("availability"))
        size_penalty = abs(chosen_size - 780) if chosen_size is not None else 10000
        if re.search(r"not available|waitlist", availability, re.I):
            saw_unavailable_1br = True
            unavailable_candidates.append((chosen_rent, size_penalty, chosen_size, availability, bed_label))
            continue
        if re.search(r"available\s+now|now available|immediately|\bnow\b|\d+\s+(?:available|units?)", availability, re.I):
            available_now = 0
        else:
            available_now = 2
        avail_num = 99
        avail_match = re.search(r"(\d+)\s+(?:units?|available)", availability, re.I)
        if avail_match:
            avail_num = int(avail_match.group(1))
        candidates.append((available_now, avail_num, chosen_rent, size_penalty, chosen_size, availability, bed_label))

    def _cheapest_over_600(pool, size_idx):
        above = [c for c in pool if c[size_idx] is None or c[size_idx] >= 600]
        target = above if above else pool
        return target[0] if target else None

    if not candidates:
        if unavailable_candidates:
            unavailable_candidates.sort(key=lambda x: (x[0], x[1]))
            fb = _cheapest_over_600(unavailable_candidates, 2)
            fb_rent, _, fb_size, fb_availability, fb_bed_label = fb
            fb_rent_psf = round(fb_rent / fb_size, 2) if fb_rent and fb_size else None
            fb_note = f"{fb_bed_label} | {fb_availability} | floorplan unavailable; listed rent".strip(" |")
            if fb_size is not None:
                fb_note = f"{fb_note} | {int(fb_size)} SF"
            return fb_rent, fb_size, fb_rent_psf, fb_note
        if saw_unavailable_1br:
            return None, None, None, "no available 1BR units"
        if saw_unquoted_1br:
            return None, None, None, "1BR floorplans not quoted on site"
        return None, None, None, None
    candidates.sort(key=lambda x: (x[2], x[0], x[1], x[3]))
    best = _cheapest_over_600(candidates, 4)
    rent = best[2]
    size = best[4]
    rent_psf = round(rent / size, 2) if rent and size else None
    note = f"{best[6]} | {best[5]}".strip(" |")
    if size is not None:
        note = f"{note} | {int(size)} SF"
    return rent, size, rent_psf, note


def _extract_title_from_raw_text(raw_text: str, url: str) -> str:
    text = str(raw_text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines[:40]):
        if re.fullmatch(r"\d{2,6} .+?, .+?, [A-Z]{2} \d{5}(?:-\d{4})?", line):
            if i > 0:
                prev = _norm_text(lines[i - 1])
                if prev and not re.search(r"apartments for rent in|\b\d+ rentals\b|menu|english|save search|sort", prev, re.I):
                    return prev
    return _title_from_property_url(url)


def _extract_address_from_raw_text(raw_text: str) -> str:
    text = str(raw_text or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:80]:
        if re.fullmatch(r"\d{2,6} .+?, .+?, [A-Z]{2} \d{5}(?:-\d{4})?", line):
            return _norm_text(line)
    m = re.search(r"(\d{2,6}[^\n]*?,\s*[A-Za-z .'-]+,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?)", text)
    return _norm_text(m.group(1)) if m else ""


def _extract_1br_rent_from_raw_text(raw_text: str) -> Optional[float]:
    text = str(raw_text or "")
    patterns = [
        r"1\s*Bed\s*\$\s*([\d,]+)\+?",
        r"\$\s*([\d,]+)\+?\s*1\s*Bed",
        r"1\s*Bedroom\s*\$\s*([\d,]+)\+?",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return _safe_float(m.group(1))
    return None


def _extract_1br_tuple_from_raw_text(raw_text: str) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    text = str(raw_text or "")
    text = re.split(r"average prices by area|compare neighborhood and city", text, flags=re.I)[0]
    normalized = _norm_text(text)
    patterns = [
        r"([A-Z0-9-]{2,10})\s+\$\s*([\d,]+)(?:\s*[-–]\s*\$\s*([\d,]+))?.{0,120}?1\s*Bed\s*[•\-\|]\s*1\s*Bath\s*[•\-\|]\s*(\d{1,2}(?:,\d{3})+|\d{3,4})\s*Sq\s*Ft",
        r"1\s*Bed\D{0,40}\$\s*([\d,]+)\+?\D{0,40}(\d{1,2}(?:,\d{3})+|\d{3,4})\s*(?:sq\.?\s*ft\.?|square feet|sq ft)",
        r"\$\s*([\d,]+)\+?\D{0,40}1\s*Bed\D{0,40}(\d{1,2}(?:,\d{3})+|\d{3,4})\s*(?:sq\.?\s*ft\.?|square feet|sq ft)",
        r"1\s*Bed\D{0,120}\$\s*([\d,]+)\+?.*?(\d{1,2}(?:,\d{3})+|\d{3,4})\s*(?:sq\.?\s*ft\.?|square feet|sq ft)",
    ]
    for pattern in patterns:
        m = re.search(pattern, normalized, re.I)
        if not m:
            continue
        if pattern.startswith("([A-Z0-9-]{2,10})"):
            rent = _safe_float(m.group(2))
            if rent is None and m.group(3):
                rent = _safe_float(m.group(3))
            size = _safe_float(m.group(4))
            note = _norm_text(m.group(0))[:200]
        else:
            rent = _safe_float(m.group(1))
            size = _safe_float(m.group(2))
            note = _norm_text(m.group(0))[:200]
        if rent is None:
            continue
        rent_psf = round(rent / size, 2) if rent and size else None
        return rent, size, rent_psf, note

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if not re.search(r"1\s*Bed", line, re.I):
            continue
        if not re.search(r"bed\s*/|bath|\$|\d{3,4}\s*sq\s*ft", line, re.I):
            continue
        window = " ".join(lines[max(0, idx - 2): min(len(lines), idx + 4)])
        price_match = re.search(r"\$\s*([\d,]+)(?:\s*[-–]\s*\$\s*([\d,]+))?", window)
        size_match = re.search(r"(\d{1,2}(?:,\d{3})+|\d{3,4})\s*Sq\s*Ft", window, re.I)
        if price_match and size_match:
            rent = _safe_float(price_match.group(1))
            size = _safe_float(size_match.group(1))
            if rent is not None:
                rent_psf = round(rent / size, 2) if rent and size else None
                note = _norm_text(window)[:200]
                return rent, size, rent_psf, note
    return None, None, None, None


def _extract_text_section(text: str, start_label: str, end_labels: List[str], preserve_lines: bool = False) -> str:
    if not text:
        return ""
    pattern = rf"(?:^|\n)\s*{re.escape(start_label)}\s*\n+(.*?)(?=(?:^|\n)\s*(?:{'|'.join(re.escape(label) for label in end_labels)})\s*(?:\n|$)|$)"
    match = re.search(pattern, text, flags=re.I | re.S)
    if not match:
        return ""
    return match.group(1).strip() if preserve_lines else _norm_text(match.group(1))


def _merge_school_ratings(schools: List[Dict[str, Any]], ratings_by_name: Dict[str, str]) -> List[Dict[str, Any]]:
    return schools


def _is_dark_forest_green(color_value: str) -> bool:
    if not color_value:
        return False
    m = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", color_value)
    if not m:
        return False
    r, g, b = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return g >= 70 and g <= 130 and r <= 25 and b <= 90 and (g - r) >= 45 and (g - b) >= 10


def _school_bucket(text: str) -> Optional[str]:
    value = (text or "").lower()
    if "elementary" in value:
        return "elementary"
    if "middle" in value or re.search(r"grades\s+6-8|grades\s+k-8|grades\s+pk-8", value):
        return "middle"
    if "high" in value or re.search(r"grades\s+9-12", value):
        return "high"
    return None


def _school_buckets(text: str) -> List[str]:
    value = (text or "").lower()
    buckets = set()
    if "elementary" in value:
        buckets.add("elementary")
    if "middle" in value or re.search(r"grades\s+6-8|grades\s+k-8|grades\s+pk-8", value):
        buckets.add("middle")
    if "high" in value or re.search(r"grades\s+9-12", value):
        buckets.add("high")
    if re.search(r"(?:pk|k)\s*-\s*[7-8]", value) and ("elementary" in value or re.search(r"grades\s+(?:pk|k)\s*-\s*[7-8]", value)):
        buckets.update({"elementary", "middle"})
    if not buckets:
        fallback = _school_bucket(text)
        if fallback:
            buckets.add(fallback)
    return sorted(buckets)


def _extract_school_name(raw: str, fallback: str = "") -> str:
    text = _norm_text(raw)
    patterns = [
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Middle School)",
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+High School)",
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Elementary School)",
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Elementary)",
        r"([A-Z][A-Za-z0-9.'&-]+(?:\s+[A-Z][A-Za-z0-9.'&-]+){0,6}\s+Montessori)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _norm_text(match.group(1))
    return _norm_text(fallback).split(" Grades ")[0].strip()


def _attendance_zone_school_entries(schools: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    entries: List[Dict[str, str]] = []
    seen = set()
    for school in schools:
        raw = _norm_text(school.get("raw"))
        zone = _norm_text(school.get("zone"))
        if zone != "Attendance Zone" and not re.search(r"attendance zone", raw, re.I):
            continue
        name = _extract_school_name(raw, _norm_text(school.get("name")))
        grades_match = re.search(r"(Grades\s+[A-Z0-9-]+)", raw, re.I)
        grades = _norm_text(grades_match.group(1)) if grades_match else _norm_text(school.get("grades"))
        bucket = _school_bucket(" ".join([name, grades, raw])) or "other"
        buckets = _school_buckets(" ".join([name, grades, raw])) or [bucket]
        key = (name.lower(), grades.lower(), bucket)
        if not name or key in seen:
            continue
        seen.add(key)
        entries.append({
            "name": name,
            "grades": grades,
            "bucket": bucket,
            "buckets": buckets,
            "badge_color": _norm_text(school.get("badge_color")),
            "rating": _norm_text(school.get("rating")),
        })
    return entries


def _education_badge_flags(schools: List[Dict[str, Any]]) -> str:
    entries = _attendance_zone_school_entries(schools)

    def _dark_green(entry: Dict[str, str]) -> bool:
        return (
            _is_dark_forest_green(entry.get("badge_color", ""))
            or (_parse_rating(entry.get("rating", "")) or 0) >= 7.0
        )

    elementary_dark = any("elementary" in e.get("buckets", [e["bucket"]]) and _dark_green(e) for e in entries)
    middle_dark = any("middle" in e.get("buckets", [e["bucket"]]) and _dark_green(e) for e in entries)
    return f"attendance_zone_elementary_dark_green={'yes' if elementary_dark else 'no'}; attendance_zone_middle_dark_green={'yes' if middle_dark else 'no'}"


def _parse_schools_section(section_text: str) -> Tuple[str, bool]:
    if not section_text:
        return "", False
    lines = [line.strip() for line in section_text.splitlines() if line.strip()]
    schools: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in lines:
        if line in {"Schools", "Public Schools", "Private Schools"}:
            continue
        is_type_line = bool(re.match(r"^(?:public|private)\s", line, re.I))
        is_school_name = (
            re.fullmatch(r".+School", line)
            or re.fullmatch(r".+Elementary", line)
            or re.fullmatch(r".+High", line)
            or re.fullmatch(r".+Middle School", line)
            or re.fullmatch(r".+Intermediate", line)
        )
        if is_school_name and not is_type_line:
            if current:
                schools.append(current)
            current = {"name": line, "type": "", "grades": "", "students": "", "rating": "", "zone": ""}
            continue
        if current is None:
            continue
        if re.search(r"^\d+(?:\.\d+)?/10$", line):
            current["rating"] = line
        elif line in {"Nearby", "Attendance Zone"}:
            current["zone"] = line
        elif line.lower().startswith("grades"):
            current["grades"] = line
        elif re.search(r"students?$", line, re.I):
            current["students"] = line
        elif not current.get("type"):
            current["type"] = line
    if current:
        schools.append(current)
    if not schools:
        return _norm_text(section_text)[:2000], False
    attendance = [s for s in schools if s.get("zone") == "Attendance Zone"]
    if not attendance:
        return "", False
    parts = []
    bucket_ratings: Dict[str, float] = {}
    for school in attendance:
        rating = _parse_rating(school.get("rating"))
        for bucket in _school_buckets(" ".join([school.get("type", ""), school.get("grades", ""), school.get("name", "")])):
            if rating is not None and bucket not in bucket_ratings:
                bucket_ratings[bucket] = rating
        part = " | ".join(p for p in [school.get("name"), school.get("rating"), school.get("grades"), school.get("zone")] if p)
        if part:
            parts.append(part)
    elementary_dark = bucket_ratings.get("elementary", 0) >= 7.0
    middle_dark = bucket_ratings.get("middle", 0) >= 7.0
    strong = elementary_dark and middle_dark
    summary = "; ".join(parts)
    flags = f"attendance_zone_elementary_dark_green={'yes' if elementary_dark else 'no'}; attendance_zone_middle_dark_green={'yes' if middle_dark else 'no'}"
    avg = sum(bucket_ratings.values()) / len(bucket_ratings) if bucket_ratings else None
    if avg is not None:
        summary = f"attendance_zone_avg={avg:.1f}; {summary}"
    summary = f"{summary}; {flags}" if summary else flags
    return summary[:2000], strong


def _merge_school_ratings(schools: List[Dict[str, Any]], ratings_by_name: Dict[str, str]) -> List[Dict[str, Any]]:
    if not ratings_by_name:
        return schools
    merged = []
    for school in schools:
        item = dict(school)
        name = _norm_text(item.get("name"))
        if not item.get("rating") and name:
            direct = ratings_by_name.get(name)
            if not direct:
                for key, value in ratings_by_name.items():
                    if name in key or key in name:
                        direct = value
                        break
            if direct:
                item["rating"] = direct
        merged.append(item)
    return merged


def _summarize_schools(schools: List[Dict[str, Any]]) -> Tuple[str, bool]:
    if not schools:
        return "", False
    parts = []
    ratings = []
    for school in schools[:6]:
        name = _norm_text(school.get("name"))
        grade = _norm_text(school.get("grades"))
        distance = _norm_text(school.get("distance"))
        rating_text = _norm_text(school.get("rating"))
        rating = _parse_rating(rating_text)
        if rating is not None:
            ratings.append(rating)
        school_part = " | ".join(p for p in [name, rating_text, grade, distance] if p)
        if school_part:
            parts.append(school_part)
    avg = sum(ratings) / len(ratings) if ratings else None
    strong = avg is not None and avg >= 7.5 and len(ratings) >= 2
    summary = "; ".join(parts)
    if avg is not None:
        summary = f"avg_rating={avg:.1f}; {summary}"
    return summary, strong


def _summarize_attendance_zone_school_cards(schools: List[Dict[str, Any]]) -> Tuple[str, bool]:
    entries = _attendance_zone_school_entries(schools)
    if not entries:
        return "", False
    parts = []
    bucket_ratings: Dict[str, float] = {}
    for school in entries:
        name = school.get("name", "")
        grades = school.get("grades", "")
        bucket = school.get("bucket", "")
        part = " | ".join(p for p in [name, grades] if p)
        if part:
            parts.append(part)
        rating = _parse_rating(school.get("rating", ""))
        if rating is None and _is_dark_forest_green(school.get("badge_color", "")):
            rating = 7.0
        for b in school.get("buckets") or ([bucket] if bucket else []):
            if rating is not None and rating >= 7.0 and b not in bucket_ratings:
                bucket_ratings[b] = rating
    elementary_dark = bucket_ratings.get("elementary", 0) >= 7.0
    middle_dark = bucket_ratings.get("middle", 0) >= 7.0
    strong = elementary_dark and middle_dark
    summary = "; ".join(parts)
    return summary[:2000], strong


def _strong_schools_from_badges(schools: List[Dict[str, Any]]) -> bool:
    flags = _education_badge_flags(schools)
    return "attendance_zone_elementary_dark_green=yes" in flags and "attendance_zone_middle_dark_green=yes" in flags


def _summarize_transit(transit_items: List[Dict[str, Any]], neighborhood_text: str) -> Tuple[str, bool, bool]:
    parts = []
    rail_hit = False
    for item in transit_items[:8]:
        label = _norm_text(item.get("label"))
        distance = _norm_text(item.get("distance"))
        mode = _norm_text(item.get("mode"))
        piece = " | ".join(p for p in [label, mode, distance] if p)
        if piece:
            parts.append(piece)
        dist_m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mi\b", distance, re.I)
        if re.search(r"caltrain|bart|commuter|rail|train|ferry|subway|station", f"{label} {mode}", re.I) and dist_m and float(dist_m.group(1)) <= 1.5:
            rail_hit = True
    if not rail_hit:
        for m in re.finditer(r"caltrain|bart|light rail|commuter rail|ferry|subway|train station|station", neighborhood_text, re.I):
            window = neighborhood_text[m.end():m.end() + 80]
            dist = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mi(?:les)?\b", window, re.I)
            if dist and float(dist.group(1)) <= 1.5:
                rail_hit = True
                break
    highway_hit = bool(re.search(r"(?:highway|freeway|route)\s*(?:entrance|access|on[- ]?ramp|off[- ]?ramp|interchange)|\b(?:us[- ]?101|interstate ?101|i[- ]?280|interstate ?280|i[- ]?380|sr[- ]?92|(?:hwy|highway|route)\s*-?\s*(?:101|280|380|92)|101\s+freeway|280\s+freeway|bayshore\s+f(?:ree)?wa?y|junipero serra f(?:ree)?wa?y|the (?:101|280|380|92))\b", neighborhood_text, re.I))
    summary = "; ".join(parts)
    if highway_hit:
        summary = (summary + "; highway access mentioned").strip("; ")
    return summary, rail_hit, highway_hit


def _resolve_county(city: Optional[str], state: Optional[str], fallback: str) -> str:
    if not city or not state:
        return fallback
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return fallback
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=50,
            system="Return only the county name in the format 'X County' for the given US city and state. If unsure, return UNKNOWN.",
            messages=[{"role": "user", "content": f"{city}, {state}"}],
        )
        parts = getattr(msg, "content", None) or []
        county = "\n".join(getattr(p, "text", "") for p in parts if getattr(p, "type", None) == "text").strip()
        if county and county.upper() != "UNKNOWN" and "county" in county.lower():
            return county
    except Exception:
        pass
    return fallback


def _next_uid(ws, header_map: Dict[str, int]) -> int:
    uid_col = header_map.get("uid")
    max_uid = UID_START - 1
    if not uid_col:
        return UID_START
    for row in range(2, ws.max_row + 1):
        val = ws.cell(row, uid_col).value
        uid = _safe_int(val)
        if uid is not None and uid > max_uid:
            max_uid = uid
    return max_uid + 1


def _build_header_map(ws) -> Dict[str, int]:
    out = {}
    for col in range(1, ws.max_column + 1):
        key = ws.cell(1, col).value
        if key is None:
            continue
        header_key = str(key).strip().lower()
        if header_key not in out:
            out[header_key] = col
    return out


def _ensure_header_columns(ws, header_map: Dict[str, int], columns: List[str]) -> None:
    for header in columns:
        header_key = str(header).strip().lower()
        if not header_key or header_key in header_map:
            continue
        col = ws.max_column + 1
        ws.cell(1, col).value = header
        header_map[header_key] = col


def _upsert_row(ws, header_map: Dict[str, int], row_data: Dict[str, Any]) -> None:
    url_key = row_data.get("source_url")
    deal_name = _norm_text(row_data.get("deal_name"))
    address = _norm_text(row_data.get("address"))
    target_row = None
    source_col = header_map.get("source_mailbox")
    deal_name_col = header_map.get("deal_name")
    address_col = header_map.get("address")
    for row_idx in range(2, ws.max_row + 1):
        existing_source = _norm_text(ws.cell(row_idx, source_col).value) if source_col else ""
        existing_name = _norm_text(ws.cell(row_idx, deal_name_col).value) if deal_name_col else ""
        existing_address = _norm_text(ws.cell(row_idx, address_col).value) if address_col else ""
        if url_key and existing_source == url_key:
            target_row = row_idx
            break
        if deal_name and address and existing_name == deal_name and existing_address == address:
            target_row = row_idx
            break
    if target_row is None:
        target_row = ws.max_row + 1
    for key, value in row_data.items():
        header_key = key.lower()
        col = header_map.get(header_key)
        if not col:
            continue
        ws.cell(target_row, col).value = value


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
    if not path:
        return False
    tour_markers = ("virtual-tour", "virtual_tour", "matterport", "3d-tour", "video-tour", "/tours/", "/tour/")
    if any(marker in path.lower() for marker in tour_markers):
        return False
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2:
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
    }
    if slug.lower() in blocked_tokens or listing_id.lower() in blocked_tokens:
        return False
    if re.search(r"county-[a-z]{2}$", slug, re.I):
        return False
    return True


def _title_from_property_url(url: str) -> str:
    if not _is_property_url(url):
        return ""
    try:
        slug = [p for p in urlparse(url).path.strip("/").split("/") if p][0]
    except Exception:
        return ""
    parts = [p for p in slug.split("-") if p and p.lower() not in {"ca", "tx", "fl", "ny", "wa", "or", "ma", "il"}]
    if len(parts) > 2:
        parts = parts[:-2]
    return " ".join(p.capitalize() for p in parts).strip()


def _detail_looks_consistent(requested_url: str, fields: Dict[str, Any]) -> bool:
    deal_name = _norm_text(fields.get("deal_name"))
    address = _norm_text(fields.get("address"))
    if not deal_name or not address:
        return False
    expected_title = _title_from_property_url(requested_url).lower()
    haystack = f"{deal_name} {address}".lower()
    if expected_title and expected_title.split()[0] not in haystack:
        return False
    return True


def _collect_listing_cards(page) -> List[Dict[str, str]]:
    cards = []
    seen = set()
    stagnant = 0
    prev_count = 0
    for _ in range(30):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.2)
        items = page.evaluate(
            """() => {
                const results = [];
                const txt = (el) => (el && el.innerText ? el.innerText.replace(/\s+/g, ' ').trim() : '');
                let anchors = Array.from(document.querySelectorAll('a.property-link[href]')).filter(a => a.href);
                if (!anchors.length) anchors = Array.from(document.querySelectorAll('a[href]'));
                anchors.forEach((a) => {
                    const href = a.href || '';
                    if (!href.includes('apartments.com/')) return;
                    if (/virtual[-_ ]?tour|matterport|3d[-_ ]?tour|video[-_ ]?tour|\/tours?\//i.test(href)) return;
                    if (href.includes('/apartments/') || href.includes('#')) return;
                    if (/\/my-account|\/mkt\/|\/rental-manager|\/add-listing|\/favorites|\/savedsearch|\/moving-center|\/sell|\/manage-property/i.test(href)) return;
                    const path = new URL(href).pathname || '';
                    const parts = path.split('/').filter(Boolean);
                    if (parts.length < 1) return;
                    const last = parts[parts.length - 1] || '';
                    if (!/^[a-z0-9-]+$/i.test(last)) return;
                    if (/county-[a-z]{2}$/i.test(last)) return;
                    if (/^(houses|condos|townhomes|apartments|cheap|luxury|pet-friendly|senior|studio|1-bedroom|2-bedroom|3-bedroom)$/i.test(parts[0] || '')) return;
                    let card = a;
                    let chosenText = txt(a);
                    let node = a;
                    for (let i = 0; i < 7 && node; i += 1) {
                        const candidate = node.closest('article, li, section, [role="listitem"], div');
                        if (!candidate) break;
                        const candidateText = txt(candidate);
                        if (candidateText && candidateText.length >= 40 && candidateText.length <= 420 && /\d{2,6} .+?, .+?, [A-Z]{2} \d{5}|\$\d|Studio|1 Bed|2 Beds/i.test(candidateText)) {
                            card = candidate;
                            chosenText = candidateText;
                        }
                        node = candidate.parentElement;
                    }
                    const text = chosenText || txt(card) || txt(a);
                    const name = (a.getAttribute('aria-label') || '').trim();
                    results.push({ url: href, text, name });
                });
                return results;
            }"""
        )
        unique_count = 0
        for item in items:
            url = item.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            unique_count += 1
            cards.append(item)
        cur_count = len(cards)
        if cur_count == prev_count:
            stagnant += 1
        else:
            stagnant = 0
        prev_count = cur_count
        if stagnant >= 4:
            break
    return cards


def _capture_selected_page_text(page) -> str:
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.5)
        page.locator("body").click(timeout=3000)
        time.sleep(0.3)
        page.keyboard.press("Meta+A")
        time.sleep(0.5)
        selected = page.evaluate("() => window.getSelection ? window.getSelection().toString() : ''") or ""
        if selected.strip():
            return selected
    except Exception:
        pass
    return page.evaluate("() => document.body && document.body.innerText ? document.body.innerText : ''") or ""


def _capture_school_rating_ocr(page) -> Dict[str, str]:
    return {}


PROPERTY_SITE_TEXT_PATH = Path(__file__).resolve().parent.parent / "data" / "property_site_text.jsonl"
PROPERTY_SITE_MD_DIR = Path(__file__).resolve().parent.parent / "data" / "property_sites"
_PROPERTY_SITE_SUBPAGE_RE = re.compile(
    r"about|community|neighbou?rhood|location|amenit|features?|lifestyle|the[- ]area|"
    r"explore|living|our[- ]story|the[- ]building|residences|floor\s*plans?",
    re.I,
)
_PROPERTY_SITE_MAX_CHARS = 20000


def _write_property_site_md(record: Dict[str, Any]) -> None:
    PROPERTY_SITE_MD_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", str(record.get("deal_name") or "unknown").lower()).strip("_") or "unknown"
    lines = [
        f"# {record.get('deal_name')}",
        "",
        f"- **Address**: {record.get('address')}",
        f"- **Apartments.com**: {record.get('apartments_url')}",
        f"- **Property site**: {record.get('property_site_url')}",
        f"- **Captured**: {record.get('captured_at')}",
        "",
    ]
    for pg in record.get("pages") or []:
        lines += [f"## {pg.get('page')}", f"<{pg.get('url')}>", "", pg.get("text") or "", ""]
    (PROPERTY_SITE_MD_DIR / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")


def _page_main_text(page) -> str:
    try:
        return _norm_text(page.evaluate(
            """() => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('script, style, noscript, svg, nav, header, footer, form, iframe').forEach((el) => el.remove());
                const main = clone.querySelector('main') || clone;
                return main.innerText || '';
            }"""
        ))
    except Exception:
        return ""


def _scrape_property_site(context, site_url: str, max_subpages: int = 4) -> Dict[str, Any]:
    result: Dict[str, Any] = {"url": site_url, "pages": [], "text": ""}
    page = None
    try:
        page = context.new_page()
        page.goto(site_url, timeout=30000, wait_until="domcontentloaded")
        time.sleep(3)
        home_url = _norm_text(page.evaluate("() => location.href")) or site_url
        result["url"] = home_url
        home_text = _page_main_text(page)
        if home_text:
            result["pages"].append({"page": "home", "url": home_url, "text": home_text[:12000]})
        try:
            page.evaluate(
                """() => {
                    const sels = [
                        'button[aria-label*="menu" i]', 'button[aria-label*="navigation" i]',
                        '.menu-toggle', '.hamburger', '.nav-toggle', '.navbar-toggler',
                        '[class*="hamburger"]', '[class*="menu-toggle"]', 'a[href="#menu"]',
                        'button:has(span.sr-only)',
                    ];
                    for (const sel of sels) {
                        const el = document.querySelector(sel);
                        if (el) { try { el.click(); } catch (e) {} }
                    }
                }"""
            )
            time.sleep(1.5)
        except Exception:
            pass
        try:
            links = page.evaluate(
                """() => {
                    const host = location.hostname;
                    const seen = new Set();
                    const out = [];
                    document.querySelectorAll('a[href]').forEach((a) => {
                        let href = a.href.split('#')[0];
                        if (!href || seen.has(href)) return;
                        let u;
                        try { u = new URL(href); } catch (e) { return; }
                        if (u.hostname !== host) return;
                        const label = ((a.innerText || '') + ' ' + u.pathname).replace(/\s+/g, ' ').trim();
                        out.push({href, label});
                        seen.add(href);
                    });
                    return out;
                }"""
            )
        except Exception:
            links = []
        sub_urls = [
            l["href"] for l in links
            if _PROPERTY_SITE_SUBPAGE_RE.search(l.get("label") or "")
        ][:max_subpages]
        for sub_url in sub_urls:
            try:
                page.goto(sub_url, timeout=20000, wait_until="domcontentloaded")
                time.sleep(2)
                sub_text = _page_main_text(page)
                if sub_text:
                    slug = _norm_text(sub_url.rstrip("/").rsplit("/", 1)[-1]) or "subpage"
                    result["pages"].append({"page": slug[:60], "url": sub_url, "text": sub_text[:12000]})
            except Exception:
                continue
        seen_chunks = set()
        parts = []
        total = 0
        for pg in result["pages"]:
            chunk = pg["text"]
            key = hash(chunk)
            if not chunk or key in seen_chunks:
                continue
            seen_chunks.add(key)
            parts.append(chunk)
            total += len(chunk)
            if total >= _PROPERTY_SITE_MAX_CHARS:
                break
        result["text"] = "\n\n".join(parts)[:_PROPERTY_SITE_MAX_CHARS]
    except Exception as exc:
        print(f"    property site scrape failed ({site_url}): {exc}", flush=True)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
    return result


def _scrape_detail(page, url: str, navigate: bool = True) -> Dict[str, Any]:
    if navigate:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        time.sleep(4)
    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('a, button, span, div').forEach((el) => {
                    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                    if (/^(?:read\s*more|show\s*full\s*review|show\s*more)$/i.test(t)) {
                        try { el.click(); } catch (e) {}
                    }
                });
            }"""
        )
        time.sleep(1)
    except Exception:
        pass
    initial_raw_text = _capture_selected_page_text(page)
    for _ in range(25):
        at_bottom = page.evaluate(
            "() => { window.scrollBy(0, 900); return Math.ceil(window.scrollY + window.innerHeight) >= document.body.scrollHeight; }"
        )
        time.sleep(0.75)
        if at_bottom:
            break
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(3)
    tab_sections: Dict[str, str] = {}
    school_rating_ocr: Dict[str, str] = {}
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(1)
    detail = page.evaluate(
        """() => {
            const q = (sel, root=document) => root.querySelector(sel);
            const qa = (sel, root=document) => Array.from(root.querySelectorAll(sel));
            const txt = (el) => (el && el.innerText ? el.innerText.replace(/\s+/g, ' ').trim() : '');
            const linesOf = (value) => value.split('|').map(s => s.trim()).filter(Boolean);
            const text = txt(document.body);
            const canonical = q('link[rel="canonical"]')?.href || location.href;
            const title = txt(q('h1')) || document.title || '';
            let address = '';
            const addrEl = q('[data-test="property-address"]') || q('.propertyAddress') || q('header address');
            if (addrEl) address = txt(addrEl);
            if (!address) {
                const candidates = qa('address, .address, [class*="address"]');
                for (const el of candidates) {
                    const t = txt(el);
                    if (/,\s*[A-Z]{2}\s*\d{5}/.test(t)) { address = t; break; }
                }
            }
            const pricingRows = [];
            qa('tr, [data-test="pricingGridItem"], .pricingGridItem, .rentalGridRow').forEach((row) => {
                const rowText = txt(row);
                if (!rowText) return;
                let anc = row;
                for (let i = 0; i < 6 && anc; i++) {
                    anc = anc.parentElement;
                    if (!anc) break;
                    if (/average prices by area|compare neighborhood and city/i.test(txt(anc).slice(0, 250))) return;
                }
                const cells = qa('td, [role="cell"], .cell', row).map(txt).filter(Boolean);
                const full = cells.length ? cells.join(' | ') : rowText;
                if (row.tagName === 'TR' && !/sq\.?\s*ft|deposit|available|waitlist|\\bnow\\b/i.test(full)) return;
                const rentMatch = full.match(/\$\s*([\d,]+)(?:\s*[-–]\s*\$?\s*([\d,]+)|\s+\$\s*([\d,]+))?/);
                const sizeMatch = full.match(/(\d{1,2}(?:,\d{3})+|\d{3,4})\s*(?:sq\.?\s*ft\.?|square feet|sq ft)/i);
                const sizeRangeMatch = full.match(/(\d{1,2}(?:,\d{3})+|\d{3,4})\s*-\s*(\d{1,2}(?:,\d{3})+|\d{3,4})\s*(?:sq\.?\s*ft\.?|square feet|sq ft)/i);
                let bedCell = cells.find(c => /bed|studio|br|1x|2x/i.test(c)) || '';
                if (!bedCell) {
                    const bedInText = full.match(/\\b(studio|\d\s*bed(?:room)?s?)\\b/i);
                    if (bedInText) bedCell = bedInText[1];
                }
                if (!bedCell) {
                    const planRoot = row.closest('[data-test="pricingGridItem"], .pricingGridItem');
                    if (planRoot) {
                        const bedInRoot = txt(planRoot).match(/\\b(studio|\d\s*bed(?:room)?s?)\\b/i);
                        if (bedInRoot) bedCell = bedInRoot[1];
                    }
                }
                const availability = cells.find(c => /available|units?|now|waitlist|soon/i.test(c)) || rowText;
                pricingRows.push({
                    label: bedCell || rowText.slice(0, 80),
                    beds: bedCell,
                    availability,
                    rent_min: rentMatch ? rentMatch[1] : null,
                    rent_max: rentMatch ? (rentMatch[2] || rentMatch[3] || null) : null,
                    size_min: sizeRangeMatch ? sizeRangeMatch[1] : (sizeMatch ? sizeMatch[1] : null),
                    size_max: sizeRangeMatch ? sizeRangeMatch[2] : null,
                    raw: rowText
                });
            });
            const schools = [];
            qa('.schoolsSection .card, .schoolsSection .component-frame, .profilev2SchoolsModule .card, .profilev2SchoolsModule .component-frame, [data-test="schoolCard"], .educationCard, .schoolCard').forEach((card) => {
                const rawBlock = (card.innerText || '').trim();
                const block = txt(card);
                if (!block) return;
                if (!/Attendance Zone|Nearby|Students/i.test(block)) return;
                if (block.length > 260) return;
                const lines = rawBlock.split(/\\n+/).map(s => s.trim()).filter(Boolean);
                const ratingLine = lines.find((s) => {
                    const parts = s.split('/');
                    return parts.length === 2 && parts[1].trim() === '10' && !Number.isNaN(parseFloat(parts[0]));
                }) || lines.find(s => s.includes('/10')) || '';
                const ratingImage = card.querySelector('img[src*="summary-rating-"]');
                const ratingImageMatch = ratingImage ? (ratingImage.getAttribute('src') || '').match(/summary-rating-(\d+)/) : null;
                schools.push({
                    name: lines[0] || '',
                    rating: ratingLine || (ratingImageMatch ? ratingImageMatch[1] + '/10' : ''),
                    grades: lines.find(s => s.toLowerCase().startsWith('grades ')) || '',
                    distance: lines.find(s => s.toLowerCase().includes(' mi')) || '',
                    zone: lines.find(s => ['attendance zone', 'nearby'].includes(s.toLowerCase())) || '',
                    badge_color: (() => {
                        const hit = Array.from(card.querySelectorAll('*')).find((el) => txt(el).includes('/10'));
                        if (!hit) return '';
                        let node = hit;
                        for (let i = 0; i < 6 && node; i += 1) {
                            const cs = window.getComputedStyle(node);
                            const bg = cs.backgroundColor || '';
                            const radius = cs.borderRadius || '';
                            const width = parseFloat(cs.width || '0');
                            const height = parseFloat(cs.height || '0');
                            const circular = radius.includes('%') || parseFloat(radius || '0') >= Math.min(width, height) / 2 - 2;
                            if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent' && circular && width >= 24 && height >= 24) {
                                return bg;
                            }
                            node = node.parentElement;
                        }
                        const cs = window.getComputedStyle(hit);
                        return cs.backgroundColor || '';
                    })(),
                    raw: rawBlock
                });
            });
            const transit = [];
            qa('[data-test="transportationCard"], .transportationCard, [class*="transport"] li, [class*="transport"] article').forEach((item) => {
                const block = txt(item);
                if (!block) return;
                const lines = linesOf(block);
                transit.push({
                    label: lines[0] || '',
                    mode: lines.find(s => /bus|rail|train|bart|caltrain|transit|airport/i.test(s)) || '',
                    distance: lines.find(s => /[0-9.]+\s*mi\b/i.test(s)) || '',
                    raw: block
                });
            });
            const dedupedReviews = [];
            const seenReviewKeys = new Set();
            const addReviewBlock = (block) => {
                const normalized = txt({ innerText: block });
                if (!normalized) return;
                const key = normalized.slice(0, 240).toLowerCase();
                if (seenReviewKeys.has(key)) return;
                seenReviewKeys.add(key);
                dedupedReviews.push(normalized.slice(0, 1800));
            };

            const reviewRoots = [];
            qa('.reviewsViewModel, [class*="reviewsViewModel"], [class*="renter-review"], [class*="reviewCard"], [class*="reviewSection"], [class*="ReviewSection"], [class*="reviews-section"], [class*="reviews-section"]').forEach((el) => {
                if (el && !reviewRoots.includes(el)) reviewRoots.push(el);
            });

            const reviewHead = qa('h1, h2, h3, h4, h5, [role="heading"], .sectionTitle, [data-test]').find((el) => /renter reviews|reviews for/i.test(txt(el)));
            if (reviewHead) {
                let node = reviewHead;
                for (let i = 0; i < 10 && node; i += 1) {
                    const block = txt(node);
                    if (block && /renter reviews|reviews for/i.test(block)) {
                        if (!reviewRoots.includes(node)) reviewRoots.push(node);
                    }
                    node = node.parentElement;
                }
            }

            reviewRoots.forEach((root) => {
                const rootText = txt(root);
                if (rootText && /was this helpful|show full review|most recent|write a review|renter reviews|reviews for/i.test(rootText)) {
                    addReviewBlock(rootText);
                }
                qa('article, li, section, div', root).forEach((card) => {
                    const block = txt(card);
                    const lower = block.toLowerCase();
                    if (!block) return;
                    if (!(lower.includes('was this helpful') || lower.includes('show full review') || lower.includes('most recent') || lower.includes('write a review') || lower.includes('review'))) return;
                    if (block.length < 40 || block.length > 2600) return;
                    addReviewBlock(block);
                });
            });

            if (!dedupedReviews.length) {
                qa('article, section, div, li').forEach((card) => {
                const block = txt(card);
                const lower = block.toLowerCase();
                if (!block) return;
                if (!(lower.includes('was this helpful') || lower.includes('show full review') || lower.includes('most recent') || lower.includes('review'))) return;
                if (block.length < 40 || block.length > 2200) return;
                    addReviewBlock(block);
                });
            }
            const mgmt = (() => {
                const result = { logo_found: false, logo_src: null, link: '', company: '' };
                const isExternal = (href) => {
                    if (!href || href.startsWith('javascript')) return false;
                    let host = '';
                    try { host = new URL(href).hostname || ''; } catch (e) { return false; }
                    return !!host && !/(^|\.)apartments\.com$/i.test(host);
                };
                const logoImg = qa('img[alt]').find((i) => /property management company logo/i.test(i.getAttribute('alt') || ''));
                if (!logoImg) return result;
                result.logo_found = true;
                result.logo_src = logoImg.currentSrc || logoImg.src || logoImg.getAttribute('data-src') || null;
                let container = logoImg.parentElement;
                for (let depth = 0; depth < 6 && container; depth += 1) {
                    const anchors = qa('a[href]', container).filter((a) => isExternal(a.href));
                    if (anchors.length) {
                        result.link = anchors[0].href;
                        result.company = txt(anchors[0]);
                        return result;
                    }
                    container = container.parentElement;
                }
                let sib = logoImg.nextElementSibling;
                for (let i = 0; i < 4 && sib; i += 1) {
                    const a = sib.tagName === 'A' ? sib : sib.querySelector('a[href]');
                    if (a && isExternal(a.href)) {
                        result.link = a.href;
                        result.company = txt(a);
                        return result;
                    }
                    sib = sib.nextElementSibling;
                }
                const logo_srcs = qa('img[alt="Property Management Company Logo"]').map((img) => img.currentSrc || img.src || img.getAttribute('data-src') || '').filter(Boolean);
                result.logo_src = logo_srcs[0] || null;
                return result;
            })();
            const geo = (() => {
                const out = { lat: null, lng: null };
                const latMeta = document.querySelector('meta[property="place:location:latitude"]');
                const lngMeta = document.querySelector('meta[property="place:location:longitude"]');
                if (latMeta && lngMeta) {
                    const lat = parseFloat(latMeta.content);
                    const lng = parseFloat(lngMeta.content);
                    if (!Number.isNaN(lat) && !Number.isNaN(lng)) {
                        out.lat = lat;
                        out.lng = lng;
                        return out;
                    }
                }
                qa('script[type="application/ld+json"]').forEach((s) => {
                    try {
                        const data = JSON.parse(s.textContent || '');
                        const items = Array.isArray(data) ? data : [data];
                        items.forEach((item) => {
                            const g = item && item.geo;
                            if (g && g.latitude != null && g.longitude != null && out.lat == null) {
                                out.lat = parseFloat(g.latitude);
                                out.lng = parseFloat(g.longitude);
                            }
                        });
                    } catch (e) {}
                });
                return out;
            })();
            const detailBlocks = qa('section, div').map((el) => txt(el)).filter(Boolean).filter((t) => /built|units|apartment homes|neighborhood|schools|transportation/i.test(t)).slice(0, 80);
            return {
                url: canonical,
                title,
                address,
                text,
                geo,
                detail_blocks: detailBlocks,
                pricing_rows: pricingRows,
                schools,
                transit,
                reviews: dedupedReviews,
                mgmt
            };
        }"""
    )
    canonical_url = _norm_text(detail.get("url"))
    if not _is_property_url(canonical_url):
        detail["url"] = url
    all_reviews = list(detail.get("reviews") or [])
    seen_review_keys = {str(r)[:240].lower() for r in all_reviews}
    visited_pages = {1}
    for _ in range(2):
        target_page = page.evaluate(
            """(visited) => {
                const items = Array.from(document.querySelectorAll('a.review-paginator-item'));
                for (const it of items) {
                    const n = parseInt((it.innerText || '').trim(), 10);
                    if (!isNaN(n) && !visited.includes(n) && !it.classList.contains('selected')) return n;
                }
                return null;
            }""",
            sorted(visited_pages),
        )
        if target_page is None:
            break
        visited_pages.add(target_page)
        clicked = page.evaluate(
            """(n) => {
                const it = Array.from(document.querySelectorAll('a.review-paginator-item'))
                    .find(x => parseInt((x.innerText || '').trim(), 10) === n && !x.classList.contains('selected'));
                if (it) { it.click(); return true; }
                return false;
            }""",
            target_page,
        )
        if not clicked:
            break
        time.sleep(2.5)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.5)
        extra = page.evaluate(
            """() => {
                const txt = (el) => (el && el.innerText ? el.innerText.replace(/\s+/g, ' ').trim() : '');
                const out = [];
                document.querySelectorAll('article, li, section, div').forEach((card) => {
                    const block = txt(card);
                    const lower = block.toLowerCase();
                    if (!block || block.length < 40 || block.length > 2600) return;
                    if (!(lower.includes('was this helpful') || lower.includes('show full review'))) return;
                    out.push(block.slice(0, 1800));
                });
                return out;
            }"""
        )
        new_blocks = [b for b in (extra or []) if str(b)[:240].lower() not in seen_review_keys]
        for b in new_blocks:
            seen_review_keys.add(str(b)[:240].lower())
            all_reviews.append(b)
        if not new_blocks:
            break
    if len(all_reviews) > len(detail.get("reviews") or []):
        print(f"    DEBUG review pagination: {len(detail.get('reviews') or [])} -> {len(all_reviews)} blocks", flush=True)
    detail["reviews"] = all_reviews
    post_scroll_text = _capture_selected_page_text(page)
    detail["raw_text"] = post_scroll_text if len(post_scroll_text) > len(initial_raw_text) else initial_raw_text
    detail["school_rating_ocr"] = school_rating_ocr
    detail["tab_sections"] = tab_sections
    return detail


def _build_notes(neighborhood_text: str, school_summary: str, strong_schools: bool, transport_summary: str, transit_hit: bool, pricing_note: Optional[str]) -> Tuple[str, List[str]]:
    signals = []
    notes_parts = []
    if neighborhood_text:
        notes_parts.append(neighborhood_text)
    if pricing_note:
        notes_parts.append(f"1BR basis: {pricing_note}")
    if school_summary:
        notes_parts.append(f"Schools: {school_summary}")
    if transport_summary:
        notes_parts.append(f"Transportation: {transport_summary}")
    if strong_schools:
        signals.append("strong schools")
    if transit_hit:
        signals.append("transit access")
    if re.search(r"limited supply|supply constrained|barriers to entry|high barriers to entry", neighborhood_text, re.I):
        signals.append("limited supply")
    return " | ".join(p for p in notes_parts if p), signals


def _parse_property(detail: Dict[str, Any], county_fallback: str) -> Dict[str, Any]:
    raw_text = str(detail.get("raw_text") or "")
    title = _extract_title_from_raw_text(raw_text, _norm_text(detail.get("url")))
    detail_title = _norm_text(detail.get("title"))
    detail_title = re.sub(r"\s*[-|]\s*Apartments\.com.*$", "", detail_title, flags=re.I).strip()
    if detail_title and not re.search(r"apartments for rent|apartments\.com", detail_title, re.I):
        if len(detail_title) >= len(title):
            title = detail_title
    address = _extract_address_from_raw_text(raw_text)
    city, state = _extract_city_state(address)
    text = _norm_text(detail.get("text"))
    detail_block_text = " | ".join(_norm_text(x) for x in (detail.get("detail_blocks") or []) if _norm_text(x))
    unit_text = " | ".join(part for part in [raw_text, text, detail_block_text] if part)
    neighborhood_section = _extract_text_section(raw_text, "Neighborhood", ["Average Prices by Area", "Education", "Schools", "Transportation", "Points of Interest", "Reviews"])
    neighborhood_name = _extract_neighborhood_name(raw_text, neighborhood_section or "") or None
    if neighborhood_name and neighborhood_name.lower() in ("san francisco", "san mateo"):
        neighborhood_name = None
    schools_section = _extract_text_section(raw_text, "Schools", ["School data provided by", "Transportation", "Parks and Recreation", "Military Bases", "Shopping Centers", "Airports", "Points of Interest", "Reviews"], preserve_lines=True)
    transportation_section = _extract_text_section(raw_text, "Transportation", ["Parks and Recreation", "Military Bases", "Airports", "Points of Interest", "Reviews"])
    about_section = ""
    for about_label in [f"About {title}", "About This Property"]:
        about_section = _extract_text_section(raw_text, about_label, ["Average Prices by Area", "Neighborhood", "Education", "Schools", "Transportation", "Points of Interest", "Reviews", "Lease Details", "Fees", "Ratings"])
        if about_section:
            break
    property_site = detail.get("property_site") or {}
    property_site_text = _norm_text(property_site.get("text"))
    if property_site_text:
        about_section = property_site_text[:6000]
        neighborhood_section = property_site_text[:6000]
    units = _extract_units(unit_text)
    vintage, renovation_year = _extract_built_and_renovation(raw_text)
    if vintage is None:
        vintage = _extract_year(text)
    if renovation_year is None:
        _, renovation_year = _extract_built_and_renovation(text)
    rent, unit_size, rent_psf, pricing_note = _pick_best_1br(detail.get("pricing_rows") or [])
    no_available_1br = pricing_note in ("no available 1BR units", "1BR floorplans not quoted on site")
    if rent is None and not no_available_1br:
        pricing_section = _extract_text_section(raw_text, "Pricing & Floor Plans", ["Fees and Policies", "Fees & Policies", "Amenities", "Neighborhood", "Education", "Transportation"])
        rent, unit_size, rent_psf, pricing_note = _extract_1br_tuple_from_raw_text(pricing_section or raw_text)
    if no_available_1br:
        pricing_note = None
    schools = _merge_school_ratings(detail.get("schools") or [], detail.get("school_rating_ocr") or {})
    school_summary, strong_schools = _summarize_attendance_zone_school_cards(schools)
    education_note = _education_badge_flags(schools)
    parsed_school_summary, parsed_strong_schools = _parse_schools_section(schools_section)
    if parsed_school_summary:
        parsed_flags_match = re.search(
            r"attendance_zone_elementary_dark_green=(?:yes|no);\s*attendance_zone_middle_dark_green=(?:yes|no)",
            parsed_school_summary,
            re.I,
        )
        parsed_flags = parsed_flags_match.group(0) if parsed_flags_match else ""
        if parsed_flags.count("=yes") > education_note.count("=yes") or not school_summary:
            school_summary = re.sub(r";\s*attendance_zone_elementary_dark_green=.*$", "", parsed_school_summary)
            education_note = parsed_flags
            strong_schools = parsed_strong_schools
    if education_note:
        school_summary = "; ".join(p for p in [school_summary, education_note] if p)
    if not strong_schools:
        strong_schools = _strong_schools_from_badges(schools)
    transit_scan_text = " ".join(p for p in [transportation_section or "", about_section, neighborhood_section or ""] if p)
    transport_summary, transit_hit, highway_hit = _summarize_transit(detail.get("transit") or [], transit_scan_text)
    if not highway_hit and re.search(r"(?:highway|freeway|route)\s*(?:entrance|access|on[- ]?ramp|off[- ]?ramp|interchange)|\b(?:us[- ]?101|interstate ?101|i[- ]?280|interstate ?280|i[- ]?380|sr[- ]?92|(?:hwy|highway|route)\s*-?\s*(?:101|280|380|92)|101\s+freeway|280\s+freeway|bayshore\s+f(?:ree)?wa?y|junipero serra f(?:ree)?wa?y|the (?:101|280|380|92))\b", transit_scan_text, re.I):
        highway_hit = True
    if transportation_section and len(transportation_section) > len(transport_summary):
        transport_summary = transportation_section[:2000]
    county = _resolve_county(city, state, county_fallback)
    msa = _resolve_msa(city, state, county)
    mgmt = detail.get("mgmt") or {}
    review_records = detail.get("reviews") or []
    review_parts: List[str] = []
    review_chars = 0
    for review in review_records:
        block = _norm_text(review)
        if not block:
            continue
        if review_chars + len(block) > 25000:
            break
        review_parts.append(block)
        review_chars += len(block)
    review_text = " | ".join(review_parts)
    if review_records:
        print(f"    DEBUG review blocks captured: {len(review_records)}", flush=True)
        for idx, review in enumerate(review_records[:5], 1):
            print(f"    DEBUG review[{idx}]: {_norm_text(review)[:500]}", flush=True)
    else:
        print("    DEBUG review blocks captured: 0", flush=True)
    no_institutional_mgmt, no_institutional_mgmt_evidence = _no_institutional_mgmt(title, address, mgmt)
    neighborhood_excerpt = ""
    if neighborhood_section:
        neighborhood_excerpt = neighborhood_section[:800]
    notes, positive_signals = _build_notes(neighborhood_excerpt, school_summary, strong_schools, transport_summary, transit_hit, pricing_note)
    if highway_hit:
        positive_signals.append("highway access")
    downtown_text = "\n".join(p for p in [about_section or "", neighborhood_section or ""] if p)
    downtown_verdict = _downtown_adjacent_llm(downtown_text)
    if downtown_verdict is True or (downtown_verdict is None and _downtown_adjacent(downtown_text)):
        positive_signals.append("downtown")
    if education_note:
        notes = " | ".join(p for p in [notes, f"Education: {education_note}"] if p)
    if renovation_year is not None:
        reno_note = f"Renovated in {renovation_year}"
        if vintage is not None:
            reno_note = f"Built {vintage}, renovated in {renovation_year}"
        notes = " | ".join(p for p in [notes, reno_note] if p)
    transaction_type = None
    fields: Dict[str, Any] = {
        "deal_name": title,
        "address": address,
        "city": city,
        "state": state,
        "county": county,
        "msa": msa,
        "neighborhood": neighborhood_name,
        "product": "MF",
        "asset_class": "Multifamily",
        "transaction_type": transaction_type,
        "units": units,
        "vintage": vintage,
        "renovation_year": renovation_year,
        "renovated": True if renovation_year is not None else None,
        "signal_no_institutional_mgmt": no_institutional_mgmt,
        "_no_institutional_mgmt_evidence": no_institutional_mgmt_evidence,
        "_has_institutional_mgmt": not no_institutional_mgmt,
        "_no_available_1br": no_available_1br,
        "avg_rent_monthly": rent,
        "rent_psf": rent_psf,
        "rent_units": "monthly_per_unit" if rent is not None else None,
        "unit_size": unit_size,
        "notes": notes,
        "notes_raw": raw_text[:30000] if raw_text else None,
        "positive_signals": positive_signals,
        "neighborhood notes": neighborhood_excerpt,
        "assigned schools": school_summary,
        "transportation notes": transport_summary,
        "email_subject": f"Apartments.com: {title}",
        "email_from": "Apartments.com",
        "source_mailbox": detail.get("url"),
        "_full_text": " | ".join(part for part in [text, review_text] if part),
        "_lat": (detail.get("geo") or {}).get("lat"),
        "_lng": (detail.get("geo") or {}).get("lng"),
        "property_site_url": _norm_text(property_site.get("url")) or None,
        "property_site_text": property_site_text[:15000] or None,
    }
    scoring = _compute_apartment_score(fields)
    fields.update(scoring)
    return fields


def _inspect_school_dom(page) -> Dict[str, Any]:
    return page.evaluate(
        """() => {
            const normalize = s => (s || '').replace(/\s+/g, ' ').trim();
            const target = Array.from(document.querySelectorAll('a,div,span,h2,h3,h4')).find(el => normalize(el.innerText) === 'Lincoln Elementary School');
            if (!target) {
                return {
                    found: false,
                    mentions: Array.from(document.querySelectorAll('a,div,span,h2,h3,h4'))
                        .map(el => normalize(el.innerText))
                        .filter(text => text.includes('Lincoln'))
                        .slice(0, 20)
                };
            }
            const ancestors = [];
            let node = target;
            for (let i = 0; i < 8 && node; i += 1) {
                ancestors.push({
                    level: i,
                    tag: node.tagName,
                    className: String(node.className || ''),
                    dataTest: node.getAttribute('data-test'),
                    text: normalize(node.innerText).slice(0, 600)
                });
                node = node.parentElement;
            }
            const card = target.closest('.card') || target.closest('.component-frame') || target.parentElement;
            const descendants = Array.from(card.querySelectorAll('*')).slice(0, 120).map((el) => {
                const cs = window.getComputedStyle(el);
                return {
                    tag: el.tagName,
                    className: String(el.className || ''),
                    text: normalize(el.innerText).slice(0, 120),
                    ariaLabel: el.getAttribute('aria-label'),
                    title: el.getAttribute('title'),
                    alt: el.getAttribute('alt'),
                    role: el.getAttribute('role'),
                    src: el.getAttribute('src'),
                    backgroundColor: cs.backgroundColor,
                    backgroundImage: cs.backgroundImage,
                    borderRadius: cs.borderRadius,
                    width: cs.width,
                    height: cs.height
                };
            });
            return {found: true, ancestors, descendants};
        }"""
    )


def _open_workbook(path: str, sheet_name: Optional[str]):
    wb = load_workbook(path)
    ws = wb[sheet_name] if sheet_name else wb[wb.sheetnames[0]]
    return wb, ws


def _get_azure_conn():
    import pymssql
    return pymssql.connect(
        server=os.getenv("AZURE_SQL_SERVER"),
        user=os.getenv("AZURE_SQL_USER"),
        password=os.getenv("AZURE_SQL_PASSWORD"),
        database=os.getenv("AZURE_SQL_DATABASE"),
    )


def _validate_table_name(table_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name or ""):
        raise ValueError(f"Invalid Azure SQL table name: {table_name}")
    return table_name


def _azure_table_columns(conn, table_name: str) -> List[str]:
    table_name = _validate_table_name(table_name)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT COLUMN_NAME
           FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = %s
           ORDER BY ORDINAL_POSITION""",
        (table_name,),
    )
    return [row[0] for row in cursor.fetchall()]


def _next_azure_uid(conn, table_name: str) -> int:
    table_name = _validate_table_name(table_name)
    cursor = conn.cursor()
    cursor.execute(f"SELECT MAX([uid]) FROM dbo.[{table_name}] WHERE [uid] >= %s", (UID_START,))
    row = cursor.fetchone()
    return row[0] + 1 if row and row[0] is not None else UID_START


def _db_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)):
        return " | ".join(str(item) for item in value)
    return value


def _upsert_azure_row(conn, table_name: str, table_columns: List[str], row_data: Dict[str, Any]) -> None:
    table_name = _validate_table_name(table_name)
    available = set(table_columns)
    db_data = {
        col: _db_value(row_data[col])
        for col in APARTMENT_DB_COLUMNS
        if col in available and col in row_data
    }
    if not db_data:
        raise RuntimeError(f"No matching columns found for dbo.{table_name}")

    source_url = row_data.get("source_mailbox")
    uid = row_data.get("uid")
    cursor = conn.cursor()
    if source_url:
        cursor.execute(f"SELECT COUNT(*) FROM dbo.[{table_name}] WHERE [source_mailbox] = %s", (source_url,))
    else:
        cursor.execute(f"SELECT COUNT(*) FROM dbo.[{table_name}] WHERE [uid] = %s", (uid,))
    exists = bool(cursor.fetchone()[0])

    if exists:
        update_cols = [col for col in db_data if col != "uid"]
        if update_cols:
            set_clause = ", ".join(f"[{col}] = %s" for col in update_cols)
            values = [db_data[col] for col in update_cols]
            if source_url:
                cursor.execute(
                    f"UPDATE dbo.[{table_name}] SET {set_clause} WHERE [source_mailbox] = %s",
                    tuple(values + [source_url]),
                )
            else:
                cursor.execute(
                    f"UPDATE dbo.[{table_name}] SET {set_clause} WHERE [uid] = %s",
                    tuple(values + [uid]),
                )
    else:
        insert_cols = list(db_data.keys())
        col_list = ", ".join(f"[{col}]" for col in insert_cols)
        placeholders = ", ".join(["%s"] * len(insert_cols))
        cursor.execute(
            f"INSERT INTO dbo.[{table_name}] ({col_list}) VALUES ({placeholders})",
            tuple(db_data[col] for col in insert_cols),
        )
    if table_name == "apartment_deals":
        try:
            cursor.execute(
                """
                INSERT INTO dbo.apartment_deals_score_history
                    (apartment_deal_id, deal_name, score, total_score, score_breakdown)
                SELECT apartment_deal_id, deal_name, score, total_score, score_breakdown
                FROM dbo.[apartment_deals]
                WHERE [source_mailbox] = %s
                """,
                (source_url,),
            )
        except Exception as hist_err:
            print(f"    WARNING score history insert failed: {hist_err}", flush=True)
    conn.commit()
    return True


class _RawCDPLocator:
    def __init__(self, page, selector: str):
        self.page = page
        self.selector = selector

    def click(self, timeout: int = 3000) -> None:
        self.page.evaluate(
            f"""() => {{
                const el = document.querySelector({json.dumps(self.selector)});
                if (el) el.click();
            }}"""
        )


class _RawCDPKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key: str) -> None:
        if key.lower() in {"meta+a", "cmd+a"}:
            for event_type in ("rawKeyDown", "keyUp"):
                self.page._send("Input.dispatchKeyEvent", {
                    "type": event_type,
                    "key": "a",
                    "code": "KeyA",
                    "windowsVirtualKeyCode": 65,
                    "nativeVirtualKeyCode": 0,
                    "modifiers": 4,
                })
            return
        self.page._send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        self.page._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})


class _RawCDPPage:
    def __init__(self, ws_url: str):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self._message_id = 0
        self.keyboard = _RawCDPKeyboard(self)
        self._send("Page.enable")
        self._send("Runtime.enable")

    def _send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._message_id += 1
        message_id = self._message_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            response = json.loads(self.ws.recv())
            if response.get("id") != message_id:
                continue
            if "error" in response:
                raise RuntimeError(f"CDP {method} failed: {response['error']}")
            return response.get("result") or {}

    def goto(self, url: str, timeout: int = 30000, wait_until: Optional[str] = None) -> None:
        self._send("Page.navigate", {"url": url})
        time.sleep(0.75)

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        expr = expression.strip()
        if re.match(r"^\([^()]*\)\s*=>", expr, flags=re.S):
            call = f"({json.dumps(arg)})" if arg is not None else "()"
            expr = f"({expr}){call}"
        result = self._send("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": True,
            "returnByValue": True,
        })
        remote = result.get("result") or {}
        if remote.get("subtype") == "error":
            raise RuntimeError(remote.get("description") or "CDP evaluation failed")
        return remote.get("value")

    def locator(self, selector: str) -> _RawCDPLocator:
        return _RawCDPLocator(self, selector)

    def bring_to_front(self) -> None:
        self._send("Page.bringToFront")

    def close(self) -> None:
        try:
            self._send("Page.close")
        except Exception:
            pass
        try:
            self.ws.close()
        except Exception:
            pass


class _RawCDPContext:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url.rstrip("/")
        self.pages: List[_RawCDPPage] = []
        self._load_pages()

    def _json_request(self, path: str, method: str = "GET") -> Any:
        request = urllib.request.Request(f"{self.cdp_url}{path}", method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def _load_pages(self) -> None:
        for target in self._json_request("/json/list"):
            if target.get("type") != "page":
                continue
            ws_url = target.get("webSocketDebuggerUrl")
            if ws_url:
                try:
                    self.pages.append(_RawCDPPage(ws_url))
                except Exception:
                    continue

    def new_page(self) -> _RawCDPPage:
        target = self._json_request("/json/new?about:blank", method="PUT")
        page = _RawCDPPage(target["webSocketDebuggerUrl"])
        self.pages.append(page)
        return page


class _RawCDPBrowser:
    def __init__(self, cdp_url: str, process: Optional[subprocess.Popen] = None):
        self.cdp_url = cdp_url.rstrip("/")
        self.process = process
        self.contexts = [_RawCDPContext(self.cdp_url)]

    def new_context(self, viewport: Optional[Dict[str, int]] = None) -> _RawCDPContext:
        return self.contexts[0]

    def close(self) -> None:
        for context in self.contexts:
            for page in list(context.pages):
                try:
                    page.ws.close()
                except Exception:
                    pass
        if self.process is not None:
            self.process.terminate()


def _chrome_executable() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("No Chrome-compatible browser found for --no-playwright mode")


def _cdp_available(cdp_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def _open_raw_cdp_browser(cdp_url: Optional[str], start_url: str) -> _RawCDPBrowser:
    cdp_url = (cdp_url or "http://127.0.0.1:18803").rstrip("/")
    if _cdp_available(cdp_url):
        return _RawCDPBrowser(cdp_url)
    port_match = re.search(r":(\d+)$", cdp_url)
    if not port_match:
        raise ValueError(f"Cannot determine CDP port from {cdp_url}")
    profile_dir = Path.home() / "Library" / "Application Support" / "DealSifta" / "apartments-chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        _chrome_executable(),
        f"--remote-debugging-port={port_match.group(1)}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--disable-session-crashed-bubble",
        start_url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if _cdp_available(cdp_url):
            return _RawCDPBrowser(cdp_url, process=process)
        time.sleep(0.25)
    process.terminate()
    raise RuntimeError(f"Chrome did not expose CDP at {cdp_url}")


class _RawCDPChromiumShim:
    def __init__(self, start_url: str):
        self.start_url = start_url

    def launch(self, headless: bool = False) -> _RawCDPBrowser:
        return _open_raw_cdp_browser(None, self.start_url)

    def connect_over_cdp(self, cdp_url: str) -> _RawCDPBrowser:
        return _RawCDPBrowser(cdp_url)


class _RawCDPPlaywrightShim:
    def __init__(self, start_url: str):
        self.chromium = _RawCDPChromiumShim(start_url)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Apartments.com listings into the DealSifta workbook and Azure SQL")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--urls-file", default=None, help="File with one detail-page URL per line; skips search-page crawling")
    parser.add_argument("--names-file", default=None, help="File with one property name per line; only search cards matching these names are crawled")
    parser.add_argument("--include-no-1br", action="store_true", help="Write rows even when no 1BR is currently available (rent left blank)")
    parser.add_argument("--workbook", default=DEFAULT_WORKBOOK)
    parser.add_argument("--sheet", default=DEFAULT_SHEET)
    parser.add_argument("--county", default=DEFAULT_COUNTY)
    parser.add_argument("--min-units", type=int, default=MIN_UNITS)
    parser.add_argument("--max", type=int, default=20)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-playwright", action="store_true")
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--azure-table", default="apartment_deals")
    parser.add_argument("--no-azure", action="store_true")
    parser.add_argument("--debug-schools", action="store_true")
    parser.add_argument("--property-site", action="store_true", help="Follow the apartments.com management-company link and scrape the property's own website for about/neighborhood narrative text")
    parser.add_argument("--city-only", default=None, help="Only write rows whose parsed city matches this exactly (e.g. 'Boise')")
    parser.add_argument("--name-suffix", default=None, help="Append this suffix to deal_name on written rows (e.g. ',Boise, ID')")
    parser.add_argument("--manual-units", type=int, default=None, help="Override units when the page does not list them")
    parser.add_argument("--manual-vintage", type=int, default=None, help="Override vintage/year built when the page does not list it")
    parser.add_argument("--manual-rent", type=float, default=None, help="Override 1BR monthly rent")
    parser.add_argument("--manual-size", type=float, default=None, help="Override 1BR square footage")
    args = parser.parse_args()

    if args.no_playwright:
        def sync_playwright():
            return _RawCDPPlaywrightShim(args.url)
    else:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SystemExit("Playwright not installed. Run: pip install -r requirements.txt && playwright install")

    if not Path(args.workbook).exists():
        raise SystemExit(f"Workbook not found: {args.workbook}")

    wb, ws = _open_workbook(args.workbook, args.sheet)
    header_map = _build_header_map(ws)
    _ensure_header_columns(ws, header_map, APARTMENT_EXCEL_EXTRA_COLUMNS)
    next_uid = _next_uid(ws, header_map)

    azure_conn = None
    azure_columns: List[str] = []
    if not args.no_azure and not args.dry_run:
        try:
            azure_conn = _get_azure_conn()
            azure_columns = _azure_table_columns(azure_conn, args.azure_table)
            if not azure_columns:
                raise RuntimeError(f"Azure SQL table not found or has no columns: dbo.{args.azure_table}")
            next_uid = max(next_uid, _next_azure_uid(azure_conn, args.azure_table))
            print(f"Azure SQL upsert enabled: dbo.{args.azure_table}", flush=True)
        except Exception as exc:
            print(f"Azure SQL disabled: {exc}", flush=True)
            if azure_conn is not None:
                azure_conn.close()
            azure_conn = None
            azure_columns = []

    with sync_playwright() as p:
        close_browser = False
        if args.cdp_url:
            browser = p.chromium.connect_over_cdp(args.cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1600, "height": 1200})
            page = None
            target_url = args.url.split("#")[0].rstrip("/")
            for existing in getattr(context, "pages", []):
                try:
                    existing_url = _norm_text(existing.evaluate("() => location.href")).split("#")[0].rstrip("/")
                except Exception:
                    continue
                if existing_url == target_url:
                    page = existing
                    break
            if page is None:
                page = context.new_page()
                page.goto(args.url, timeout=30000, wait_until="domcontentloaded")
            try:
                page.bring_to_front()
            except Exception:
                pass
            time.sleep(8)
        else:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context(viewport={"width": 1600, "height": 1200})
            page = context.new_page()
            close_browser = True
            page.goto(args.url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(5)
        if args.debug_schools:
            print(json.dumps(_inspect_school_dom(page), indent=2), flush=True)
            if close_browser:
                browser.close()
            if azure_conn is not None:
                azure_conn.close()
            return
        if args.urls_file:
            url_lines = [
                line.strip()
                for line in Path(args.urls_file).read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            listings = [{"url": u, "text": "", "name": ""} for u in url_lines]
        elif _is_property_url(args.url):
            listings = [{"url": args.url, "text": "", "name": ""}]
        else:
            listings = []
            seen_listing_urls = set()
            target = args.skip + args.max
            page_num = 1
            while len(listings) < target and page_num <= 10:
                if page_num > 1:
                    base_url = re.sub(r"/\d+/?(?=\?)", "", args.url)
                    paged_url = re.sub(r"/?\?", f"/{page_num}/?", base_url, count=1)
                    print(f"    loading search page {page_num}: {paged_url}", flush=True)
                    page.goto(paged_url, timeout=60000, wait_until="domcontentloaded")
                    time.sleep(3.0)
                before = len(listings)
                for item in _collect_listing_cards(page):
                    u = item.get("url") or ""
                    if _is_property_url(u) and u not in seen_listing_urls:
                        seen_listing_urls.add(u)
                        listings.append(item)
                if len(listings) == before:
                    break
                page_num += 1
        if not listings:
            try:
                diag = page.evaluate(
                    """() => {
                        const imgs = Array.from(document.querySelectorAll('img[alt="Property Management Company Logo"]'));
                        const logo_found = imgs.length > 0;
                        const logo_srcs = imgs
                            .map((img) => img.currentSrc || img.src || img.getAttribute('data-src') || '')
                            .filter(Boolean);
                        const anchors = Array.from(document.querySelectorAll('a[href]'));
                        return {
                            title: document.title,
                            url: location.href,
                            readyState: document.readyState,
                            bodyLen: (document.body && document.body.innerText || '').length,
                            anchorCount: anchors.length,
                            sample: anchors.slice(0, 12).map(a => a.href),
                            logo_found,
                            logo_srcs
                        };
                    }"""
                )
                print(f"diag: {json.dumps(diag)[:1200]}", flush=True)
            except Exception as exc:
                print(f"diag failed: {exc}", flush=True)
            if close_browser:
                browser.close()
            if azure_conn is not None:
                azure_conn.close()
            raise SystemExit("No listing URLs found on page")
        if args.names_file:
            wanted = {
                line.strip().lower()
                for line in Path(args.names_file).read_text().splitlines()
                if line.strip()
            }
            def _card_matches(item: Dict[str, Any]) -> bool:
                card_text = f"{_norm_text(item.get('name'))} {_norm_text(item.get('text'))}".lower()
                return any(w in card_text for w in wanted)
            before_ct = len(listings)
            listings = [item for item in listings if _card_matches(item)]
            print(f"names filter: {before_ct} -> {len(listings)} listings", flush=True)
        sliced = listings[args.skip:args.skip + args.max]
        print(f"Found {len(listings)} listings, processing {len(sliced)}", flush=True)
        written = 0
        azure_written = 0
        skipped = 0
        for idx, listing in enumerate(sliced, start=1):
            url = listing.get("url")
            if not url:
                skipped += 1
                continue
            if "-unit-" in url.lower():
                print(f"    skip unit-listing url: {url}", flush=True)
                skipped += 1
                continue
            detail_page = None
            try:
                print(f"[{idx}/{len(sliced)}] {url}", flush=True)
                if idx > 1:
                    time.sleep(random.uniform(8, 20))
                detail_page = context.new_page()
                detail = _scrape_detail(detail_page, url)
                page_title = _norm_text(detail.get("title"))
                if page_title.lower() == "access denied":
                    print("    BLOCKED: Access Denied - rotate VPN/IP and resume with --skip", flush=True)
                    if detail_page is not None:
                        detail_page.close()
                    break
                if not _is_property_url(_norm_text(detail.get("url"))):
                    print(f"    skip non-property url={detail.get('url')}", flush=True)
                    skipped += 1
                    continue
                if len(str(detail.get("raw_text") or "")) < 500:
                    print(f"    BLOCKED/thin page ({len(str(detail.get('raw_text') or ''))}ch) - bot wall, stopping. Resume with --skip {args.skip + idx}", flush=True)
                    break
                fields = _parse_property(detail, args.county)
                if args.manual_units is not None:
                    fields["units"] = args.manual_units
                if args.manual_vintage is not None:
                    fields["vintage"] = args.manual_vintage
                if args.manual_rent is not None:
                    fields["avg_rent_monthly"] = args.manual_rent
                    fields["rent_units"] = "monthly_per_unit"
                if args.manual_size is not None:
                    fields["unit_size"] = args.manual_size
                if fields.get("avg_rent_monthly") and fields.get("unit_size"):
                    fields["rent_psf"] = round(fields["avg_rent_monthly"] / fields["unit_size"], 2)
                if any(getattr(args, a) is not None for a in ["manual_units", "manual_vintage", "manual_rent", "manual_size"]):
                    fields.update(_compute_apartment_score(fields))
                if not _detail_looks_consistent(url, fields):
                    print(f"    skip inconsistent detail for url={url}", flush=True)
                    skipped += 1
                    continue
                if args.city_only:
                    parsed_city = _norm_text(fields.get("city")).lower()
                    if parsed_city != args.city_only.strip().lower():
                        print(f"    skip outside {args.city_only}: {fields.get('deal_name')} | city={fields.get('city')}", flush=True)
                        skipped += 1
                        continue
                if fields.get("_no_available_1br") and not args.include_no_1br:
                    print(f"    skip no available 1BR: {fields.get('deal_name')}", flush=True)
                    skipped += 1
                    continue
                combined_text = " ".join(_norm_text(fields.get(key)) for key in ["notes_raw", "_full_text", "deal_name"])
                preclusion_text = re.split(r"nearby properties", combined_text, flags=re.I)[0]
                affordable_match = _AFFORDABLE_SENIOR_RE.search(preclusion_text)
                if affordable_match:
                    start = max(0, affordable_match.start() - 100)
                    evidence = combined_text[start:affordable_match.end() + 100]
                    print(f"    skip affordable/senior community: {fields.get('deal_name')} | evidence: ...{evidence}...", flush=True)
                    skipped += 1
                    continue
                if _is_individual_unit_listing(_norm_text(fields.get("address"))):
                    print(f"    skip individual unit listing: {fields.get('address')}", flush=True)
                    skipped += 1
                    continue
                if args.county.lower().startswith("san mateo"):
                    city = _norm_text(fields.get("city")).lower()
                    if city and city not in SM_COUNTY_CITIES:
                        print(f"    skip outside San Mateo County: {fields.get('deal_name')} | city={city}", flush=True)
                        skipped += 1
                        continue
                units = _safe_int(fields.get("units"))
                if units is None or units < args.min_units:
                    print(f"    skip units={units}", flush=True)
                    skipped += 1
                    continue
                if args.property_site:
                    mgmt_link = _norm_text((detail.get("mgmt") or {}).get("link"))
                    if mgmt_link and (urlparse(mgmt_link).hostname or "").lower() in {"google.com", "www.google.com", "maps.google.com"}:
                        mgmt_link = ""
                    if mgmt_link:
                        psite = _scrape_property_site(context, mgmt_link)
                        if psite.get("text"):
                            detail["property_site"] = psite
                            print(f"    property site: {psite['url']} | {len(psite['text'])}ch from {len(psite['pages'])} page(s)", flush=True)
                        else:
                            print(f"    property site thin/unreachable: {mgmt_link}", flush=True)
                    else:
                        print("    no property-site link on page", flush=True)
                    if detail.get("property_site"):
                        fields = _parse_property(detail, args.county)
                psite = detail.get("property_site") or {}
                if psite.get("text"):
                    try:
                        PROPERTY_SITE_TEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
                        site_record = {
                            "deal_name": fields.get("deal_name"),
                            "address": fields.get("address"),
                            "city": fields.get("city"),
                            "state": fields.get("state"),
                            "county": fields.get("county"),
                            "neighborhood": fields.get("neighborhood"),
                            "apartments_url": url,
                            "property_site_url": psite.get("url"),
                            "captured_at": datetime.utcnow().isoformat(),
                            "apartments_text": detail.get("text"),
                            "pages": psite.get("pages"),
                        }
                        with PROPERTY_SITE_TEXT_PATH.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps(site_record, ensure_ascii=False) + "\n")
                        _write_property_site_md(site_record)
                    except Exception as exc:
                        print(f"    property-site log write failed: {exc}", flush=True)
                inserted_at = datetime.utcnow()
                row_data = {
                    "uid": next_uid,
                    "is_residential": 1 if fields.get("is_residential") else 0,
                    "residential_gated": 1 if fields.get("residential_gated") else 0,
                    "residential_gating_reason": fields.get("residential_gating_reason"),
                    "score": fields.get("score"),
                    "score_breakdown": fields.get("score_breakdown"),
                    "notes_score_reason": fields.get("notes_score_reason"),
                    "notes_score_reason_structured": fields.get("notes_score_reason"),
                    "notes_score_reason_text": fields.get("notes_score_reason"),
                    "score_missing_inputs": fields.get("score_missing_inputs"),
                    "deal_name": fields.get("deal_name"),
                    "address": fields.get("address"),
                    "city": fields.get("city"),
                    "state": fields.get("state"),
                    "county": fields.get("county"),
                    "neighborhood": fields.get("neighborhood"),
                    "msa": fields.get("msa"),
                    "product": fields.get("product"),
                    "asset_class": fields.get("asset_class"),
                    "transaction_type": fields.get("transaction_type"),
                    "units": fields.get("units"),
                    "vintage": fields.get("vintage"),
                    "renovation_year": fields.get("renovation_year"),
                    "renovated": 1 if fields.get("renovated") else 0,
                    "example_rent_one_bed": fields.get("avg_rent_monthly"),
                    "rent_psf": fields.get("rent_psf"),
                    "rent_units": fields.get("rent_units"),
                    "example_one_bed_unit_size": fields.get("unit_size"),
                    "notes": fields.get("notes"),
                    "notes_raw": fields.get("notes_raw"),
                    "property_site_url": fields.get("property_site_url"),
                    "property_site_text": fields.get("property_site_text"),
                    "signal_evidence": fields.get("signal_evidence"),
                    "email_subject": fields.get("email_subject"),
                    "email_from": fields.get("email_from"),
                    "source_mailbox": fields.get("source_mailbox"),
                    "neighborhood notes": fields.get("neighborhood notes"),
                    "assigned schools": fields.get("assigned schools"),
                    "transportation notes": fields.get("transportation notes"),
                    "neighborhood_notes": fields.get("neighborhood notes"),
                    "assigned_schools": fields.get("assigned schools"),
                    "transportation_notes": fields.get("transportation notes"),
                    "inserted_at": inserted_at,
                    "score_component_vector": fields.get("score_component_vector"),
                    "geo_score": fields.get("geo_score"),
                    "units_score": fields.get("units_score"),
                    "price_score": fields.get("price_score"),
                    "rent_score": fields.get("rent_score"),
                    "vintage_score": fields.get("vintage_score"),
                    "reno_boost_score": fields.get("reno_boost_score"),
                    "returns_score": fields.get("returns_score"),
                    "strategy_score": fields.get("strategy_score"),
                    "metro_score": fields.get("metro_score"),
                    "parking_score": fields.get("parking_score"),
                    "notes_boost_score": fields.get("notes_boost_score"),
                    "total_score": fields.get("total_score"),
                    "qualitative_signal_evidence": fields.get("qualitative_signal_evidence"),
                    "negative_signal_evidence": fields.get("negative_signal_evidence"),
                    "qualitative_signal_source": fields.get("qualitative_signal_source"),
                    "qualitative_boost_total": fields.get("qualitative_boost_total"),
                    "source_url": fields.get("source_mailbox"),
                }
                for signal_col in APARTMENT_SIGNAL_COLUMNS:
                    sig_val = fields.get(signal_col)
                    row_data[signal_col] = sig_val if isinstance(sig_val, float) else (1 if sig_val else 0)
                _upsert_row(ws, header_map, row_data)
                if azure_conn is not None:
                    try:
                        azure_ok = _upsert_azure_row(azure_conn, args.azure_table, azure_columns, row_data)
                        azure_written += 1 if azure_ok else 0
                    except Exception as exc:
                        print(f"    azure failed: {exc}", flush=True)
                mgmt_dbg = detail.get("mgmt") or {}
                print(
                    f"    ok {fields.get('deal_name')} | units={fields.get('units')} | rent={fields.get('avg_rent_monthly')} "
                    f"| strategy={fields.get('investment_strategy')} | metro={fields.get('metro_score')} | score={fields.get('score')} "
                    f"| mgmt_logo={bool(mgmt_dbg.get('logo_found'))} mgmt_src={_norm_text(mgmt_dbg.get('logo_src'))[:60] or '-'} "
                    f"mgmt_link={_norm_text(mgmt_dbg.get('link'))[:80] or '-'} "
                    f"| no_inst={fields.get('signal_no_institutional_mgmt')}",
                    flush=True,
                )
                next_uid += 1
                written += 1
            except Exception as exc:
                print(f"    failed: {exc}", flush=True)
                skipped += 1
            finally:
                if detail_page is not None:
                    detail_page.close()
        if close_browser:
            browser.close()
    if azure_conn is not None:
        azure_conn.close()
    if not args.dry_run:
        wb.save(args.workbook)
    print(f"Done. written={written} azure_written={azure_written} skipped={skipped} dry_run={args.dry_run}", flush=True)


if __name__ == "__main__":
    main()
