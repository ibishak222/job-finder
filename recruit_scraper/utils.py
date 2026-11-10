from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from typing import Any, Optional

from bs4 import BeautifulSoup

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere)\b", re.I)


_MOJIBAKE_RE = re.compile("[\u00c2\u00c3\u00e2][\u0080-\u00bf\u20ac\u201a-\u2122]")


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1/CP1252 upstream (e.g. 'â€™' -> '’')."""
    if not text or not _MOJIBAKE_RE.search(text):
        return text
    for enc in ("latin-1", "cp1252"):
        try:
            return text.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return text


def html_to_text(value: Optional[str]) -> str:
    """Convert (possibly entity-escaped) HTML to readable plain text."""
    if not value:
        return ""
    value = fix_mojibake(value)
    raw = _html.unescape(value) if "&lt;" in value else value
    if "<" not in raw:
        return re.sub(r"[ \t\xa0]+", " ", _html.unescape(raw)).strip()
    soup = BeautifulSoup(raw, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        tag.append("\n")
    text = soup.get_text()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def to_iso_date(value: Any) -> Optional[str]:
    """Normalise epoch seconds/ms or a date string to an ISO date (YYYY-MM-DD)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            if v > 1e11:  # milliseconds
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
        s = str(value).strip()
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%a, %d %b %Y %H:%M:%S %Z", "%B %d, %Y", "%d %B %Y"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        return _dateparser(s)
    except Exception:
        return None


def _dateparser(s: str) -> Optional[str]:
    """Fallback for human dates like 'Posted 3 days ago', 'yesterday', '12. März 2026' (scrapinghub/dateparser)."""
    try:
        import dateparser
    except ImportError:
        return None
    s = re.sub(r"^(posted|published|date posted|veröffentlicht|publié)\s*:?\s*", "", s.strip(), flags=re.I)
    d = dateparser.parse(s, settings={"PREFER_DATES_FROM": "past", "DATE_ORDER": "DMY",
                                      "RETURN_AS_TIMEZONE_AWARE": False})
    return d.date().isoformat() if d else None


def detect_remote(*texts: Optional[str]) -> Optional[bool]:
    for t in texts:
        if t and _REMOTE_RE.search(t):
            return True
    return None


_BAD_EMAIL = re.compile(r"(\.(png|jpe?g|gif|svg|webp|css|js)$|^(no-?reply|donotreply|example|test|privacy|dpo|"
                        r"dataprotection|gdpr|accessibility|accommodations?|abuse|security|legal|unsubscribe)@|"
                        # template placeholders, not real addresses
                        r"^(first(name)?[._-]?(last|sur)(name)?|f(irst)?\.?surname|your[._-]?(name|email)|name|you|user(name)?|"
                        r"someone|email|john[._-]?doe|jane[._-]?doe)@|"
                        r"@(example\.(com|org)|sentry\.io|wixpress\.com|domain\.com|email\.com)$)", re.I)


def find_emails(text: Optional[str]) -> list[str]:
    """All plausible contact emails published in a text (skips no-reply/privacy/image-like addresses)."""
    out: list[str] = []
    for m in _EMAIL_RE.finditer(text or ""):
        e = m.group(0).strip(".").lower()
        if not _BAD_EMAIL.search(e) and e not in out:
            out.append(e)
    return out


def find_email(text: Optional[str]) -> Optional[str]:
    """Return a contact email only if it is published in the job posting text."""
    found = find_emails(text)
    return found[0] if found else None


# UK numbers (+44 / 0xxxx) and generic international (+1 …); requires a phone-ish context or + prefix
_PHONE_RE = re.compile(r"(?:(?:tel|phone|call|mobile|contact)[^0-9+]{0,15})?(\+44\s?\(?0?\)?\s?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}"
                       r"|\b0\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}\b|\+\d{1,3}[\s-]?\(?\d{1,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4})", re.I)


