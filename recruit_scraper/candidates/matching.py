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
              "paediatric nurse", "rsc nurse", "nursing associate", "general practice nurse", "gpn",
              "treatment room nurse", "advanced nurse practitioner", "nurse prescriber", "registered general nurse",
              "nmc registered", "bank nurse", "agency nurse", "locum nurse"],
    "registered nurse": ["rgn", "rmn", "rnld", "staff nurse", "nurse"],
    "mental health nurse": ["rmn", "psychiatric nurse", "cpn", "community psychiatric nurse", "mental health nursing"],
    "learning disability nurse": ["rnld", "ld nurse"],
    "theatre nurse": ["scrub nurse", "anaesthetic nurse", "recovery nurse", "perioperative", "odp"],
    "icu nurse": ["itu nurse", "critical care nurse", "intensive care nurse"],
    "a&e nurse": ["emergency nurse", "ed nurse", "emergency department nurse"],
    # primary care nursing: the same job under the names GP surgeries, PCNs and nurses themselves use
    "practice nurse": ["general practice nurse", "gp nurse", "gpn", "treatment room nurse", "primary care nurse",
                       "practice nursing", "gp practice nurse", "surgery nurse"],
    "general practice nurse": ["practice nurse", "gp nurse", "gpn", "treatment room nurse", "primary care nurse"],
    "treatment room nurse": ["practice nurse", "general practice nurse", "gp nurse", "gpn"],
    "advanced nurse practitioner": ["anp", "nurse practitioner", "advanced clinical practitioner",
                                    "advanced practice nurse", "advanced nurse practitioners"],
    "anp": ["advanced nurse practitioner", "nurse practitioner", "advanced clinical practitioner"],
    "nurse practitioner": ["anp", "advanced nurse practitioner", "advanced clinical practitioner",
                           "advanced practice nurse"],
    "district nurse": ["community nurse", "community nursing", "district nursing", "district nursing sister",
                       "community staff nurse"],
    "community nurse": ["district nurse", "community nursing", "district nursing", "community staff nurse"],
    "healthcare assistant": ["hca", "health care assistant", "nursing assistant", "care assistant", "clinical support worker",
                             "healthcare support worker", "health care support worker", "hcsw"],
    "hca": ["healthcare assistant", "health care assistant", "healthcare support worker", "hcsw"],
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
    # not 'lecturer' / 'teaching' on their own: universities and lecturers aren't school teachers
    "teacher": ["educator", "tutor", "secondary teacher", "primary teacher", "headteacher", "supply teacher",
                "early career teacher", "nqt", "sendco", "senco"],
    "teaching assistant": ["learning support assistant", "classroom assistant"],
    # a bare 'cook' is too often a surname or a game ('Monte Cook Games'): see _not_the_job
    "chef": ["cook", "head cook", "line cook", "prep cook", "school cook", "breakfast cook", "professional cook", "sous chef", "head chef", "pastry chef", "chef de partie", "commis chef"],
    "driver": ["hgv driver", "lgv driver", "delivery driver", "van driver", "courier", "class 1 driver",
               "class 2 driver"],
    "receptionist": ["front of house", "reception", "front desk"],
    "administrator": ["admin assistant", "administrative assistant", "office administrator", "office manager",
                      "admin"],
    "cleaner": ["cleaning", "housekeeper", "housekeeping", "domestic cleaner"],
    "hairdresser": ["hair stylist", "stylist", "barber", "hairstylist"],
    "lawyer": ["solicitor", "barrister", "paralegal", "legal counsel", "attorney", "legal executive", "cilex",
               "conveyancer", "in-house counsel", "trainee solicitor", "associate solicitor", "solicitor advocate"],
    "solicitor": ["lawyer", "trainee solicitor", "associate solicitor", "partner solicitor", "legal counsel",
                  "solicitor advocate"],
    "barrister": ["counsel", "kc", "qc", "pupil barrister", "faculty of advocates", "called to the bar"],
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
# close-but-not-identical jobs a recruiter would still want to see (scored like 'related', below exact synonyms):
# a GP surgery hiring a practice nurse also wants nurse prescribers, chronic-disease / screening nurses, ANPs
_PN_RELATED = ["nurse practitioner", "advanced nurse practitioner", "anp", "nurse prescriber", "independent prescriber",
               "non-medical prescriber", "chronic disease nurse", "long term conditions nurse", "respiratory nurse",
               "diabetes nurse", "diabetes specialist nurse", "asthma nurse", "copd nurse", "travel health nurse",
               "immunisation nurse", "sexual health nurse", "smear taker", "cervical sample taker", "cervical screening"]
