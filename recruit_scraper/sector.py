"""NHS or independent? A simple, honest split for health-sector jobs and employers.

* Jobs from the NHS job boards (NHS Jobs, NHS Scotland Jobs, HSC Northern Ireland) are 'NHS' unless the advertiser is a
  well-known independent provider that uses those boards (Practice Plus Group, Spire, Bupa, hospices, ...).
* Employers from the regulators' independent-healthcare registers (CQC independent clinics / hospitals / private GP
  services, Healthcare Inspectorate Wales, RQIA independent clinics and hospitals) are 'Independent'.
* Anything else is left blank (a care home, a school, a law firm: the split does not apply).
"""
from __future__ import annotations

import re

NHS_SOURCES = {"nhsjobs", "nhsscotland", "hscni"}
_INDEPENDENT = re.compile(r"practice plus group|\bspire\b|\bbupa\b|nuffield|ramsay|circle health|priory|virgin care|"
                          r"care uk|\bhospice\b|\bprivate\b|independent|aesthetic|optegra|spamedica|harley street|"
                          r"london clinic", re.I)
_NHS_BODY = re.compile(r"\bNHS\b|foundation trust|health board", re.I)


def job_sector(source: str, company: str = "") -> str:
    """'NHS' / 'Independent' / '' for a job ad, from where it was advertised and who advertises it."""
    indep = bool(company and _INDEPENDENT.search(company) and not _NHS_BODY.search(company))
    if (source or "").split(":")[0] in NHS_SOURCES:
        return "Independent" if indep else "NHS"
    return "Independent" if indep else ""
