"""Parse salary ranges from free text.

Examples handled:
    '£40,000 - £50,000 per annum' -> 40000, 50000, GBP, year
    '$120k-$150k'                 -> 120000, 150000, USD, year
    '£25/hour'                    -> 25, 25, GBP, hour
    '€50.000 – €60.000 brutto'    -> 50000, 60000, EUR, year
    'Up to £60k + bonus'          -> None, 60000, GBP, year
    '£450 - £550 per day'         -> 450, 550, GBP, day
"""
from __future__ import annotations

import re
from typing import Optional

_SYMBOLS = [
    ("US$", "USD"), ("CA$", "CAD"), ("C$", "CAD"), ("A$", "AUD"), ("AU$", "AUD"),
    ("NZ$", "NZD"), ("£", "GBP"), ("€", "EUR"), ("$", "USD"), ("₹", "INR"), ("¥", "JPY"),
]
_CODES = ["GBP", "USD", "EUR", "CAD", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK", "PLN", "INR", "JPY", "SGD"]
_WORDS = {"pounds": "GBP", "sterling": "GBP", "dollars": "USD", "euros": "EUR", "euro": "EUR"}

_PERIODS = [
    (r"per\s+annum|p\.?\s?a\.?\b|annual(?:ly)?|a\s+year|per\s+year|/\s?(?:yr|year|y)\b|\byearly\b|\bper\s+yr", "year"),
    (r"per\s+hour|an\s+hour|hourly|/\s?(?:hr|hour|h)\b|\bp/?h\b|\bph\b", "hour"),
    (r"per\s+day|a\s+day|daily|/\s?(?:day|d)\b|\bp/?d\b|day\s+rate", "day"),
    (r"per\s+week|a\s+week|weekly|/\s?(?:wk|week)\b|\bpw\b", "week"),
    (r"per\s+month|a\s+month|monthly|/\s?(?:mo|month)\b|\bpcm\b", "month"),
]

_NUM = r"(\d{1,3}(?:\.\d{3})+,\d{1,2}(?!\d)|\d{1,3}(?:[,.\s]\d{3})+(?:\.\d+)?|\d+(?:[.,]\d{1,2}(?!\d))?|\d+)\s*([kKmM])?(?![a-zA-Z])"
_NUM_RE = re.compile(_NUM)
_RANGE_SEP = r"\s*(?:-|–|—|to|and)\s*"
_CUR_PREFIX = r"(?:US\$|CA\$|C\$|AU?\$|NZ\$|[£€$₹¥]|(?:GBP|USD|EUR|CAD|AUD|CHF|INR)\s?)"
# Used for locating a salary inside a long job description.
_SALARY_IN_TEXT = re.compile(
    _CUR_PREFIX + r"\s?\d[\d,.\s]*[kK]?(?:" + _RANGE_SEP + r"(?:" + _CUR_PREFIX + r")?\s?\d[\d,.\s]*[kK]?)?"
    r"(?:\s*(?:per\s+\w+|p\.?a\.?|/\s?\w+|pcm|hourly|daily|annually|a\s+year|an\s+hour))?",
    re.I,
)


def _to_number(num: str, suffix: Optional[str]) -> float:
    s = num.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", s):  # European: 5.082,02
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", s):  # European decimal: 12,50
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:[,.\s]\d{3})+", s):
        s = re.sub(r"[,.\s]", "", s)
    elif re.fullmatch(r"\d{1,3}(?:[,\s]\d{3})+\.\d+", s):
        s = re.sub(r"[,\s]", "", s)
    else:
        s = s.replace(",", "")
    v = float(s)
    if suffix:
        v *= 1000 if suffix.lower() == "k" else 1_000_000
    return v


def detect_currency(text: str) -> Optional[str]:
    for sym, code in _SYMBOLS:
        if sym in text:
            return code
    up = text.upper()
    for code in _CODES:
        if re.search(rf"\b{code}\b", up):
            return code
    low = text.lower()
    for w, code in _WORDS.items():
        if w in low:
            return code
    return None


def detect_period(text: str) -> Optional[str]:
    for pat, period in _PERIODS:
        if re.search(pat, text, re.I):
            return period
    return None


