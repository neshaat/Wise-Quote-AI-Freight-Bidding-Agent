"""
Email parser — extracts freight request fields from raw email text.

Pure logic module: no I/O, no Streamlit, no database.

Customer identification strategy (in order of priority):
  1. Sender email domain matched against DOMAIN_CUSTOMER_MAP
  2. CUST-XXXX pattern anywhere in body/subject (internal staff forwarding)
  3. None — operator must fill in manually

Two-pass extraction:
  Pass 1 — labeled-field patterns (semi-structured emails)
  Pass 2 — prose heuristics for origin/destination from natural sentences
"""

import re
from datetime import date, datetime
from typing import Optional


# ── Domain → customer ID map ───────────────────────────────────────────────────
# Maps sender email domains to internal customer IDs.
# In production this would be a DB lookup; here it mirrors the default customers.
DOMAIN_CUSTOMER_MAP: dict = {
    "acmecorp.com":    "CUST-A",
    "betaimports.com": "CUST-B",
    "gammallc.com":    "CUST-C",
    "deltaco.com":     "CUST-D",
}


# ── Demo templates ─────────────────────────────────────────────────────────────
# These are written as real people would write them — no structured fields,
# no mention of customer IDs or markup. The system identifies the customer
# from the sender's email domain.
DEMO_EMAIL_TEMPLATES = [
    {
        "label": "Acme Corp — electronics pallets, Chicago → LA",
        "subject": "Freight quote needed — Chicago, IL to Los Angeles, CA",
        "sender": "maria.kowalski@acmecorp.com",
        "body": (
            "Hi,\n\n"
            "Hope you're well. We need to move a shipment of consumer electronics "
            "from Chicago, IL to Los Angeles, CA. "
            "We've got 4 standard pallets, each about 48\" x 40\", stacked roughly "
            "54 inches high. Total weight is around 4,800 lbs.\n\n"
            "The items are monitors and laptop accessories — all properly boxed and "
            "shrink-wrapped on the pallets. Nothing fragile beyond normal care.\n\n"
            "We're looking to have them picked up on April 14th if possible, "
            "or April 15th at the latest.\n\n"
            "Please let us know the best rate you can do and estimated transit time.\n\n"
            "Thanks,\n"
            "Maria Kowalski\n"
            "Logistics Coordinator — Acme Corp"
        ),
    },
    {
        "label": "Beta Imports — furniture, Miami → New York",
        "subject": "Rate request: Miami to New York",
        "sender": "tom.r@betaimports.com",
        "body": (
            "Hey team,\n\n"
            "Quick one — we need to ship a batch of imported rattan furniture from "
            "Miami, FL up to New York, NY. "
            "About 12 pieces total: sofas, chairs, and a few side tables. "
            "All wrapped and ready to go. Rough weight is about 2,200 lbs, "
            "give or take.\n\n"
            "We're pretty flexible on pickup — anytime from April 16th works for us. "
            "Just need it there within the week if possible.\n\n"
            "Can you get me a number by end of day?\n\n"
            "Cheers,\n"
            "Tom R.\n"
            "Beta Imports"
        ),
    },
    {
        "label": "Gamma LLC — industrial chemicals, Dallas → Seattle (hazmat)",
        "subject": "Hazardous materials shipment quote — Dallas to Seattle",
        "sender": "carlos.m@gammallc.com",
        "body": (
            "Hello,\n\n"
            "We have an urgent shipment going from Dallas, TX to Seattle, WA.\n\n"
            "It's 8 drums of industrial cleaning solvents — UN1219, Class 3 "
            "flammable liquid. Each drum is 55 gallons. Total gross weight is "
            "approximately 3,500 lbs. The drums are on a single pallet, "
            "properly labeled and documented per DOT requirements.\n\n"
            "Pickup would need to be April 12th. Please make sure the carrier "
            "is certified for hazardous materials transport.\n\n"
            "Let me know what you can do.\n\n"
            "Best,\n"
            "Carlos M.\n"
            "Operations — Gamma LLC"
        ),
    },
    {
        "label": "New prospect — no account yet (missing customer)",
        "subject": "Shipping quote request",
        "sender": "jessica@newprospect.com",
        "body": (
            "Hi there,\n\n"
            "I found your contact through a referral. We're a small home goods "
            "company and we're looking for a reliable freight partner.\n\n"
            "We need to move about 1,800 lbs of boxed kitchen products from "
            "Atlanta, GA to Denver, CO. "
            "Pickup date would be around April 17th.\n\n"
            "Could you give us a quote? We might have more regular shipments "
            "if the pricing works out.\n\n"
            "Thanks,\n"
            "Jessica"
        ),
    },
]


# ── Field extraction regexes (Pass 1 — labeled patterns) ──────────────────────

