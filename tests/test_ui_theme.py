from datetime import date

import pandas as pd

import ui_theme as t
from ui_cards import employer_card, job_card, person_card


def test_safe_helpers_block_bad_values():
    assert t.safe_url("javascript:alert(1)") == ""
    assert t.safe_url("data:text/html,x") == ""
    assert t.safe_url('https://x.com/a"onmouseover=1') == ""
    assert t.safe_url("https://x.com/a?b=1") == "https://x.com/a?b=1"
    assert t.safe_email("a@b.co.uk") == "a@b.co.uk"
    assert t.safe_email("a@b.com<script>") == ""
    assert t.safe_tel("07700 900123") == "07700900123"
    assert t.safe_tel("+44 20 7946 0000") == "+442079460000"
    assert t.safe_tel("123") == ""


def test_clean_handles_missing_values():
    assert t.clean(None) == "" and t.clean(float("nan")) == "" and t.clean(pd.NaT) == ""
    assert t.closing_badge(pd.NaT) == ""


def test_closing_badge():
    today = date(2026, 11, 12)
    assert "Closed" in t.closing_badge("2026-11-01", today)
    assert "today" in t.closing_badge("2026-11-12", today)
    assert "Closes in 3 days" in t.closing_badge("2026-11-15", today)
    assert "Closes 30 Nov" in t.closing_badge("2026-11-30", today)


def test_contract_badge_kinds():
    assert t.contract_badge("", "", "Locum Practice Nurse")[1] == "locum"
    assert t.contract_badge("permanent", "", "Nurse")[1] == "perm"
    assert t.contract_badge("", "", "Bank Manager")[1] != "locum"
    assert t.contract_badge("", "", "Bank Nurse")[1] == "locum"


def test_cards_escape_scraped_text():
    evil = "<script>alert(1)</script>"
    row = {"title": evil, "company": evil, "location": evil, "url": "javascript:alert(1)", "description": evil,
           "contact_email": "x@y.com<b>", "contact_phone": "07700900123", "source": evil}
    for html in (job_card(row), person_card({"name": evil, "kind": "candidate", "bio": evil, "website": "javascript:x"}),
                 employer_card({"name": evil, "employer_type": evil, "website": "javascript:x", "address": evil}, False)):
        assert "<script>" not in html and "javascript:" not in html
        assert "&lt;script&gt;" in html