RELATED: dict[str, list[str]] = {
    "practice nurse": _PN_RELATED,
    "general practice nurse": _PN_RELATED,
    "treatment room nurse": _PN_RELATED,
    "advanced nurse practitioner": ["nurse prescriber", "independent prescriber", "practice nurse", "paramedic practitioner"],
    "nurse practitioner": ["nurse prescriber", "independent prescriber", "practice nurse"],
    "healthcare assistant": ["phlebotomist", "phlebotomy", "health care support worker"],
    "hca": ["phlebotomist", "phlebotomy", "nursing assistant"],
    "district nurse": ["community matron", "specialist practitioner district nursing", "community nursing sister"],
    "community nurse": ["community matron", "district nursing sister"],
}
# a LOCUM / bank search for one of these titles also takes vacancies for the close primary / community nursing jobs
# (locum ANP, bank district nurse, sessional immunisation nurse): locum work is advertised under many names
LOCUM_FAMILY: dict[str, list[str]] = {
    "practice nurse": _PN_RELATED + ["district nurse", "community nurse", "community staff nurse", "community matron",
                                     "advanced clinical practitioner", "vaccinator",
                                     "minor injuries nurse", "urgent care nurse", "walk-in centre nurse",
                                     "out of hours nurse"],
}
LOCUM_FAMILY["general practice nurse"] = LOCUM_FAMILY["treatment room nurse"] = LOCUM_FAMILY["practice nurse"]
LOCUM_FAMILY["nurse practitioner"] = LOCUM_FAMILY["advanced nurse practitioner"] = [
    "advanced clinical practitioner", "practice nurse", "general practice nurse", "nurse prescriber", "urgent care nurse"]
LOCUM_FAMILY["district nurse"] = LOCUM_FAMILY["community nurse"] = [
    "district nurse", "community nurse", "community staff nurse", "community matron", "district nursing",
    "community nursing", "practice nurse"]
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


_ACRONYM = re.compile(r"^([b-df-hj-np-tv-z&]{2,5}|[a-z]{1,2}&[a-z]|acca|aca|cima|rnld|rmn|rgn|hca|icu|itu|sen|senco|gp)$")


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
        related: list[str] = list(RELATED.get(self.core, []))
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
            # skip ambiguous acronyms like 'pa', 'ta', 'rnld', 'hca' for searching (they still count when matching)
            if len(s) > 3 and s not in out and not _ACRONYM.match(s):
                out.append(s)
        return [o for o in out if o]

    def match(self, *texts: Optional[str]) -> Match:
        text = _norm(" ".join(t for t in texts if t))
        if not text:
            return Match()
        if self.kw and not any(all(w in text for w in k.split()) for k in self.kw):
            return Match(0, "")
        hit = None
        # the first phrase that really is the job wins: 'Unpaid carer, support worker' -> 'support worker'
        cands = ([(3.0, self.core, self._exact)] if self._exact is not None else []) + [(sc, s, rx) for s, rx, sc in self._syn]
        for sc, s, rx in cands:
            if (mm := rx.search(text)) and not _not_the_job(text, f"profile says '{s}'"):
                hit = (sc, f"profile says '{s}'", mm.start(), mm.end())
                break
        if hit is None:
            if len(self.content_words) >= 2 and all(rx.search(text) for rx in self._words):
                hit = (1.5, "profile mentions " + " + ".join(f"'{w}'" for w in self.content_words), -1, -1)
            else:
                return Match()
        score, reason, pos, end = hit
        if pos < 0 and _not_the_job(text, reason):
            return Match()
        ctx = text[max(0, pos - 45):pos] if pos >= 0 else ""
        fm = _FORMER.search(ctx)
        if fm and not re.search(r"\b(now|currently|today)\b", ctx[fm.start():]):
            return Match()  # "retired accountant", "former electrician", "ex-nurse": not a current match
        if pos >= 0 and _ASPIRING.search(text[max(0, pos - 40):pos]):
            return Match()  # "aspiring to become a general practice nurse", "student nurse hoping to be a GPN"
        if end >= 0 and _FORMER_AFTER.match(text[end:end + 60]):
            return Match()  # "GP practice nurse before", "nurse (retired)", "nurse in a past life"
        if _ORG.search(text) and not _PERSON.search(text):
            score, reason = score - 0.5, reason + " (looks like a business/organisation account)"
        return Match(score, reason)


