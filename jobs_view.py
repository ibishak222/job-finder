"""Pure helpers for the jobs page: source labels, the results table and downloads (no Streamlit calls)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from recruit_scraper.display import format_salary
from recruit_scraper.models import Job
from recruit_scraper.presets import PRESET_COMPANIES
from recruit_scraper.utils import contract_bucket
from ui_common import to_csv_bytes, to_excel_bytes, to_json_bytes

# name -> (friendly label, one-line hint, on by default)
SOURCE_INFO = {
    "greenhouse": ("Company career sites – Greenhouse", "Jobs straight from companies that use Greenhouse (e.g. Monzo, Deliveroo, Stripe).", True),
    "lever": ("Company career sites – Lever", "Jobs straight from companies that use Lever (e.g. Spotify, Palantir, Zopa).", True),
    "ashby": ("Company career sites – Ashby", "Jobs straight from companies that use Ashby (e.g. Wayve, Synthesia, OpenAI).", True),
    "workable": ("Company career sites – Workable", "Companies using Workable (e.g. Hugging Face, Moneyhub).", True),
    "smartrecruiters": ("Company career sites – SmartRecruiters", "Big employers on SmartRecruiters (e.g. ASOS, Wise, Experian).", True),
    "recruitee": ("Company career sites – Recruitee", "Companies using Recruitee (e.g. bunq).", True),
    "himalayas": ("Himalayas", "~100k remote jobs worldwide; keyword search supported.", True),
    "jobicy": ("Jobicy", "Remote jobs, filterable to the UK.", True),
    "weworkremotely": ("We Work Remotely", "Large remote-jobs board (RSS feed).", True),
    "remotive": ("Remotive", "Remote jobs worldwide (small free feed, about a day old).", True),
    "remoteok": ("Remote OK", "Popular remote-jobs board, mostly tech.", True),
    "arbeitnow": ("Arbeitnow", "Large job board, mostly Germany/EU plus remote roles.", True),
    "jsonld": ("Any careers page (paste links)", "Reads job details from careers pages you paste below.", True),
    "nhsjobs": ("NHS Jobs (UK)", "NHS and NHS-funded jobs in England & Wales (nurses, HCAs, admin, doctors…). "
                "Shows Permanent / Fixed-term / Bank / Locum. A locum / bank title asks for those contracts.", True),
    "nhsscotland": ("NHS Scotland Jobs", "NHS boards in Scotland (apply.jobs.scot.nhs.uk): staff bank, fixed-term and "
                    "permanent nursing posts, with pay, hours and closing date.", True),
    "hscni": ("HSC Northern Ireland jobs", "HSC trusts and GP federations in Northern Ireland (jobs.hscni.net).", True),
    "reed": ("Reed.co.uk (UK)", "Big UK job board – needs a free Reed API key.", True),
    "adzuna": ("Adzuna (UK)", "UK job search engine – needs a free Adzuna app ID + key.", True),
}

PRESETS = {name: slug for slug, name, _ in PRESET_COMPANIES}



def source_label(src: str) -> str:
    base, _, rest = src.partition(":")
    name = SOURCE_INFO.get(base, (base,))[0].replace("Company career sites – ", "")
    return f"{name} · {rest}" if rest else name


def jobs_to_frame(jobs: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(jobs, columns=Job.field_names())
    if df.empty:
        return df
    df.insert(3, "salary", [format_salary(r.salary_min, r.salary_max, r.salary_currency, r.salary_period)
                            for r in df.itertuples()])
    df["posted_date"] = pd.to_datetime(df["posted_date"], errors="coerce").dt.date
    df["where_found"] = df["source"].map(source_label)
    df["remote"] = df["remote"].map({True: "Yes", False: "No"}).fillna("")
    df["contract"] = [contract_bucket(t) for t in df["employment_type"]]
    df["closing_date"] = pd.to_datetime(df["closing_date"], errors="coerce").dt.date
    for c in ("company", "location", "employment_type", "contact_email", "contact_phone", "description", "hours"):
        df[c] = df[c].fillna("")
    return df


EXPORT_COLUMNS = {  # column -> friendly header (Excel/CSV). JSON keeps the plain keys.
    "title": "Job title", "company": "Company", "location": "Location", "salary": "Salary",
    "salary_min": "Salary min", "salary_max": "Salary max", "salary_currency": "Currency", "salary_period": "Salary period",
    "posted_date": "Posted", "employment_type": "Type", "remote": "Remote", "contact_email": "Contact email",
    "contact_phone": "Contact phone", "url": "Job link", "where_found": "Found on", "source": "Source id",
    "closing_date": "Closing date", "hours": "Hours / sessions", "salary_raw": "Pay as written", "description": "Description", "scraped_at": "Collected at (UTC)",
}
CONTACT_COLS = ("contact_email", "contact_phone")


def export_frame(df: pd.DataFrame, hide_contacts: bool = False, friendly: bool = True) -> pd.DataFrame:
    """Every useful column, in a sensible order, for downloads."""
    cols = [c for c in EXPORT_COLUMNS if c in df.columns and not (hide_contacts and c in CONTACT_COLS)]
    out = df[cols].copy()
    return out.rename(columns=EXPORT_COLUMNS) if friendly else out


def to_excel(df: pd.DataFrame) -> bytes:
    return to_excel_bytes(df, "Jobs", link_cols=("Job link", "url"),
                          wide_cols={"Job title": 45, "Description": 80, "Job link": 45, "Location": 30,
                                     "Contact email": 32, "Salary": 22})


def job_exports(hide_contacts: bool) -> dict:
    return {
        "excel": lambda d: to_excel(export_frame(d, hide_contacts)),
        "csv": lambda d: to_csv_bytes(export_frame(d, hide_contacts)),
        "json": lambda d: to_json_bytes(export_frame(d, hide_contacts, friendly=False), key="jobs",
                                        meta={"exported_at": datetime.now().isoformat(timespec="seconds")}),
    }