_ORIGIN_RE = re.compile(
    r"(?i)(?:origin|from|pickup\s+(?:location|city|address)|ship(?:ping)?\s+from|"
    r"departing\s+from|out\s+of|leaving\s+from)\s*[:\-]\s*(.+)"
)
_DEST_RE = re.compile(
    r"(?i)(?:destination|to|delivery\s+(?:location|city|address)|ship(?:ping)?\s+to|"
    r"delivering\s+to|going\s+to|headed\s+to)\s*[:\-]\s*(.+)"
)
_WEIGHT_RE = re.compile(
    r"(?i)(?:total\s+)?(?:gross\s+)?weight\s*(?:is\s+)?(?:approximately\s+|about\s+|"
    r"around\s+|roughly\s+|~)?\s*[:\-]?\s*([\d,]+(?:\.\d+)?)\s*(lbs?|pounds?|kg|kilograms?)?"
)
# Inline weight: "about 4,800 lbs" / "roughly 2,200 pounds" / "~3500 lbs"
_INLINE_WEIGHT_RE = re.compile(
    r"(?i)(?:about|around|roughly|approximately|~)\s+([\d,]+(?:\.\d+)?)\s*(lbs?|pounds?|kg|kilograms?)"
)
# Bare weight with unit: "4,800 lbs" (only when unit is explicit)
_BARE_WEIGHT_RE = re.compile(
    r"\b([\d,]+(?:\.\d+)?)\s*(lbs?|pounds?)\b"
)
_CARGO_RE = re.compile(
    r"(?i)(?:cargo\s+type|freight\s+type|commodity|goods|items?|products?|shipment\s+(?:is|contains?))\s*[:\-]\s*(.+)"
)
_PICKUP_RE = re.compile(
    r"(?i)(?:pickup\s+date|ship\s+date|ready\s+date|ready\s+on|ship(?:ment)?\s+date|"
    r"picked\s+up\s+on|pickup)\s*[:\-]\s*(.+)"
)
_CUSTOMER_ID_RE = re.compile(r"\bCUST-[A-Z0-9]+\b")
_HAZMAT_RE = re.compile(
    r"(?i)\b(hazmat|hazardous|dangerous\s+goods?|class\s+[0-9]|un\s*\d{4}|flammable|"
    r"corrosive|toxic|explosive|radioactive)\b"
)

# Prose route patterns: "from Dallas, TX to Seattle, WA"
_PROSE_ROUTE_RE = re.compile(
    r"(?i)from\s+([A-Z][a-zA-Z\s]+,\s*[A-Z]{2})\s+to\s+([A-Z][a-zA-Z\s]+,\s*[A-Z]{2})"
)
# Fallback city,state pattern
_LOCATION_RE = re.compile(r"\b([A-Z][a-zA-Z\s]{2,25},\s*[A-Z]{2})\b")

# Prose pickup: flexible — keyword within ~60 chars of a date expression
# Handles: "Pickup would need to be April 12th", "anytime from April 16th", "Pickup date would be around April 17th"
_PROSE_DATE_RE = re.compile(
    r"(?i)(?:pickup|picked\s+up|ship(?:ment)?|ready|pick(?:\s+up)?)"
    r".{0,60}?"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?|"
    r"\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?|\d{4}-\d{2}-\d{2})"
)
# Last-resort: "anytime from April 16th" / "from April 16th"
_ANYTIME_DATE_RE = re.compile(
    r"(?i)(?:anytime\s+from|from|by|before|around|ideally)\s+"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)"
)

_DATE_FMTS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    "%B %dst, %Y", "%B %dnd, %Y", "%B %drd, %Y", "%B %dth, %Y",
    "%d %B %Y", "%d %b %Y",
]


def _clean(s: str) -> str:
    return s.strip().rstrip(".,;:")


def _parse_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    raw = re.sub(r"(?i)^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s*", "", raw)
    # Normalize ordinals: "14th" → "14"
    raw = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", raw)
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    # Month-name scan fallback
    m = re.search(
        r"(?i)(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2}),?\s*(\d{4})?",
        raw,
    )
    if m:
        day, month_abbr = m.group(2), m.group(1)[:3].capitalize()
        year = m.group(3) or str(date.today().year)
        try:
            return datetime.strptime(f"{month_abbr} {day} {year}", "%b %d %Y").date().isoformat()
        except ValueError:
            pass
    return None


def _parse_weight(raw: str, unit: Optional[str]) -> Optional[float]:
    try:
        val = float(raw.replace(",", ""))
        if unit and unit.lower().startswith("kg"):
            val *= 2.20462
        return round(val, 1)
    except ValueError:
        return None


def _extract_domain(email: str) -> str:
    """Return the domain part of an email address, lowercased."""
    m = re.search(r"@([\w.\-]+)", email)
    return m.group(1).lower() if m else ""


# ── Public API ─────────────────────────────────────────────────────────────────

