"""Streamlit UI tests (offline: searches are stubbed and run inline instead of in a background thread)."""
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import recruit_scraper.core as core
import ui_common
from recruit_scraper.core import SearchResult, SourceResult
from recruit_scraper.models import Job

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")
sys.path.insert(0, str(ROOT))


def fake_search(sources, **kw):
    for s in sources:
        kw["progress"](s, "start", None)
        r = SourceResult(s, "skipped", message="add some companies") if s == "greenhouse" else SourceResult(s, "ok", 1, 1)
        kw["progress"](s, "done", r)
    return SearchResult(jobs=[Job(title="Data Engineer", company="Acme", location="London", salary_min=40000,
                                  salary_max=50000, salary_currency="GBP", salary_period="year",
                                  posted_date="2026-11-01", description="Build things", url="https://x.test/1",
                                  source="greenhouse:acme", remote=True, contact_email="talent@acme.test",
                                  contact_phone="+44 20 7946 0000"),
                              Job(title="Analyst", company="Beta", url="https://x.test/2", source="remoteok",
                                  description="=HYPERLINK(\"bad\")\x07", employment_type="locum")])


@pytest.fixture
def jobs_app(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "")  # no password gate in tests
    monkeypatch.setattr(core, "search", fake_search)
    monkeypatch.setattr(ui_common, "RUN_INLINE", True)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception
    return at


def _search(at):
    at.text_input(key="j_keywords").set_value("engineer")
    at.button(key="jobs_go").click()
    at.run()
    assert not at.exception, at.exception


def test_ui_renders_and_searches(jobs_app):
    at = jobs_app
    assert at.title[0].value.startswith("💼 Find jobs")
    _search(at)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Jobs shown"] == "2" and metrics["With salary"] == "1" and metrics["With contact email"] == "1"
    assert "skipped: add some companies" in " ".join(m.value for m in at.markdown)
    assert at.session_state["j_keywords"] == "engineer"   # inputs remembered


def test_results_survive_reruns_and_filters(jobs_app):
    at = jobs_app
    _search(at)
    at.toggle(key="j_f_salary").set_value(True).run()      # instant filter, no new search
    assert {m.label: m.value for m in at.metric}["Jobs shown"] == "1"
    # unrelated widget changes / reruns (e.g. what a download click triggers) keep the results
    at.toggle(key="j_hide_contacts").set_value(True).run()
    at.run()
    assert not at.exception
    m = {m.label: m.value for m in at.metric}
    assert m["Jobs shown"] == "1" and m["Found in total"] == "2" and m["With contact email"] == "hidden"
    assert len(at.session_state["results"]) == 2
    at.toggle(key="j_f_salary").set_value(False).run()
    at.radio(key="j_f_contract").set_value("Locum / temp / contract").run()   # permanent vs locum filter
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Jobs shown"] == "1"
    at.radio(key="j_f_contract").set_value("Any")
    at.toggle(key="j_f_salary").set_value(True)
    at.run()
    labels = [r.options for r in at.radio if r.key == "jobs_scope"]
    assert labels and labels[0] == ["Rows shown (1)", "All results (2)"]          # export-all offered while filtered