_PAID_CARER = re.compile(r"\b(professional|paid|senior|home|domiciliary|live[- ]in|night|care home|agency|bank|relief|"
                         r"lead|nhs|community|private|personal|registered)\s+(carer|caregiver)s?\b|\b(carer|caregiver)s?"
                         r"\s+(at|in|with|for)\s+(a|an|the)\s+\w*\s*(care|home|agency|company|hospital|service|charity)|"
                         r"\b(work|working|works|job|employed)\s+(as\s+)?(an?\s+)?(carer|caregiver)\b|"
                         r"^\W*(carer|caregiver)\W*$", re.I)
# 'carer' that means looking after family, not a care job
_UNPAID = re.compile(r"\b(full[- ]?time|unpaid|family|parent|young|informal|primary|sole|24/7|live[- ]in family)\s+"
                     r"(carer|caregiver|care giver)s?\b|\b(carer|caregiver|care giver)s?\s+(for|to|of)\s+(my|her|his|"
                     r"their|our|a family|mum|mom|dad|mother|father|son|daughter|wife|husband|partner|parents?|"
                     r"step|disabled|autistic|elderly)|\b(mum|mom|dad|mother|father|wife|husband|parent|gran|nan|"
                     r"grandparent|daughter|son)\b\W{1,3}(and\s+|&\s*)?(carer|caregiver)\b|\bmy\s+(carer|caregiver)\b",
                     re.I)
# words that mean something else in another language ('chef de service' = head of department in French)
_FALSE_FRIEND = {"chef": re.compile(r"\bchef(fe)?\s*(\(fe\))?\s*(d['’]une?|de (service|projet|file|clinique|bureau|"
                                     r"cabinet|l['’]|la |équipe|equipe|mission|produit))", re.I)}


_ANIMAL = re.compile(r"\b(veterinary|vet|animal|pet|equine|zoo)\s+(health\s*)?(care\s+assistant|care\s+worker|carer|"
                     r"nurse|nursing|care)\b", re.I)


def _not_the_job(text: str, reason: str) -> bool:
    term = (re.search(r"'([^']+)'", reason or "") or [None, ""])[1].lower()
    if term in ("carer", "caregiver", "care giver", "home carer") and _UNPAID.search(text):
        return True
    if term in ("carer", "caregiver") and not _PAID_CARER.search(text):
        return True       # a bare 'carer' in a bio is nearly always family / unpaid / foster care, not a care job
    if term == "cook" and not re.search(r"^\W*cook\W*$|\b(a|as|the|head|line|prep|school|pastry|breakfast|"
                                        r"professional|trainee|commis|private)\s+cook\b|\bcook\s+(at|in|for)\b",
                                        text, re.I):
        return True
    if term and _ANIMAL.search(text) and not _phrase_re(term).search(_ANIMAL.sub(" ", text)):
        return True       # 'Veterinary Care Assistant', 'vet nurse': animal care, not a health / social care job
    rx = _FALSE_FRIEND.get(term.split()[-1] if term else "")
    return bool(rx and rx.search(text) and not re.search(r"\bchef de (partie|cuisine|rang)\b", text, re.I))


