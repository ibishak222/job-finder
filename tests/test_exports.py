"""Regression tests for downloads: filtered vs all, required columns, Excel/CSV/JSON details."""
import io
import json
import sys
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from recruit_scraper.models import Job  # noqa: E402


def _jobs_df():
    import jobs_view as ns
    jobs = [Job(title="Data Engineer", company="Acme £", location="London", salary_min=40000, salary_max=50000,
                salary_currency="GBP", salary_period="year", posted_date="2026-11-01", url="https://x.test/1",
                source="greenhouse:acme", remote=True, contact_email="talent@acme.test", contact_phone="+44 20 7946 0000",
                description="Build things\x07"),
            Job(title="=cmd()", company="Beta €", url="https://x.test/2", source="remoteok")]
    return ns, ns.jobs_to_frame([j.to_dict() for j in jobs])


REQUIRED_JOB_HEADERS = {"Job title", "Company", "Location", "Salary", "Salary min", "Salary max", "Posted",
                        "Contact email", "Contact phone", "Job link", "Found on", "Description"}


def test_job_excel_csv_json_complete():
    ns, df = _jobs_df()
    ex = ns.job_exports(False)
    wb = load_workbook(io.BytesIO(ex["excel"](df)))
    ws = wb["Jobs"]
    headers = [c.value for c in ws[1]]
    assert REQUIRED_JOB_HEADERS <= set(headers)
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref
    link = ws.cell(2, headers.index("Job link") + 1)
    assert link.hyperlink.target == "https://x.test/1"
    assert ws.cell(2, headers.index("Contact email") + 1).value == "talent@acme.test"
    assert ws.cell(3, headers.index("Job title") + 1).data_type == "s"          # not a formula
    assert ws.column_dimensions["A"].width >= 30

    csv = ex["csv"](df)
    assert csv.startswith(b"\xef\xbb\xbf")                                      # BOM for Excel
    back = pd.read_csv(io.BytesIO(csv), encoding="utf-8-sig")
    assert REQUIRED_JOB_HEADERS <= set(back.columns) and "Acme £" in back["Company"].tolist()
    assert back.loc[1, "Job title"] == "'=cmd()"                                # formula neutralised
    assert back.loc[0, "Contact phone"] == "+44 20 7946 0000"                   # phones untouched

    data = json.loads(ex["json"](df))
    assert data["count"] == 2 and data["jobs"][0]["posted_date"] == "2026-11-01"
    assert data["jobs"][0]["contact_email"] == "talent@acme.test" and data["jobs"][1]["salary_min"] is None


def test_job_export_filtered_vs_all_and_hidden_contacts():
    ns, df = _jobs_df()
    shown = df[df["salary"] != ""]
    ex = ns.job_exports(True)
    assert json.loads(ex["json"](shown))["count"] == 1
    assert json.loads(ex["json"](df))["count"] == 2
    headers = [c.value for c in load_workbook(io.BytesIO(ex["excel"](shown)))["Jobs"][1]]
    assert "Contact email" not in headers and "Contact phone" not in headers


def test_candidate_excel_has_personal_data_sheet():
    import candidates_page as cp
    from recruit_scraper.candidates.models import Candidate
    df = cp._frame([Candidate(name="A", email="a@x.example", email_source="self-published on personal website https://x.example",
                              phone="+44 161 000 0000", profile_url="https://github.com/a", website="https://x.example",
                              linkedin_url="https://www.linkedin.com/in/a", distance_miles=2.0, provider="github").to_dict()])
    wb = load_workbook(io.BytesIO(cp._excel(df)))
    assert wb.sheetnames == ["People", "Personal data"]
    ws = wb["People"]
    headers = [c.value for c in ws[1]]
    for h in ("Name", "Email", "Email source", "Phone", "Profile / ad link", "Website", "LinkedIn", "Miles away", "Found via"):
        assert h in headers
    assert ws.cell(2, headers.index("Profile / ad link") + 1).hyperlink.target == "https://github.com/a"
    assert "PERSONAL DATA" in wb["Personal data"]["A1"].value
    j = json.loads(cp.cand_exports("q")["json"](df))
    assert j["notice"].startswith("PERSONAL DATA") and j["candidates"][0]["phone"] == "+44 161 000 0000"