def find_phone(text: Optional[str]) -> Optional[str]:
    """A phone number only when published with a phone context ('call', 'tel', …) or in +international form."""
    for m in _PHONE_RE.finditer(text or ""):
        num = m.group(1).strip()
        digits = re.sub(r"\D", "", num)
        ctx = (text or "")[max(0, m.start() - 25): m.start(1)].lower()
        if not 10 <= len(digits) <= 13:
            continue
        if num.startswith("+") or re.search(r"tel|phone|call|mobile|contact|ring", ctx):
            return num
    return None


_EMP_MAP = {
    "fulltime": "full-time", "full time": "full-time", "vollzeit": "full-time",
    "parttime": "part-time", "part time": "part-time", "teilzeit": "part-time",
    "contract": "contract", "contractor": "contract", "freelance": "contract", "freiberuflich": "contract",
    "temporary": "temporary", "temp": "temporary", "fixed term": "fixed-term", "befristeter arbeitsvertrag": "fixed-term",
    "permanent": "permanent", "unbefristeter arbeitsvertrag": "permanent", "permanent employment": "permanent",
    "intern": "internship", "internship": "internship", "praktikum": "internship",
    "locum": "locum", "bank": "bank", "agency": "agency", "sessional": "sessional", "ad hoc": "temporary",
    "flexi pool": "bank", "flexipool": "bank", "secondment": "fixed-term", "zero hours": "temporary",
    "werkstudent": "working student", "ausbildung": "apprenticeship", "apprenticeship": "apprenticeship",
}


def normalise_employment_type(value: Any) -> Optional[str]:
    """Normalise employment type(s) to a comma-separated list like 'full-time, permanent'."""
    if not value:
        return None
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,/;]", str(value))
    out: list[str] = []
    for item in items:
        s = re.sub(r"[_\-]+", " ", str(item)).strip().lower()
        s = re.sub(r"\s+", " ", s)
        if not s:
            continue
        found = [v for k, v in _EMP_MAP.items() if re.search(rf"\b{re.escape(k)}\b", s)]
        if not found and s.replace(" ", "") in _EMP_MAP:
            found = [_EMP_MAP[s.replace(" ", "")]]
        for f in found or [s]:
            if f not in out:
                out.append(f)
    return ", ".join(out) or None


TEMP_TYPES = {"locum", "bank", "agency", "temporary", "contract", "fixed-term", "freelance", "sessional"}
_AD_TEMP = re.compile(r"\b(locum|sessional|ad[- ]hoc (?:sessions?|shifts?|basis)|bank (?:staff|nurse|shifts?|worker|hca|role|position)|agency (?:nurse|staff|worker)|"
                      r"temporary (?:role|position|contract|post)|fixed[- ]term|contract role|(?:\d+|six|three|twelve)"
                      r"[- ](?:month|week) contract|freelance)\b", re.I)
_AD_PERM = re.compile(r"\b(permanent (?:role|position|post|contract|basis|job)|this is a permanent)\b", re.I)


def detect_employment_type(title: str, description: str = "") -> Optional[str]:
    """Contract type ONLY where the ad says so: 'locum'/'bank'/'agency'/'temporary'/'fixed-term' or 'permanent'."""
    t = title or ""
    m = re.search(r"\b(locum|bank|agency|temporary|temp|fixed[- ]term|contract|freelance|permanent|perm)\b", t, re.I)
    if m:
        return normalise_employment_type(m.group(1).replace("perm", "permanent") if m.group(1).lower() == "perm"
                                          else m.group(1))
    d = (description or "")[:4000]
    m = _AD_TEMP.search(d)
    if m:
        return normalise_employment_type(m.group(1).split()[0] if m.group(1).split()[0].lower() in
                                         ("locum", "bank", "agency", "temporary", "freelance") else
                                         ("fixed-term" if "fixed" in m.group(1).lower() else "contract"))
    if _AD_PERM.search(d):
        return "permanent"
    return None


def contract_bucket(employment_type: Optional[str]) -> str:
    """'permanent', 'temporary' (locum / bank / agency / temp / contract / fixed-term / freelance) or ''."""
    parts = {p.strip() for p in (employment_type if isinstance(employment_type, str) else "").lower().split(",")}
    if parts & TEMP_TYPES:
        return "temporary"
    if "permanent" in parts:
        return "permanent"
    return ""
