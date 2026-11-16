"""Keep the offline test suite offline: the 'Employers to contact' step (NHS ODS + OpenStreetMap) is stubbed for
engine-level tests unless a test replaces it itself."""
import pytest


@pytest.fixture(autouse=True)
def _no_live_employer_leads(monkeypatch):
    from recruit_scraper.candidates import engine

    class _Off:
        notes, stats = [], {}

        def find(self, *a, **kw):
            return [], 0
    monkeypatch.setattr(engine, "EmployerLeadsFinder", lambda *a, **kw: _Off())


@pytest.fixture(autouse=True)
def _fresh_nhs_page_cache():
    """NHS Jobs feed pages are cached for 30 minutes in production; every test starts with an empty cache."""
    from recruit_scraper.sources import nhs
    nhs._PAGE_CACHE.clear()
    yield
    nhs._PAGE_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_live_locum_adverts(monkeypatch):
    """Employer-level tests never read NHS Jobs / NHS Scotland / HSC NI for locum adverts unless they replace this."""
    from recruit_scraper.candidates import locum
    monkeypatch.setattr(locum, "locum_adverts", lambda *a, **kw: [])
