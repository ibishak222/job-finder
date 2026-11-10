"""Does a person's own public profile text actually match the job being searched for?

Every candidate source runs its results through `RoleMatcher`, so people only appear when their bio / headline /
website mentions the job (or a common synonym). Results are never padded with location-only matches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# common UK job titles -> other ways people describe the same job in their own bio
SYNONYMS: dict[str, list[str]] = {
    "care worker": ["carer", "care assistant", "support worker", "healthcare assistant", "domiciliary care",
                    "home carer", "senior carer", "care giver", "caregiver", "personal assistant care", "hca"],
    "support worker": ["care worker", "carer", "care assistant", "support work"],
    "nurse": ["registered nurse", "rgn", "rmn", "rnld", "staff nurse", "nursing", "mental health nurse",
              "psychiatric nurse", "learning disability nurse", "theatre nurse", "scrub nurse", "icu nurse",
              "itu nurse", "critical care nurse", "a&e nurse", "emergency nurse", "practice nurse", "district nurse",
              "community nurse", "nurse practitioner", "anp", "ward sister", "charge nurse", "nurse manager",
              "paediatric nurse", "rsc nurse", "midwife", "nursing associate"],
    "registered nurse": ["rgn", "rmn", "rnld", "staff nurse", "nurse"],
    "mental health nurse": ["rmn", "psychiatric nurse", "cpn", "community psychiatric nurse", "mental health nursing"],
    "learning disability nurse": ["rnld", "ld nurse"],
    "theatre nurse": ["scrub nurse", "anaesthetic nurse", "recovery nurse", "perioperative", "odp"],
    "icu nurse": ["itu nurse", "critical care nurse", "intensive care nurse"],
    "a&e nurse": ["emergency nurse", "ed nurse", "emergency department nurse"],
    "practice nurse": ["gp nurse", "general practice nurse"],
    "district nurse": ["community nurse", "community nursing"],
    "healthcare assistant": ["hca", "health care assistant", "nursing assistant", "care assistant", "clinical support worker",
                             "healthcare support worker"],
    "electrician": ["electrical contractor", "electrical installer", "electrical engineer", "sparky", "sparkie",
                    "electrical installation", "approved electrician", "niceic", "electrical apprentice"],
    "plumber": ["plumbing", "heating engineer", "gas engineer", "gas safe", "plumbing and heating"],
    "carpenter": ["joiner", "joinery", "carpentry", "cabinet maker"],
    "builder": ["construction", "bricklayer", "general builder", "building contractor"],
    "mechanic": ["vehicle technician", "motor mechanic", "auto technician", "car mechanic"],
    "accountant": ["accountancy", "chartered accountant", "acca", "cima", "aca", "bookkeeper", "bookkeeping",
                   "management accountant", "financial accountant", "tax adviser", "tax advisor", "auditor"],
    "bookkeeper": ["bookkeeping", "accounts assistant", "accountant"],
    "sales": ["sales executive", "sales manager", "account executive", "account manager", "business development",
              "bdm", "sales rep", "sales representative", "salesperson", "sdr", "bdr", "sales consultant"],
    "sales executive": ["sales", "account executive", "business development", "sales consultant"],
    "marketing": ["marketer", "digital marketing", "marketing manager", "content marketing", "seo", "social media manager"],
    "recruiter": ["recruitment consultant", "talent acquisition", "recruitment", "headhunter", "talent partner"],
    "teacher": ["teaching", "educator", "tutor", "lecturer", "teaching assistant", "secondary teacher",
                "primary teacher"],
    "teaching assistant": ["learning support assistant", "classroom assistant"],
    "chef": ["cook", "sous chef", "head chef", "pastry chef", "chef de partie", "commis chef"],
    "driver": ["hgv driver", "lgv driver", "delivery driver", "van driver", "courier", "class 1 driver",
               "class 2 driver"],
    "receptionist": ["front of house", "reception", "front desk"],
    "administrator": ["admin assistant", "administrative assistant", "office administrator", "office manager",
                      "admin"],
    "cleaner": ["cleaning", "housekeeper", "housekeeping", "domestic cleaner"],
    "hairdresser": ["hair stylist", "stylist", "barber", "hairstylist"],
    "lawyer": ["solicitor", "barrister", "paralegal", "legal counsel", "attorney", "legal executive", "cilex",
               "conveyancer", "in-house counsel", "trainee solicitor", "associate solicitor", "advocate"],
    "solicitor": ["lawyer", "trainee solicitor", "associate solicitor", "partner solicitor", "legal counsel",
                  "solicitor advocate"],
    "barrister": ["counsel", "kc", "qc", "pupil barrister", "advocate", "called to the bar"],
    "paralegal": ["legal assistant", "legal executive", "cilex", "trainee legal executive"],
    "legal executive": ["cilex", "chartered legal executive", "paralegal"],
    "conveyancer": ["conveyancing", "licensed conveyancer", "property lawyer", "residential property solicitor"],
    "legal secretary": ["legal pa", "legal assistant", "legal administrator", "secretary at a law firm"],
    "pharmacist": ["pharmacy", "pharmacy technician"],
    "physiotherapist": ["physio", "physiotherapy"],
    "social worker": ["social work", "social care"],
    "photographer": ["photography"],
    "graphic designer": ["graphic design", "designer", "illustrator", "visual designer", "brand designer"],
    "designer": ["design", "ux designer", "ui designer", "product designer", "graphic designer"],
    "project manager": ["programme manager", "project management", "pmp", "prince2", "delivery manager"],
    "customer service": ["customer support", "customer success", "call centre", "contact centre", "customer advisor"],
    "warehouse": ["warehouse operative", "picker", "packer", "forklift", "logistics", "stock controller"],
    "security": ["security officer", "security guard", "door supervisor", "sia"],
    "data analyst": ["data analysis", "analytics", "business analyst", "bi analyst", "power bi", "sql analyst"],
    "data scientist": ["data science", "machine learning", "ml engineer"],
    "software engineer": ["software developer", "developer", "programmer", "software engineering", "swe", "coder",
                          "full stack", "fullstack", "backend", "back-end", "frontend", "front-end"],
    "software developer": ["software engineer", "developer", "programmer", "coder"],
    "developer": ["software engineer", "programmer", "coder"],
    "web developer": ["frontend", "front-end", "full stack", "wordpress developer"],
    "devops": ["devops engineer", "sre", "site reliability", "platform engineer", "cloud engineer"],
    "qa": ["tester", "test engineer", "quality assurance", "qa engineer", "sdet"],
    "it support": ["helpdesk", "help desk", "service desk", "it technician", "desktop support", "1st line",
                   "2nd line"],
}
# words that say nothing about the job on their own (never enough for a match)
GENERIC = {"senior", "junior", "lead", "head", "principal", "trainee", "apprentice", "graduate", "intern", "chief",
           "assistant", "manager", "officer", "worker", "engineer", "specialist", "consultant", "executive",
           "staff", "team", "member", "of", "and", "the", "a", "an", "in", "for", "to", "with", "at", "or", "&",
           "uk", "london", "remote", "job", "jobs", "role", "position", "level", "experienced", "qualified",
           "full", "part", "time", "contract", "freelance", "permanent", "temp", "mid", "sr", "jr", "ii", "iii"}
SENIORITY = re.compile(r"\b(senior|junior|lead|head of|principal|trainee|graduate|intern|chief|sr\.?|jr\.?|mid[- ]level|"
                       r"experienced|qualified)\b", re.I)
TECH_WORDS = re.compile(
    r"\b(developer|programmer|software|devops|sre|data (scien|engineer|analyst)|machine learning|\bml\b|\bai\b|"
    r"frontend|front[- ]end|backend|back[- ]end|full[- ]?stack|web dev|python|java|javascript|typescript|react|"
    r"node|golang|rust|ruby|php|c#|\.net|kotlin|swift|ios|android|cloud|platform engineer|qa engineer|sdet|"
    r"test automation|security engineer|cyber|infosec|ux|ui designer|product designer|data analyst|analytics|"
    r"database|dba|sysadmin|linux|network engineer|it support|helpdesk|engineer(ing)? manager|cto|tech lead)", re.I)


def _norm(s: str) -> str:
    s = (s or "").lower().replace("&amp;", "&")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _phrase_re(phrase: str) -> re.Pattern:
    words = [re.escape(w) for w in phrase.split()]
    # allow simple plurals / -ing on the last word ("electricians", "accountancy" handled by synonyms)
    body = r"[\s\-/]+".join(words)
    return re.compile(rf"(?<![a-z0-9]){body}(s|es)?(?![a-z0-9])", re.I)


def is_tech_role(title: str) -> bool:
    return bool(TECH_WORDS.search(title or ""))


@dataclass
class Match:
    score: float = 0.0
    reason: str = ""


@dataclass
class RoleMatcher:
    """Scores profile text against a job title (+ optional extra keywords)."""
    title: str
    keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        t = _norm(self.title)
        self.contract = contract_type(t)          # "locum nurse" -> temporary; "permanent solicitor" -> permanent
        core = _norm(SENIORITY.sub(" ", t))
        core = CONTRACT_TEMP.sub(" ", core)
        core = CONTRACT_PERM.sub(" ", core)
        core = re.sub(r"[^a-z0-9#+.&\- ]", " ", core).strip()
        core = re.sub(r"\s+", " ", core)
        self.core = core or t
        self.tech = is_tech_role(t)
        exact: list[str] = []
        related: list[str] = []
        for key, alts in SYNONYMS.items():
            if key == self.core:
                exact += [a for a in alts if a != self.core]
            elif re.search(rf"\b{re.escape(key)}\b", self.core) and is_tech_role(key) == self.tech:
                related += [a for a in [key] + alts if a != self.core]
        self.synonyms = list(dict.fromkeys(exact))
        self.related = [r for r in dict.fromkeys(related) if r not in self.synonyms]
        words = [w for w in self.core.split() if w not in GENERIC and len(w) > 1]
        self.content_words = words
        self.kw = [_norm(k) for k in self.keywords if k and k.strip()]
        self._exact = _phrase_re(self.core) if self.core else None
        self._syn = [(s, _phrase_re(s), 2.0) for s in self.synonyms] + [(s, _phrase_re(s), 1.5) for s in self.related]
        self._words = [_phrase_re(w) for w in words]

    def search_terms(self, n: int = 4) -> list[str]:
        """Phrases to send to a source's search box: the title first, then the most common synonyms."""
        out = [self.core]
        for s in self.synonyms + self.related:
            if len(out) >= n:
                break
            if len(s) > 3 and s not in out:  # skip ambiguous acronyms like 'pa', 'ta' for searching
                out.append(s)
        return [o for o in out if o]

    def match(self, *texts: Optional[str]) -> Match:
        text = _norm(" ".join(t for t in texts if t))
        if not text:
            return Match()
        if self.kw and not any(all(w in text for w in k.split()) for k in self.kw):
            return Match(0, "")
        hit = None
        if self._exact is not None and (mm := self._exact.search(text)):
            hit = (3.0, f"profile says '{self.core}'", mm.start())
        else:
            for s, rx, sc in self._syn:
                if mm := rx.search(text):
                    hit = (sc, f"profile says '{s}'", mm.start())
                    break
        if hit is None:
            if len(self.content_words) >= 2 and all(rx.search(text) for rx in self._words):
                hit = (1.5, "profile mentions " + " + ".join(f"'{w}'" for w in self.content_words), -1)
            else:
                return Match()
        score, reason, pos = hit
        ctx = text[max(0, pos - 45):pos] if pos >= 0 else ""
        fm = _FORMER.search(ctx)
        if fm and not re.search(r"\b(now|currently|today)\b", ctx[fm.start():]):
            return Match()  # "retired accountant", "former electrician", "ex-nurse": not a current match
        if _ORG.search(text) and not _PERSON.search(text):
            score, reason = score - 0.5, reason + " (looks like a business/organisation account)"
        return Match(score, reason)


