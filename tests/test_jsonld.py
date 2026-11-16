from recruit_scraper.sources.jsonld import extract_jobpostings, jobposting_to_job

PAGE = """
<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme"}</script>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebPage", "name": "Careers"},
  {"@type": "JobPosting",
   "title": "Senior Data Engineer",
   "description": "<p>Build pipelines.</p><ul><li>Python</li><li>SQL</li></ul><p>Apply: jobs@acme.co.uk</p>",
   "datePosted": "2026-10-01T09:00:00+01:00",
   "employmentType": "FULL_TIME",
   "hiringOrganization": {"@type": "Organization", "name": "Acme Ltd"},
   "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
        "addressLocality": "London", "addressRegion": "England", "addressCountry": "GB"}},
   "baseSalary": {"@type": "MonetaryAmount", "currency": "GBP",
        "value": {"@type": "QuantitativeValue", "minValue": 60000, "maxValue": 75000, "unitText": "YEAR"}},
   "url": "/careers/senior-data-engineer"}
]}
</script>
<script type="application/ld+json">
[{"@type": ["JobPosting"], "title": "Support Agent", "jobLocationType": "TELECOMMUTE",
  "hiringOrganization": "Acme Ltd", "datePosted": "2026-10-05",
  "description": "Help customers. Pay: £25/hour."}]
</script>
<script type="application/ld+json">{ this is broken json </script>
</head><body></body></html>
"""


def test_extract_jobpostings_handles_graph_lists_and_bad_blocks():
    jps = extract_jobpostings(PAGE)
    assert [j["title"] for j in jps] == ["Senior Data Engineer", "Support Agent"]


def test_jobposting_to_job_maps_fields():
    jp = extract_jobpostings(PAGE)[0]
    job = jobposting_to_job(jp, "https://acme.co.uk/careers", source="jsonld:acme.co.uk")
    assert job.title == "Senior Data Engineer"
    assert job.company == "Acme Ltd"
    assert job.location == "London, England, GB"
    assert job.posted_date == "2026-10-01"
    assert job.employment_type == "full-time"
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (60000, 75000, "GBP", "year")
    assert job.url == "https://acme.co.uk/careers/senior-data-engineer"
    assert "Build pipelines." in job.description and "<p>" not in job.description
    assert job.contact_email == "jobs@acme.co.uk"
    assert job.remote is None


def test_remote_and_salary_from_description():
    jp = extract_jobpostings(PAGE)[1]
    job = jobposting_to_job(jp, "https://acme.co.uk/careers/support")
    assert job.remote is True
    assert job.location == "Remote"
    assert job.company == "Acme Ltd"
    assert (job.salary_min, job.salary_currency, job.salary_period) == (25, "GBP", "hour")
    assert job.url == "https://acme.co.uk/careers/support"


MICRODATA = """
<html><body>
<div itemscope itemtype="https://schema.org/JobPosting">
  <h1 itemprop="title">Warehouse Supervisor</h1>
  <div itemprop="hiringOrganization" itemscope itemtype="https://schema.org/Organization">
    <span itemprop="name">Northern Logistics Ltd</span></div>
  <div itemprop="jobLocation" itemscope itemtype="https://schema.org/Place">
    <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
      <span itemprop="addressLocality">Leeds</span><span itemprop="addressCountry">GB</span></div></div>
  <meta itemprop="datePosted" content="2026-10-20">
  <meta itemprop="employmentType" content="FULL_TIME">
  <div itemprop="description"><p>Lead a team of 12. Salary £32,000 - £36,000 per annum.</p></div>
</div>
</body></html>
"""


def test_microdata_jobposting_via_extruct():
    jps = extract_jobpostings(MICRODATA, "https://careers.example.co.uk/jobs/1")
    assert len(jps) == 1
    job = jobposting_to_job(jps[0], "https://careers.example.co.uk/jobs/1")
    assert job.title == "Warehouse Supervisor"
    assert job.company == "Northern Logistics Ltd"
    assert job.location.startswith("Leeds")
    assert job.posted_date == "2026-10-20"
    assert (job.salary_min, job.salary_max, job.salary_currency) == (32000, 36000, "GBP")


def test_relative_dates_via_dateparser():
    from datetime import date, timedelta
    from recruit_scraper.utils import to_iso_date
    assert to_iso_date("Posted 3 days ago") == (date.today() - timedelta(days=3)).isoformat()
    assert to_iso_date("12 March 2026") == "2026-03-12"
    assert to_iso_date("12. März 2026") == "2026-03-12"
    assert to_iso_date("03/04/2026") == "2026-04-03"  # UK day-first