_FORMER = re.compile(r"\b(retired|former(ly)?|ex|failed|recovering|recovered|used to be|one-?time|previous life|past life)\b[\s\-,]*([\w/&\-]+[\s,]+){0,3}$",
                     re.I)
_ASPIRING = re.compile(r"\b(aspiring|aspire|aspires|hoping to|hope to|want(s|ing)? to|would like to|dream(s|ing)? of|"
                       r"working towards|on my way to|future)\b[\w\s\-]{0,25}$", re.I)
_FORMER_AFTER = re.compile(r"^\W{0,3}(\(?retired\)?|before\b|previously\b|formerly\b|in (a|my) (past|previous|former) "
                           r"(life|career)|until (19|20)\d\d|for \d+ years,? (now|then) )", re.I)
_ORG = re.compile(r"^\W*(we|our)\b|\b(we are|we provide|we offer|we specialise|our team|contact us|call us|"
                  r"get in touch with us|services ltd|official account)\b", re.I)
_PERSON = re.compile(r"\b(i|i'm|im|my|me|he/him|she/her|they/them)\b", re.I)


_BIZ_NAME = re.compile(r"\b(ltd|limited|llp|plc|cic|inc|services|solutions|group|agency|associates|partners|& co|"
                       r"electricians|plumbers|accountants|solicitors|builders|roofers|carers|recruitment|consultancy|"
                       r"news|journal|magazine|podcast|official|council|charity|association|society|network|"
                       r"foundation|trust|union|federation|institute|uk|university|college|hospital|hospice|library|"
                       r"museum|chambers|surgery|pcn|medical (centre|center|practice|group)|health centre)\b", re.I)
# bios of publications / institutions written in the third person ('Practice Nurse journal is designed to…')
_INST_BIO = re.compile(r"^\W*(the\s+)?[\w\s&'’-]{0,60}\b(journal|magazine|publication|newsletter|podcast|university|"
                       r"college|institution|organisation|organization)\b[^.]{0,40}\b(is|are|publishes|provides)\b|\b(one of the uk'?s|official account|posting weekdays|is designed to|"
                       r"peer[- ]reviewed)\b", re.I)
_BIZ_BIO = re.compile(r"\b(provides?|delivers?|offering|we('re| are| provide| offer| help| support)|our (team|services|"
                      r"clients|firm|practice|members)|(is|are) an? (independent|leading|award[- ]winning|professional|"
                      r"chartered|family[- ]run|local|trusted|uk)[\w\s,&-]{0,40}(firm|company|practice|business|charity|"
                      r"organisation|organization|provider|agency|union|journal)|firm of|charity for|journal of|"
                      r"official (account|journal)|registered charity|company number|tel:|call us|ltd|limited|llp|"
                      r"join our|book (now|online|"
                      r"a free)|free quote)\b", re.I)


def looks_like_business(name: str, bio: str) -> bool:
    """True for firm / organisation / news accounts (e.g. 'Beckenham Electricians provides…', 'Budget Accountants is an
    ACCA-chartered company…'), which are shown as businesses, not as individual candidates."""
    if _BIZ_NAME.search(name or ""):
        return True
    b = bio or ""
    if (_INST_BIO.search(b) and not re.search(r"\b(i'm|i am|i've|my)\b", b, re.I)
            and not re.search(r"\b(at|in|from|with)\s+(the\s+)?(uni|university|college|journal)\b", b, re.I)):
        return True
    return bool(_BIZ_BIO.search(b)) and not re.search(r"\b(i'm|i am|i've|my|me)\b|\b(he|she|they)/(him|her|them)\b",
                                                      b, re.I)


CONTRACT_TEMP = re.compile(r"\b(locum|sessional|flexi ?pool|agency|bank (nurse|staff|shifts?|work)|bank|temp|temporary|contract(or|ing)?|"
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
    if getattr(place, "nationwide", False):   # whole-UK search: any UK town / nation counts as 'in the area'
        from .geo import UK_TOWNS
        return list(dict.fromkeys(list(UK_TOWNS) + ["England", "Scotland", "Wales", "Northern Ireland", "Cymru"]))
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
