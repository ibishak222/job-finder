from recruit_scraper.core import matches
from recruit_scraper.models import Job
from recruit_scraper.sources.more import JobicySource, RecruiteeSource, WorkableSource


class FakeHttp:
    def __init__(self, data):
        self.data = data

    def get_json(self, url, params=None, **kw):
        for k, v in self.data.items():
            if k in url:
                return v
        raise AssertionError(url)


def test_workable_and_recruitee_and_jobicy():
    wk = WorkableSource(FakeHttp({"workable.com": {"name": "Acme", "jobs": [{
        "title": "Data Engineer", "shortlink": "https://apply.workable.com/j/X", "published_on": "2026-11-01",
        "telecommuting": False, "employment_type": "Full-time",
        "locations": [{"city": "London", "region": "England", "country": "United Kingdom"}],
        "description": "<p>Salary £60,000 - £70,000. Questions to talent@acme.co.uk</p>"}]}}), companies=["acme"])
    j = next(wk.fetch())
    assert (j.company, j.location, j.employment_type) == ("Acme", "London, England, United Kingdom", "full-time")
    assert (j.salary_min, j.salary_max, j.contact_email) == (60000, 70000, "talent@acme.co.uk")

    rc = RecruiteeSource(FakeHttp({"recruitee.com": {"offers": [{
        "title": "QA", "company_name": "B", "location": "Leeds", "published_at": "2026-11-02 10:00:00 UTC",
        "description": "<p>Test things</p>", "careers_url": "https://b.recruitee.com/o/qa",
        "mailbox_email": "job.abc@b.recruitee.com", "salary": {"min": 30000, "max": 35000, "currency": "GBP",
                                                               "period": "year"}}]}}), companies=["b"])
    j = next(rc.fetch())
    assert j.contact_email == "job.abc@b.recruitee.com" and j.salary_currency == "GBP" and j.posted_date == "2026-11-02"

    jb = JobicySource(FakeHttp({"jobicy.com": {"jobs": [{
        "jobTitle": "Dev &amp; Ops", "companyName": "C", "jobGeo": "UK", "pubDate": "2026-11-03T08:00:00+00:00",
        "url": "https://jobicy.com/jobs/1", "jobDescription": "<p>Hi</p>", "jobType": ["Full-Time"]}]}}))
    j = next(jb.fetch())
    assert j.title == "Dev & Ops" and j.remote is True and j.location == "Remote - UK"


def test_remote_uk_matching():
    remote_uk = Job(title="Engineer", location="Remote - UK", remote=True, url="u")
    remote_us = Job(title="Engineer", location="Remote - USA only", remote=True, url="u")
    assert matches(remote_uk, ["engineer"], ["London"], remote_uk=True)
    assert not matches(remote_uk, ["engineer"], ["London"], remote_uk=False)
    assert not matches(remote_us, ["engineer"], ["London"], remote_uk=True)