_FORMER = re.compile(r"\b(retired|former(ly)?|ex|failed|recovering|recovered|used to be|one-?time|previous life|past life)\b[\s\-,]*(\w+[\s,]+){0,3}$",
                     re.I)
_ORG = re.compile(r"^\W*(we|our)\b|\b(we are|we provide|we offer|we specialise|our team|contact us|call us|"
                  r"get in touch with us|services ltd|official account)\b", re.I)
_PERSON = re.compile(r"\b(i|i'm|im|my|me|he/him|she/her|they/them)\b", re.I)


CONTRACT_TEMP = re.compile(r"\b(locum|agency|bank (nurse|staff|shifts?|work)|bank|temp|temporary|contract(or|ing)?|"
                           r"freelance|interim|fixed[- ]term|zero[- ]hours?|ad[- ]hoc|self[- ]employed|sole trader)\b", re.I)
CONTRACT_PERM = re.compile(r"\b(permanent|perm\b|full[- ]time|salaried|substantive)", re.I)


def contract_type(text: str) -> str:
    """'temporary' (locum / agency / bank / temp / contract / freelance), 'permanent', or '' when not said."""
    t = text or ""
    if CONTRACT_TEMP.search(t):
        return "temporary"
    if CONTRACT_PERM.search(t):
        return "permanent"
    return ""


def years_from_text(text: str) -> Optional[float]:
    """'10 years experience', '15+ yrs in sales' -> 10 / 15. None when the person didn't say."""
    m = re.search(r"(\d{1,2})\s*\+?\s*(years?|yrs?)\b(?:'?\s*(of\s+)?(experience|exp|in|as|working))", text or "", re.I)
    if m:
        y = int(m.group(1))
        return float(y) if 0 < y <= 50 else None
    return None


def place_terms(place) -> list[str]:
    """Names a person in this area would write in their bio: districts, metro names, the region if it's London."""
    from .providers import location_variants
    out = location_variants(place, 15)
    extra = [place.town]
    if (place.region or "").lower() == "london":
        extra.append("London")
    for x in extra:
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return [o for o in out if o and len(o) > 2]


def find_place(text: str, terms: list[str]) -> Optional[str]:
    """First area name (from `terms`) mentioned in text, as a whole word."""
    t = text or ""
    for term in terms:
        if re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", t, re.I):
            return term
    return None
