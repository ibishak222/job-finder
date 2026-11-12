"""Look and feel for Job Finder: one stylesheet plus small HTML helpers for cards, badges and link buttons.

Everything here is plain Python (no Streamlit calls except inject_css) so it can be unit-tested. Every value that comes
from a scraped page is HTML-escaped, and links are only built from http(s) URLs, mailto: and tel: (never javascript:).
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any, Iterable, Optional

BRAND = "#0f766e"

CSS = """
<style>
:root { --jf-brand:#0f766e; --jf-brand-dark:#115e59; --jf-ink:#0f172a; --jf-muted:#64748b; --jf-line:#e2e8f0;
        --jf-card:#ffffff; --jf-soft:#f1f5f9; --jf-radius:14px; }
.stApp, .stApp button, .stApp input, .stApp textarea, .stApp select {
  font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
[data-testid="stMainBlockContainer"] { max-width: 1180px; padding-top: 4.4rem; padding-bottom: 4rem; }
footer, [data-testid="stDecoration"] { visibility: hidden; height: 0; }
h1 { font-size: 1.7rem !important; letter-spacing: -.02em; font-weight: 750 !important; margin-bottom: .1rem; }
h2, h3 { letter-spacing: -.01em; }
a { text-decoration: none; }
.jf-card a, .jf-card a:hover, a.jf-btn, a.jf-btn:hover { text-decoration: none !important; }
.jf-card .jf-src a { text-decoration: underline !important; }

/* ---- brand bar + page header */
.jf-brand { display:flex; align-items:center; gap:.7rem; margin: 0 0 .6rem 0; }
.jf-logo { width:38px; height:38px; border-radius:11px; display:flex; align-items:center; justify-content:center;
  background:linear-gradient(135deg,#0f766e,#0e7490 60%,#2563eb); color:#fff; font-size:1.25rem;
  box-shadow:0 2px 8px rgba(15,118,110,.35); }
.jf-brand b { font-size:1.15rem; letter-spacing:-.02em; color:var(--jf-ink); }
.jf-brand span { display:block; color:var(--jf-muted); font-size:.82rem; margin-top:-.15rem; }
.jf-hero { background:linear-gradient(120deg,#0f766e 0%,#0e7490 55%,#2563eb 100%); color:#fff; border-radius:18px;
  padding:1.1rem 1.3rem; margin:.2rem 0 1rem 0; box-shadow:0 6px 20px rgba(14,116,144,.25); }
.jf-hero h2 { color:#fff; margin:0 0 .25rem 0; font-size:1.35rem; padding:0; }
.jf-hero p { margin:0; opacity:.93; font-size:.95rem; line-height:1.4; }
.jf-hero .jf-pill { display:inline-block; background:rgba(255,255,255,.18); border-radius:999px; padding:.15rem .7rem;
  margin:.55rem .35rem 0 0; font-size:.78rem; }

/* ---- cards */
.jf-card { background:var(--jf-card); border:1px solid var(--jf-line); border-radius:var(--jf-radius); padding:.85rem 1rem;
  margin:0 0 .7rem 0; box-shadow:0 1px 2px rgba(15,23,42,.04); }
.jf-card.locum { border-left:4px solid #d97706; }
.jf-card.perm { border-left:4px solid #2563eb; }
.jf-card .jf-title { font-size:1.05rem; font-weight:700; line-height:1.3; color:var(--jf-ink); margin:0; }
.jf-card .jf-title a { color:var(--jf-ink); }
.jf-card .jf-title a:hover { color:var(--jf-brand); text-decoration:underline; }
.jf-meta { color:var(--jf-muted); font-size:.88rem; margin:.15rem 0 .35rem 0; }
.jf-line { font-size:.9rem; margin:.2rem 0; color:#1e293b; }
.jf-line a { color:var(--jf-brand); font-weight:600; }
.jf-src { color:var(--jf-muted); font-size:.76rem; margin-top:.4rem; line-height:1.35; word-break:break-word; }
.jf-src a { color:var(--jf-muted); text-decoration:underline; }
.jf-badges { margin:.3rem 0 .15rem 0; display:flex; flex-wrap:wrap; gap:.3rem; }
.jf-badge { display:inline-block; font-size:.72rem; font-weight:650; padding:.12rem .55rem; border-radius:999px; line-height:1.4;
  background:var(--jf-soft); color:#334155; border:1px solid transparent; white-space:nowrap; }
.jf-badge.locum { background:#fef3c7; color:#92400e; border-color:#fde68a; }
.jf-badge.perm { background:#dbeafe; color:#1e40af; border-color:#bfdbfe; }
.jf-badge.remote { background:#ede9fe; color:#5b21b6; }
.jf-badge.good { background:#dcfce7; color:#166534; border-color:#bbf7d0; }
.jf-badge.warn { background:#fee2e2; color:#991b1b; border-color:#fecaca; }
.jf-badge.info { background:#e0f2fe; color:#075985; }
.jf-badge.neutral { background:#f1f5f9; color:#475569; }
.jf-actions { display:flex; flex-wrap:wrap; gap:.4rem; margin-top:.55rem; }
.jf-btn { display:inline-flex; align-items:center; gap:.3rem; min-height:38px; padding:.3rem .85rem; border-radius:10px;
  border:1px solid var(--jf-line); background:#fff; color:var(--jf-ink) !important; font-size:.86rem; font-weight:600;
  max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.jf-btn:hover { border-color:var(--jf-brand); color:var(--jf-brand) !important; background:#f0fdfa; }
.jf-btn.primary { background:var(--jf-brand); border-color:var(--jf-brand); color:#fff !important; }
.jf-btn.primary:hover { background:var(--jf-brand-dark); color:#fff !important; }
.jf-card details { margin-top:.5rem; }
.jf-card summary { cursor:pointer; color:var(--jf-brand); font-size:.85rem; font-weight:600; }
.jf-desc { font-size:.86rem; color:#334155; margin-top:.4rem; line-height:1.5; max-height:260px; overflow:auto;
  background:var(--jf-soft); padding:.6rem .8rem; border-radius:10px; }
.jf-score { height:6px; background:#e2e8f0; border-radius:999px; overflow:hidden; margin-top:.35rem; max-width:200px; }
.jf-score i { display:block; height:100%; background:linear-gradient(90deg,#14b8a6,#0f766e); }
.jf-empty { text-align:center; padding:1.6rem 1rem; color:var(--jf-muted); background:var(--jf-card);
  border:1px dashed #cbd5e1; border-radius:var(--jf-radius); }
.jf-empty .jf-empty-t { color:var(--jf-ink); font-weight:700; font-size:1.05rem; margin-bottom:.3rem; }
.jf-steps { margin:.5rem 0 0 0; padding:0; list-style:none; font-size:.86rem; color:#334155; }
.jf-steps li { padding:.12rem 0; }
.jf-steps li.now { font-weight:700; color:var(--jf-brand); }
.jf-steps li.done { color:var(--jf-muted); }
.jf-note { background:#fffbeb; border:1px solid #fde68a; color:#78350f; border-radius:12px; padding:.55rem .8rem;
  font-size:.86rem; margin:.4rem 0; }
.jf-gdpr { background:#f0f9ff; border:1px solid #bae6fd; color:#0c4a6e; border-radius:12px; padding:.5rem .8rem;
  font-size:.84rem; margin:.3rem 0 .6rem 0; }

/* ---- Streamlit widgets */
[data-testid="stMetric"] { background:var(--jf-card); border:1px solid var(--jf-line); border-radius:12px;
  padding:.55rem .85rem; box-shadow:0 1px 2px rgba(15,23,42,.04); }
[data-testid="stMetricValue"] { font-size:1.55rem; font-weight:750; color:var(--jf-brand-dark); }
[data-testid="stMetricLabel"] p { font-size:.78rem; color:var(--jf-muted); font-weight:600; }
[data-testid="stExpander"] { border-radius:12px; background:var(--jf-card); border-color:var(--jf-line); }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:var(--jf-radius); }
.stButton > button, .stDownloadButton > button, .stLinkButton > a { border-radius:10px; font-weight:600; min-height:42px; }
.stTextInput input, .stTextArea textarea, .stSelectbox [data-baseweb="select"] > div, .stNumberInput input { border-radius:10px; }
[data-testid="stTabs"] [data-baseweb="tab"] { font-weight:650; padding:.5rem .9rem; }
[data-testid="stPills"] button, [data-testid="stSegmentedControl"] button { border-radius:999px; }
div[class*="st-key-sticky_"] { position:sticky; top:3.4rem; z-index:40; background:#f7f9fb; padding:.4rem 0 .3rem 0; }
.st-key-gate { max-width:430px; margin:2.5rem auto 0 auto; background:#fff; border:1px solid var(--jf-line);
  border-radius:20px; padding:1.6rem 1.5rem 1.2rem 1.5rem; box-shadow:0 12px 40px rgba(15,23,42,.10); }
.st-key-gate h1 { text-align:center; font-size:1.55rem !important; }
.jf-gate-sub { text-align:center; color:var(--jf-muted); font-size:.92rem; margin:-.2rem 0 1rem 0; }
.jf-gate-foot { text-align:center; color:var(--jf-muted); font-size:.78rem; margin-top:.6rem; }

/* ---- phones */
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding-left:.75rem; padding-right:.75rem; padding-top:4rem; }
  h1 { font-size:1.4rem !important; }
  h2 { font-size:1.2rem !important; }
  .jf-hero { padding:.9rem 1rem; border-radius:16px; } .jf-hero h2 { font-size:1.15rem; }
  .jf-btn { flex:1 1 auto; justify-content:center; min-height:44px; }
  div[class*="st-key-metrics"] [data-testid="stHorizontalBlock"] {
    flex-wrap:wrap !important; gap:.5rem !important; }
  div[class*="st-key-metrics"] [data-testid="stColumn"] {
    min-width:calc(50% - .5rem) !important; flex:1 1 calc(50% - .5rem) !important; width:calc(50% - .5rem) !important; }
  div[class*="st-key-sticky_"] { position:static; }
  [data-testid="stMetricValue"] { font-size:1.3rem; }
  .stButton > button, .stDownloadButton > button { min-height:46px; }
  .st-key-gate { margin-top:1rem; padding:1.2rem 1rem; border-radius:16px; }
}
</style>
"""


def inject_css() -> None:
    import streamlit as st
    st.markdown(CSS, unsafe_allow_html=True)


# ------------------------------------------------------------------ safe HTML helpers
def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def clean(v: Any) -> str:
    """'' for None / NaN / NaT, else the text."""
    if v is None:
        return ""
    try:
        if v != v:               # NaN
            return ""
    except Exception:  # noqa: BLE001
        pass
    s = str(v).strip()
    return "" if s in ("NaT", "nan", "None") else s


def safe_url(u: Any) -> str:
    """http(s) URLs only (no javascript:, data: ...), with characters that could break out of an attribute removed."""
    s = clean(u).split()[0] if clean(u) else ""
    if not re.match(r"^https?://[^\s<>\"']+$", s, re.I):
        return ""
    return s


_EMAIL = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def safe_email(e: Any) -> str:
    s = clean(e)
    return s if _EMAIL.match(s) else ""


def safe_tel(p: Any) -> str:
    """A tel: number (digits and a leading +), or '' when it doesn't look like a phone number."""
    s = clean(p)
    digits = re.sub(r"[^\d+]", "", s.split(";")[0].split(" or ")[0])
    return digits if len(re.sub(r"\D", "", digits)) >= 6 else ""


def host(url: str) -> str:
    m = re.match(r"^https?://(?:www\.)?([^/\s]+)", url or "", re.I)
    return m.group(1) if m else ""


def badge(text: str, kind: str = "neutral") -> str:
    return f'<span class="jf-badge {esc(kind)}">{esc(text)}</span>'


def button(href: str, label: str, primary: bool = False, external: bool = True) -> str:
    """A link styled as a button. href must already be validated (safe_url / mailto: / tel:)."""
    if not href:
        return ""
    tgt = ' target="_blank" rel="noopener noreferrer"' if external and href.startswith("http") else ""
    return f'<a class="jf-btn{" primary" if primary else ""}" href="{esc(href)}"{tgt}>{esc(label)}</a>'


def email_button(email: Any, label: Optional[str] = None) -> str:
    e = safe_email(email)
    return button(f"mailto:{e}", label or f"✉ {e}") if e else ""


def phone_button(phone: Any, label: Optional[str] = None) -> str:
    t = safe_tel(phone)
    return button(f"tel:{t}", label or f"📞 {clean(phone)[:28]}") if t else ""


def link(url: Any, text: Optional[str] = None) -> str:
    u = safe_url(url)
    return f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{esc(text or host(u) or u)}</a>' if u else ""


def card(body: str, kind: str = "") -> str:
    return f'<div class="jf-card {esc(kind)}">{body}</div>'.replace("\n", " ")


def cards_html(items: Iterable[str]) -> str:
    return "".join(items)


def empty_state(title: str, tips: str) -> str:
    return f'<div class="jf-empty"><div class="jf-empty-t">{esc(title)}</div>{tips}</div>'


# ------------------------------------------------------------------ dates
def _as_date(v: Any) -> Optional[date]:
    if clean(v) == "":              # None, NaN, NaT
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = clean(v)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def closing_badge(v: Any, today: Optional[date] = None) -> str:
    d = _as_date(v)
    if d is None:
        return ""
    days = (d - (today or date.today())).days
    if days < 0:
        return badge("Closed", "neutral")
    if days == 0:
        return badge("Closes today", "warn")
    if days <= 7:
        return badge(f"Closes in {days} day{'s' if days != 1 else ''}", "warn")
    return badge(f"Closes {d.strftime('%d %b')}", "info")


def days_to_close(v: Any, today: Optional[date] = None) -> Optional[int]:
    d = _as_date(v)
    return None if d is None else (d - (today or date.today())).days


def ago(v: Any, today: Optional[date] = None) -> str:
    d = _as_date(v)
    if d is None:
        return ""
    n = ((today or date.today()) - d).days
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 31:
        return f"{n} days ago"
    return d.strftime("%d %b %Y")


# ------------------------------------------------------------------ contract badges
_LOCUM_WORDS = re.compile(r"\b(locum|bank|sessional|agency|ad[- ]?hoc|temp(orary)?|relief|flexi|per diem|casual)\b", re.I)


# in a job TITLE only unambiguous words count ("Bank Manager" is not a bank shift)
_TITLE_TEMP = re.compile(r"\b(locum|sessional|agency|ad[- ]?hoc|bank(?= (nurse|staff|hca|shift|worker|practice|assistant|carer))"
                         r"|(?<=nurse )bank|(?<=staff )bank)\b", re.I)


def contract_badge(contract: str = "", employment_type: str = "", title: str = "") -> tuple[str, str]:
    """(text, kind): ('Locum', 'locum'), ('Bank', 'locum'), ('Permanent', 'perm') or ('', '') when the ad doesn't say."""
    et = clean(employment_type)
    m = _LOCUM_WORDS.search(et) or _TITLE_TEMP.search(clean(title))
    if clean(contract) == "temporary" or m:
        word = (m.group(1) if m else et or "locum / temp").lower()
        word = {"temp": "Temporary", "temporary": "Temporary", "adhoc": "Ad hoc", "ad hoc": "Ad hoc", "ad-hoc": "Ad hoc"}.get(
            word, word.capitalize())
        if "fixed" in et.lower() and not m:
            word = "Fixed-term"
        if "contract" in et.lower() and not m:
            word = "Contract"
        return word, "locum"
    if clean(contract) == "permanent" or "permanent" in et.lower():
        return "Permanent", "perm"
    return "", ""