def test_password_gate(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert at.title[0].value.startswith("🔒")
    assert not any("Search jobs" in b.label for b in at.button)
    at.text_input[0].set_value("wrong")
    at.button[0].click().run()
    assert at.error
    at.text_input[0].set_value("s3cret")
    at.button[0].click().run()
    assert any("Search jobs" in b.label for b in at.button)


# ---------------------------------------------------------------- candidates page
def _cand_app():
    from candidates_page import candidates_page
    candidates_page()


def test_candidates_page(monkeypatch, tmp_path):
    import candidates_page as cp
    from recruit_scraper.candidates import suppression
    from recruit_scraper.candidates.engine import CandidateResult, ProviderReport
    from recruit_scraper.candidates.geo import Place
    from recruit_scraper.candidates.models import Candidate

    monkeypatch.setattr(suppression, "DEFAULT_PATH", tmp_path / "suppression.txt")
    monkeypatch.setattr(ui_common, "RUN_INLINE", True)

    seen = {}

    def fake_find(*a, **kw):
        seen.update(kw)
        kw["progress"]("Looking up postcode…")
        return CandidateResult(place=Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England"),
                               candidates=[Candidate(name="Test Person", location="Salford", distance_miles=2.5,
                                                     experience_years=6.1, experience_source="estimated: GitHub",
                                                     profile_url="https://github.com/test", hireable=True,
                                                     email="me@test.example", email_source="self-published on personal website https://t.example",
                                                     provider="github"),
                                           Candidate(name="Other", profile_url="https://github.com/other",
                                                     provider="github (quick list)", profile_read=False)],
                               reports=[ProviderReport("github", "ok", 2), ProviderReport("bluesky", "ok", 0)],
                               coverage="Free public sources cover jobs like this thinly. Use the Jobs page.")
    monkeypatch.setattr(cp, "find_candidates", fake_find)
    at = AppTest.from_function(_cand_app, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    at.text_input(key="c_title").set_value("python developer")
    at.text_input(key="c_postcode").set_value("M1 1AE")
    at.button(key="cand_go").click().run()
    assert not at.exception, at.exception
    m = {x.label: x.value for x in at.metric}
    assert m["People"] == "2" and m["With email"] == "1" and m["Available for hire"] == "1"
    assert any("Personal data" in w.value for w in at.warning)
    assert any("cover jobs like this thinly" in i.value for i in at.info)   # coverage note shown
    assert seen["providers"] == ["github", "bluesky", "mastodon", "openstreetmap", "job_ads"]  # all sources combined by default
    at.radio(key="c_f_show").set_value("Only with email").run()
    assert not at.exception
    at.radio(key="c_f_show").set_value("Businesses").run()
    at.radio(key="c_f_contract").set_value("Permanent").run()
    assert not at.exception
    at.toggle(key="c_f_contact").set_value(True).run()
    assert not at.exception, at.exception
    assert seen["enrich_top"] == 60 and seen["decode_obfuscated"] is True
    for how in ("Nearest", "Most experience", "Recently active", "Best match"):
        at.radio(key="c_f_sort").set_value(how).run()
        assert not at.exception, at.exception
    assert len(at.session_state["cand_results"]) == 2       # filter doesn't drop stored results
    assert any(r.key == "cands_scope" for r in at.radio)


def test_postcode_error_is_friendly(monkeypatch):
    import candidates_page as cp
    from recruit_scraper.candidates.geo import PostcodeError
    monkeypatch.setattr(ui_common, "RUN_INLINE", True)

    def bad(*a, **kw):
        raise PostcodeError("'ZZ1 1ZZ' isn't a valid UK postcode")
    monkeypatch.setattr(cp, "find_candidates", bad)
    at = AppTest.from_function(_cand_app, default_timeout=30)
    at.run()
    at.text_input(key="c_title").set_value("dev")
    at.text_input(key="c_postcode").set_value("ZZ1 1ZZ")
    at.button(key="cand_go").click().run()
    assert not at.exception
    assert any("valid UK postcode" in e.value for e in at.error)


def test_password_from_streamlit_secrets(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    at = AppTest.from_file(APP, default_timeout=30)
    at.secrets["APP_PASSWORD"] = "from-secrets"
    at.run()
    assert at.title[0].value.startswith("🔒")
    at.text_input[0].set_value("from-secrets")
    at.button[0].click().run()
    assert any("Search jobs" in b.label for b in at.button)


def test_cloud_without_password_fails_closed(monkeypatch, tmp_path):
    """No env var, no secret, no .app_password file, public bind -> locked with a 'set APP_PASSWORD' message."""
    import shutil
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    snap = tmp_path / "app"
    shutil.copytree(ROOT, snap, ignore=shutil.ignore_patterns(".venv", ".app_password", "data", "output", ".git",
                                                              "deploy", "__pycache__", ".pytest_cache"))
    at = AppTest.from_file(str(snap / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception
    assert at.title[0].value.startswith("🔒")
    assert any("APP_PASSWORD" in e.value for e in at.error)
    assert not any("Search jobs" in b.label for b in at.button)


def test_candidates_page_healthcare_extras(monkeypatch, tmp_path):
    import candidates_page as cp
    from recruit_scraper.candidates import suppression
    from recruit_scraper.candidates.employers import EmployerLead
    from recruit_scraper.candidates.engine import CandidateResult
    from recruit_scraper.candidates.geo import Place
    from recruit_scraper.candidates.models import Candidate

    monkeypatch.setattr(suppression, "DEFAULT_PATH", tmp_path / "suppression.txt")
    monkeypatch.setattr(ui_common, "RUN_INLINE", True)
    seen = {}

    def fake_find(*a, **kw):
        seen.update(kw)
        return CandidateResult(
            place=Place("M1 1AE", 53.48, -2.23, "Manchester", "North West", "England"),
            candidates=[Candidate(name="Sam Nurse", provider="bluesky", profile_url="https://bsky.app/profile/sam",
                                  message_url="https://bsky.app/profile/sam", message_via="Bluesky", match_score=3),
                        Candidate(name="Gemma Beardmore", provider="europepmc", email="gemma.beardmore@nhs.net",
                                  contact_source="https://europepmc.org/article/MED/1",
                                  profile_url="https://europepmc.org/article/MED/1", match_score=1.5)],
            research_note="Clinical researchers (Europe PMC): ... PECR ...",
            employers=[EmployerLead(name="Vallance Medical", employer_type="GP practice", phone="0161 000",
                                    source="NHS ODS", source_url="https://directory.spineservices.nhs.uk/x", sector="NHS",
                                    hires_locums=3, locum_evidence="Advertising now (NHS Jobs): Locum Practice Nurse",
                                    locum_evidence_url="https://www.jobs.nhs.uk/x"),
                       EmployerLead(name="Oak House", employer_type="Care home / nursing home", source="OpenStreetMap"),
                       EmployerLead(name="Harley Clinic", employer_type="Independent clinic", phone="020 7000", source="CQC",
                                    sector="Independent")],
            employers_note="3 employers near Manchester", agencies_removed=2)
    monkeypatch.setattr(cp, "find_candidates", fake_find)
    at = AppTest.from_function(_cand_app, default_timeout=30)
    at.run()
    at.text_input(key="c_title").set_value("practice nurse")
    at.text_input(key="c_postcode").set_value("M1 1AE")
    at.button(key="cand_go").click().run()
    assert not at.exception, at.exception
    assert seen["europepmc"] is False and seen["employers"] is True       # Europe PMC is off by default
    m = {x.label: x.value for x in at.metric}
    assert m["People"] == "1" and m["Clinical researchers"] == "1" and m["Message links"] == "1"
    assert any("PECR" in w.value for w in at.warning)
    assert any("Employers to contact (3)" in h.value for h in at.subheader)
    assert m["Independent"] == "1"
    assert any("2 recruitment / staffing / locum agencies left out" in c.value for c in at.caption)
    assert m["Hire locums"] == "1"
    html = " ".join(x.value for x in at.markdown)
    assert "Hires locums" in html and "Locum Practice Nurse" in html and "https://www.jobs.nhs.uk/x" in html
    at.toggle(key="c_emp_f_evid").set_value(True).run()
    assert not at.exception and any("Showing **1** of 3" in c.value for c in at.caption)
    at.toggle(key="c_emp_f_evid").set_value(False).run()
    at.session_state["c_emp_sector"] = "Independent"
    at.run()
    assert not at.exception and any("Showing **1** of 1" in c.value for c in at.caption)
    at.session_state["c_emp_sector"] = "All"
    at.run()
    at.radio(key="c_emp_type").set_value("GP practice").run()
    assert not at.exception
    for how in ("Message link only", "Clinical researchers"):
        at.radio(key="c_f_show").set_value(how).run()
        assert not at.exception, at.exception
    df = cp.export_frame(cp._frame(at.session_state["cand_results"]))
    assert "Message on Bluesky/Mastodon" in df.columns
    edf = cp._emp_frame(at.session_state["cand_employers"])
    csv = cp.emp_exports("q")["csv"](edf).decode("utf-8-sig")
    assert "Employer - not a candidate" in csv and "Vallance Medical" in csv


def test_nhs_independent_filter_badges_and_agency_note(monkeypatch):
    def nhs_search(sources, **kw):
        return SearchResult(jobs=[
            Job(title="Practice Nurse", company="Oaks Medical Centre", url="https://www.jobs.nhs.uk/a", source="nhsjobs",
                employment_type="permanent"),
            Job(title="Practice Nurse", company="Practice Plus Group", url="https://www.jobs.nhs.uk/b", source="nhsjobs"),
            Job(title="Practice Nurse", company="Spire Leeds", url="https://spire.test/c", source="jsonld"),
            Job(title="Practice Nurse", company="Acme Corp", url="https://x.test/d", source="remoteok")], agencies_removed=4)
    monkeypatch.setenv("APP_PASSWORD", "")
    monkeypatch.setattr(core, "search", nhs_search)
    monkeypatch.setattr(ui_common, "RUN_INLINE", True)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    _search(at)
    df = at.session_state["_jobs_df"][1]
    assert list(df["sector"]) != [] and set(df["sector"]) == {"NHS", "Independent", ""}
    assert any("4 ads posted by recruitment / staffing agencies left out" in c.value for c in at.caption)
    at.session_state["j_f_sector"] = "Independent"
    at.run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Jobs shown"] == "2"
    at.session_state["j_f_sector"] = "NHS"
    at.run()
    assert {m.label: m.value for m in at.metric}["Jobs shown"] == "1"
    html = " ".join(m.value for m in at.markdown)
    assert "jf-badge info" in html and "NHS" in html


def test_jobs_rate_stated_filter(monkeypatch):
    def rate_search(sources, **kw):
        return SearchResult(jobs=[
            Job(title="Locum Practice Nurse", company="Oaks", url="https://x.test/1", source="nhsjobs", salary_raw="£32 per hour"),
            Job(title="Locum GP", company="Elm", url="https://x.test/2", source="nhsjobs", salary_raw="£300 per session"),
            Job(title="Practice Nurse", company="Ash", url="https://x.test/3", source="nhsjobs", salary_raw="£30,000 a year")])
    monkeypatch.setenv("APP_PASSWORD", "")
    monkeypatch.setattr(core, "search", rate_search)
    monkeypatch.setattr(ui_common, "RUN_INLINE", True)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    _search(at)
    at.toggle(key="j_f_rate").set_value(True).run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Jobs shown"] == "2"