def parse_freight_email(raw_text: str, subject: str = "", sender: str = "") -> dict:
    """
    Extract freight request fields from a human-written email.

    Customer identification (in priority order):
      1. Sender email domain → DOMAIN_CUSTOMER_MAP
      2. CUST-XXXX anywhere in subject/body (internal staff use)
      3. None

    Returns dict with keys: origin, destination, weight_lbs, cargo_type,
    pickup_date, customer_id, hazmat, _parse_confidence, _raw_subject, _raw_sender.
    """
    result = {
        "origin":            None,
        "destination":       None,
        "weight_lbs":        None,
        "cargo_type":        None,
        "pickup_date":       None,
        "customer_id":       None,
        "hazmat":            False,
        "_parse_confidence": 0.0,
        "_raw_subject":      subject,
        "_raw_sender":       sender,
    }

    full_text = f"{subject}\n{raw_text}" if subject else raw_text

    # ── Customer ID ───────────────────────────────────────────────────────────
    # Priority 1: sender email domain
    domain = _extract_domain(sender)
    if domain in DOMAIN_CUSTOMER_MAP:
        result["customer_id"] = DOMAIN_CUSTOMER_MAP[domain]
    else:
        # Priority 2: explicit CUST-XXXX in text (internal staff forwarding)
        m = _CUSTOMER_ID_RE.search(full_text)
        if m:
            result["customer_id"] = m.group(0)

    # ── Hazmat ────────────────────────────────────────────────────────────────
    if _HAZMAT_RE.search(full_text):
        result["hazmat"] = True

    # ── Pass 1: labeled field extraction ─────────────────────────────────────
    m = _ORIGIN_RE.search(full_text)
    if m:
        result["origin"] = _clean(m.group(1))

    m = _DEST_RE.search(full_text)
    if m:
        result["destination"] = _clean(m.group(1))

    # Weight: labeled → inline approx → bare unit
    m = _WEIGHT_RE.search(full_text)
    if m:
        result["weight_lbs"] = _parse_weight(m.group(1), m.group(2))
    if result["weight_lbs"] is None:
        m = _INLINE_WEIGHT_RE.search(full_text)
        if m:
            result["weight_lbs"] = _parse_weight(m.group(1), m.group(2))
    if result["weight_lbs"] is None:
        # Last resort: find bare "4,800 lbs" — take the largest value (likely total)
        bare_matches = _BARE_WEIGHT_RE.findall(full_text)
        if bare_matches:
            vals = [_parse_weight(r, u) for r, u in bare_matches if _parse_weight(r, u)]
            if vals:
                result["weight_lbs"] = max(vals)

    m = _CARGO_RE.search(full_text)
    if m:
        result["cargo_type"] = _clean(m.group(1))

    # Pickup date: labeled → prose near keyword → anytime/from phrase
    m = _PICKUP_RE.search(full_text)
    if m:
        result["pickup_date"] = _parse_date(_clean(m.group(1)))
    if result["pickup_date"] is None:
        m = _PROSE_DATE_RE.search(full_text)
        if m:
            result["pickup_date"] = _parse_date(m.group(1))
    if result["pickup_date"] is None:
        m = _ANYTIME_DATE_RE.search(full_text)
        if m:
            result["pickup_date"] = _parse_date(m.group(1))

    # ── Pass 2: prose route extraction (only if origin/dest still missing) ───
    if not result["origin"] or not result["destination"]:
        m = _PROSE_ROUTE_RE.search(full_text)
        if m:
            if not result["origin"]:
                result["origin"] = _clean(m.group(1))
            if not result["destination"]:
                result["destination"] = _clean(m.group(2))

    # Final fallback: generic city, ST patterns
    if not result["origin"] or not result["destination"]:
        skip = {"Hi", "Hey", "Dear", "Hello", "Thanks", "Best", "Cheers"}
        locs = [
            loc for loc in _LOCATION_RE.findall(full_text)
            if loc.split(",")[0].strip() not in skip
        ]
        if locs and not result["origin"]:
            result["origin"] = locs[0]
        if len(locs) > 1 and not result["destination"]:
            result["destination"] = locs[1]

    # ── Confidence score ──────────────────────────────────────────────────────
    key_fields = ["origin", "destination", "weight_lbs", "customer_id", "pickup_date"]
    found = sum(1 for f in key_fields if result[f] is not None)
    result["_parse_confidence"] = round(found / len(key_fields), 2)

    return result


def validate_parsed_request(parsed: dict) -> tuple:
    """
    Returns (is_valid, missing_required_fields).
    Required: origin, destination, customer_id.
    """
    required = ["origin", "destination", "customer_id"]
    missing = [f for f in required if not parsed.get(f)]
    return (len(missing) == 0, missing)


def normalize_freight_request(parsed: dict) -> dict:
    """
    Apply defaults and produce a clean freight_request dict for run_agent().
    Stamps _source='email'. Raises ValueError if required fields are missing.
    """
    is_valid, missing = validate_parsed_request(parsed)
    if not is_valid:
        raise ValueError(f"Cannot process email — missing required fields: {', '.join(missing)}")

    return {
        "origin":       parsed["origin"],
        "destination":  parsed["destination"],
        "weight_lbs":   parsed["weight_lbs"] if parsed["weight_lbs"] else 1000.0,
        "cargo_type":   parsed["cargo_type"] if parsed["cargo_type"] else "General Merchandise",
        "pickup_date":  parsed["pickup_date"] if parsed["pickup_date"] else date.today().isoformat(),
        "customer_id":  parsed["customer_id"],
        "hazmat":       parsed.get("hazmat", False),
        "_source":      "email",
        "_raw_subject": parsed.get("_raw_subject", ""),
        "_raw_sender":  parsed.get("_raw_sender", ""),
    }