def _infer_period(value: float) -> str:
    if value >= 10_000:
        return "year"
    if value >= 1_500:
        return "month"
    if value >= 150:
        return "day"
    return "hour"


def parse_salary(text: Optional[str]) -> Optional[dict]:
    """Parse a salary string. Returns dict with min/max/currency/period/raw or None."""
    if not text or not isinstance(text, str):
        return None
    raw = " ".join(text.split())
    if re.search(r"\b(competitive|negotiable|doe|tbc)\b", raw, re.I) and not re.search(r"\d", raw):
        return None
    # strip things that look like percentages / years of experience / dates
    cleaned = re.sub(r"\d+(?:\.\d+)?\s*%", " ", raw)
    cleaned = re.sub(r"\b(19|20)\d{2}\b(?![,.]\d)", " ", cleaned)
    cleaned = re.sub(r"\d+\+?\s*(?:years?|yrs?|hours?\s+per\s+week|days?\s+per\s+week)\b", " ", cleaned, flags=re.I)

    matches = list(_NUM_RE.finditer(cleaned))
    if not matches:
        return None
    values: list[float] = []
    suffixes: list[Optional[str]] = []
    nums = [m for m in matches if float(re.sub(r"[,.\s]", "", m.group(1)) or 0) > 0]
    if not nums:
        return None
    # Only consider the first one or two numbers (a range).
    first = nums[0]
    values.append(_to_number(first.group(1), first.group(2)))
    suffixes.append(first.group(2))
    if len(nums) > 1:
        between = cleaned[first.end(): nums[1].start()]
        if re.fullmatch(r"\s*(?:" + _CUR_PREFIX + r")?\s*(?:-|–|—|to|and)\s*(?:" + _CUR_PREFIX + r")?\s*", between, re.I) or \
           re.fullmatch(_RANGE_SEP + r"(?:" + _CUR_PREFIX + r")?\s*", between, re.I):
            values.append(_to_number(nums[1].group(1), nums[1].group(2)))
            suffixes.append(nums[1].group(2))
    # '30-35k' -> apply k to the first value as well
    if len(values) == 2 and suffixes[1] and not suffixes[0] and values[0] < 1000 and values[0] * 1000 <= values[1] * 1.5:
        values[0] *= 1000 if suffixes[1].lower() == "k" else 1_000_000

    currency = detect_currency(raw)
    period = detect_period(raw)
    lo = min(values)
    hi = max(values)
    if period is None:
        period = _infer_period(hi)

    if re.search(r"\b(up\s+to|max(?:imum)?|to\s+£|to\s+\$)\b", raw, re.I) and len(values) == 1:
        lo = None
    elif re.search(r"\b(from|starting\s+at|min(?:imum)?|at\s+least)\b", raw, re.I) and len(values) == 1:
        hi = None
    if currency is None and not re.search(r"salary|pay|rate|compensation", raw, re.I) and len(raw) > 60:
        # long text with no currency: too ambiguous
        return None
    return {
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": currency,
        "salary_period": period,
        "salary_raw": raw,
    }


def find_salary_in_text(text: Optional[str]) -> Optional[dict]:
    """Find and parse the first currency-prefixed salary mention in free text."""
    if not text:
        return None
    for m in _SALARY_IN_TEXT.finditer(text):
        snippet = m.group(0).strip().rstrip(",.;: ")
        nxt = text[m.end(): m.end() + 12]
        # "$140B industry", "£2m funding", "$10 million" are not salaries
        if m.group(0)[-1:].isalnum() and re.match(r"[A-Za-z]", nxt) and not re.match(r"(USD|GBP|EUR|CAD|AUD)\b", nxt):
            continue
        if re.match(r"\s*(million|billion|bn|mn|m)\b", nxt, re.I):
            continue
        # need at least a 2-digit number to avoid matching things like '$1 coffee'
        if not re.search(r"\d{2}", snippet):
            continue
        parsed = parse_salary(snippet)
        if not parsed:
            continue
        top = parsed["salary_max"] or parsed["salary_min"] or 0
        if top < 10:
            continue
        if parsed["salary_period"] == "year" and top < 5000:
            continue  # e.g. "€1,200 a year learning budget" - a benefit, not a salary
        return parsed
    return None
