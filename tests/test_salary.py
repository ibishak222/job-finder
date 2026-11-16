import pytest

from recruit_scraper.salary import find_salary_in_text, parse_salary


@pytest.mark.parametrize("text,lo,hi,cur,period", [
    ("£40,000 - £50,000 per annum", 40000, 50000, "GBP", "year"),
    ("$120k-$150k", 120000, 150000, "USD", "year"),
    ("£25/hour", 25, 25, "GBP", "hour"),
    ("£12.50 per hour", 12.5, 12.5, "GBP", "hour"),
    ("€50.000 – €60.000", 50000, 60000, "EUR", "year"),
    ("£450 - £550 per day", 450, 550, "GBP", "day"),
    ("£30-35k", 30000, 35000, "GBP", "year"),
    ("USD 90,000 to 110,000 per year", 90000, 110000, "USD", "year"),
    ("£3,000 pcm", 3000, 3000, "GBP", "month"),
])
def test_parse_ranges(text, lo, hi, cur, period):
    r = parse_salary(text)
    assert r["salary_min"] == lo
    assert r["salary_max"] == hi
    assert r["salary_currency"] == cur
    assert r["salary_period"] == period
    assert r["salary_raw"] == text


def test_up_to_and_from():
    r = parse_salary("Up to £60k + bonus")
    assert r["salary_min"] is None and r["salary_max"] == 60000
    r = parse_salary("From £40,000 DOE")
    assert r["salary_min"] == 40000 and r["salary_max"] is None


def test_non_salary_text():
    assert parse_salary("Competitive") is None
    assert parse_salary("") is None
    assert parse_salary(None) is None


def test_find_in_description_ignores_noise():
    text = ("You have 5+ years of experience and 100% enthusiasm. We offer £45,000 - £55,000 per annum, "
            "25 days holiday and a 2024 bonus scheme.")
    r = find_salary_in_text(text)
    assert (r["salary_min"], r["salary_max"], r["salary_currency"], r["salary_period"]) == (45000, 55000, "GBP", "year")
    assert find_salary_in_text("No pay info here, just 10 years of history.") is None
    assert find_salary_in_text("Home health is a $140B industry with less than $10B in EBITDA") is None
    assert find_salary_in_text("We raised £20m. Salary: £60,000 - £70,000")["salary_min"] == 60000


def test_european_decimal_formats():
    r = parse_salary("€ 5.082,02 brutto monatlich")
    assert r["salary_min"] == 5082.02 and r["salary_currency"] == "EUR"
    assert parse_salary("€12,50 per hour")["salary_min"] == 12.5


def test_employment_type_normalisation():
    from recruit_scraper.utils import normalise_employment_type as n
    assert n("FULL_TIME") == "full-time"
    assert n(["Unbefristeter Arbeitsvertrag", "Vollzeit"]) == "permanent, full-time"
    assert n("FullTime") == "full-time"
    assert n(None) is None


def test_format_salary():
    from recruit_scraper.display import format_salary as f
    assert f(40000, 50000, "GBP", "year") == "£40k–£50k / year"
    assert f(25, 25, "GBP", "hour") == "£25 / hour"
    assert f(None, 60000, "GBP", "year") == "up to £60k / year"
    assert f(211400, 290600, "USD", "year") == "$211.4k–$290.6k / year"
    assert f(52000, 56000, "CHF", "year") == "52k–56k CHF / year"
    assert f(None, None) == ""
    assert f(float("nan"), 30000.0, float("nan"), float("nan")) == "up to 30k"


def test_fix_mojibake():
    from recruit_scraper.utils import html_to_text
    assert html_to_text("<p>We\u00e2\u0080\u0099re hiring \u00e2\u0080\u0094 now</p>") == "We\u2019re hiring \u2014 now"
    assert html_to_text("Caf\u00e9 in M\u00fcnchen") == "Caf\u00e9 in M\u00fcnchen"


def test_find_phone_and_emails():
    from recruit_scraper.utils import find_emails, find_phone
    assert find_phone("Questions? Call Sam on 0161 496 0000.") == "0161 496 0000"
    assert find_phone("Tel: +44 (0)20 7946 0018") == "+44 (0)20 7946 0018"
    assert find_phone("Requisition 0161 4960000 2026") is None      # no phone context, no +
    assert find_emails("jobs@acme.co.uk, privacy@acme.co.uk, noreply@acme.co.uk, a@2x.png") == ["jobs@acme.co.uk"]
