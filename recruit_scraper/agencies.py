"""Direct employers only: spot recruitment / staffing / locum agencies so they can be left out.

Job Finder is a tool for a recruiter to find vacancies at the employers themselves, so other recruiters are never
listed: not their job ads (the advertiser is an agency, or the ad says 'on behalf of our client') and not their
company entries in 'Employers to contact' (CQC-registered nurse / staffing agencies and the like).

What counts as an agency:
* a well-known agency or job-board name (Reed, Hays, Pertemps, Randstad, Medacs, Pulse, ID Medical, Thornbury,
  Your World Healthcare, Sanctuary Personnel, NHS Professionals, Indeed Flex, Teaching Personnel …) and locum
  booking apps / platforms (Lantum, Patchwork Health, Locum's Nest, LocumKit …);
* a name that says so ('… Recruitment', '… Staffing', 'Locum Agency', 'Nursing Agency', '… Personnel',
  'Healthcare Professionals', 'Resourcing', 'Executive Search');
* an ad whose text says it is placed for someone else ('our client', 'on behalf of our client', 'employment agency').
An NHS trust's OWN staff bank ('Bank staff at X Hospitals NHS Foundation Trust') is a direct employer and stays;
NHS Professionals and other third-party banks are agencies.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

_KNOWN = [
    # general / multi-sector
    "reed", "reed specialist recruitment", "hays", "pertemps", "randstad", "adecco", "manpower(group)?", "michael page",
    "page personnel", "robert walters", "robert half", "kelly services", "staffline", "blue arrow", "brook street",
    "gap personnel", "office angels", "morgan hunt", "hudson", "cpl", "matchtech", "badenoch ?(\\+|and|&) ?clark",
    "harvey nash", "venn group", "networkers", "computer people", "jobsite", "totaljobs", "cv-?library", "monster",
    "indeed( flex)?", "glassdoor", "linkedin", "adzuna", "jooble", "talent\\.com", "jobrapido", "coople", "gig ?hub",
    "pulse (jobs|healthcare|staffing|recruitment|medical|nursing|locums?|group)", "jobs? ?go ?public", "learn4good",
    # health and social care
    "medacs( healthcare)?", "id medical", "thornbury( nursing services| nursing)?", "your world( healthcare)?",
    "sanctuary personnel", "sanctuary (healthcare )?staffing", "nhs professionals", "nhsp", "hunter healthcare",
    "mullany", "greenstaff( medical)?", "hcl healthcare", "nurse seekers", "newcross( healthcare)?",
    "constellation healthcare", "ambition healthcare", "liquid personnel", "carlisle staffing", "medical staffing network",
    "mayday healthcare", "plan healthcare", "penguin recruitment", "locum'?s nest", "locumsnest", "bluebird staffing",
    "cavendish professionals", "healthcare locums", "bright ?path medical", "wtl healthcare", "xpr healthcare", "nurse ?plus",
    "pulse healthcare", "ashbourne healthcare", "cera care staffing", "nursing personnel", "medics ?direct",
    "gtd healthcare", "lantum", "patchwork( health)?", "locumkit", "locum ?(bookings|cover|app)", "shift ?key", "bank ?staff ?app", "tempus ?health",
    "bank partners", "rwj", "drs ?direct", "staff ?nurse ?direct", "trustedcare", "totalmed", "ashton ?healthcare",
    # education
    "teaching personnel", "tradewind( recruitment)?", "supply desk", "eteach", "teacheractive", "teach active",
    "prospero teaching", "engage education", "protocol education", "vision for education", "monarch education",
    "smart teachers", "aspire people", "randstad education", "hays education", "axcis education", "connex education",
    "ase education", "synergy teaching", "academics ltd", "capita education", "supply ?teachers? ?direct",
    # law / finance / trades / hospitality
    "taylor root", "laurence simons", "major players", "douglas scott", "ellis fairbank", "lex ?recruit",
    "accountancy ?action", "joslin rowe", "hudson legal", "chef ?in ?the ?house", "chefs ?connect", "hospitality ?staff",
    "tempest ?recruit", "jobs4chefs", "chef jobs", "caterer\\.com",
    # law
    "sammons( legal)?", "chadwick nott", "shilton sharpe quarry", "bcl legal", "g2 legal", "clayton legal", "simply law jobs",
    "totally legal", "legal ?week jobs", "jm ?legal", "macdonald ?& ?company", "law ?gazette jobs", "c ?& ?m legal",
    "tlt resourcing", "dmj legal", "edwards gibson", "frazer jones",
    # accountancy / finance
    "sewell wallis", "marks sattin", "goodman masson", "walker hamill", "pro-?finance", "brewer morris", "cast uk",
    "rise technical", "finance ?& ?accountancy professionals", "accountancy age jobs", "icaew jobs", "acca careers",
    "hays accountancy", "page group", "spencer ?ogden", "kenda ?recruitment",
    # trades / construction / facilities
    "randstad cpe", "gi ?group", "fusion people", "prime appointments", "trades ?workforce( solutions)?", "tradesmen on demand",
    "construction personnel", "build ?recruitment", "hamilton mayday", "first choice staff", "pertemps construction",
    "rullion", "redline group", "sanderson recruitment", "cpe recruitment", "workforce ?solutions", "beachwood recruit",
    "hunters? (people|workforce)", "on ?site recruitment", "tradestaff", "trade ?force",
    # hospitality / catering
    "berkeley scott", "hospitality recruitment solutions", "rocket recruitment", "syft", "stint ?(\\.co|app|staffing)", "temper (works|app)",
    "brigad", "yourcatering ?recruit", "hcareers", "springboard jobs", "indeed flex", "the ?staffing ?group",
    "hospitality ?jobs ?uk", "chefs ?on ?call", "rsvp ?staffing", "event ?staff ?uk", "ess ?staffing",
    # care
    "florence (app|staffing|healthcare staffing)", "hcl workforce", "sugarman", "ambition ?24 ?hours?", "care ?staffing ?solutions", "nursing ?point",
    "medacs care", "team ?24", "vivid ?care ?staffing", "care ?24 ?seven",
    # education
    "remedy education", "long term futures", "edex education", "senploy", "career teachers",
    "tes jobs", "spencer clarke group", "cover teachers", "school ?staffing ?solutions",
]
_KNOWN_RX = re.compile(r"(?<![\w])(" + "|".join(_KNOWN) + r")(?![\w])", re.I)

# a name that says the company supplies staff to other employers
_STAFFING_RX = re.compile(
    r"recruit(ment|ers?|ing)?\b|\bstaffing\b|\bpersonnel\b|\bresourc(ing|es) (ltd|limited|group|solutions)\b|"
    r"\bheadhunt|executive search|\btalent (partners?|solutions|acquisition|hub)\b|\bsearch (&|and) selection\b|"
    r"\bemployment (agency|business|services|bureau)\b|\blocums?\b|\blocum(s)? (agency|cover|staffing)\b|"
    r"\b(nurse|nurses|nursing|medical|healthcare|health care|clinical|carer|carers|care staff|social care|supply|teaching|"
    r"teacher) (agency|agencies|staff|staffing|bank|professionals|relief|cover|personnel|locums?|solutions)\b|"
    r"\b(nurse|nurses|nursing) (direct|link|connect|plus|force|team|relief|bank)\b|\brelief (staff|nurses|carers|cover)\b|"
    r"\bagency (nurses|staff|workers|care workers)\b|\bstaff(ing)? (bank|agency|solutions|supply)\b|\bmanpower\b|"
    r"\btemp(s|orary)? (agency|staff|jobs|work)\b|\bjobs? (board|site)\b", re.I)
# an ad that is placed for somebody else
_AD_RX = re.compile(
    r"\bour client(?:s)?\b(?=\s*(?:,|is|are|has|have|a |an |the |who|based|located|one of|requires?|wants?|is seeking|are seeking|"
    r"looking|currently))|on behalf of (?:our|a|one of our|my) (?:client|customer)|\bacting as an? employment (?:agency|business)|"
    r"\bemployment (?:agency|business) (?:and|&) (?:recruitment|staffing)|\bwe are (?:an? )?(?:independent )?(?:specialist )?"
    r"(?:recruitment|staffing|locum) (?:agency|consultancy|company|business)|\brecruitment consultant\b|"
    r"\bthis (?:vacancy|role|position|job) is (?:being )?(?:advertised|recruited|managed|handled) (?:by|through|on behalf)|"
    r"\bour (?:exclusive )?(?:client|partner) (?:trust|practice|surgery|hospital|school|home|company)\b|"
    r"\bapply (?:today )?(?:with|to|through) (?:your|our) (?:consultant|recruiter)\b|\bregistered with us (?:to|and)\b", re.I)
# an NHS trust's / board's own staff bank is a direct employer
_OWN_BANK = re.compile(r"\b(nhs|foundation trust|health board|hospitals? (nhs )?trust|nhs trust)\b", re.I)
_NHSP = re.compile(r"\bnhs professionals\b|\bnhsp\b", re.I)


def _s(v: Any) -> str:
    if v is None:
        return ""
    try:
        if v != v:                       # NaN
            return ""
    except Exception:  # noqa: BLE001
        pass
    return str(v).strip()


def is_agency_name(name: Any) -> bool:
    """True when a company / advertiser / organisation name is a recruitment, staffing or locum agency or a job board."""
    n = _s(name)
    if not n:
        return False
    if _NHSP.search(n) or n.lower() in ("pulse", "reed", "hays"):
        return True
    if _OWN_BANK.search(n) and not re.search(r"recruit|agency|staffing", n, re.I):
        return False                     # 'Royal Free London NHS Foundation Trust' / its own bank
    return bool(_KNOWN_RX.search(n) or _STAFFING_RX.search(n))


def is_agency_text(text: Any) -> bool:
    """True when an ad's text says it is placed on behalf of a client (a recruiter's ad)."""
    t = _s(text)
    return bool(t and _AD_RX.search(t[:6000]))


def agency_reason(job: Any) -> str:
    """'' for a direct employer's ad, else why it is dropped ('agency name: Hays' / 'ad says it is placed for a client')."""
    company = _s(getattr(job, "company", ""))
    if is_agency_name(company):
        return f"agency: {company}"
    desc = _s(getattr(job, "description", ""))
    if is_agency_text(desc) or is_agency_text(getattr(job, "title", "")):
        return "ad is placed on behalf of a client"
    return ""


def is_agency_job(job: Any) -> bool:
    return bool(agency_reason(job))


def is_agency_employer(name: Any, employer_type: Any = "") -> bool:
    """For 'Employers to contact': an agency by name, or registered/labelled as a nurse / staffing agency."""
    t = _s(employer_type).lower()
    if re.search(r"nurs(e|ing)( /|,)? ?(staffing )?agency|staffing agency|recruitment|employment agency|locum agency", t):
        return True
    return is_agency_name(name)


def drop_agency_jobs(jobs: Iterable[Any]) -> tuple[list[Any], int]:
    kept, n = [], 0
    for j in jobs:
        if is_agency_job(j):
            n += 1
        else:
            kept.append(j)
    return kept, n
