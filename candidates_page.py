"""'Find candidates' page for the Job Finder Streamlit app. Output is PERSONAL DATA (UK GDPR)."""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd
import streamlit as st

from recruit_scraper.candidates import suppression
from recruit_scraper.candidates.employers import EmployerLead
from recruit_scraper.candidates.engine import EXPORT_DIR, SOURCE_LABELS, find_candidates, purge_old_exports
from recruit_scraper.candidates.geo import PostcodeError
from recruit_scraper.candidates.models import Candidate
from ui_cards import employer_card, person_card
from ui_common import (defaults, export_panel, finish_task, more_button, pager, show_notice, show_running, start_task,
                       to_csv_bytes, to_excel_bytes, to_json_bytes)
from ui_theme import clean, empty_state

GDPR_TEXT = """
**These results are personal data. UK GDPR applies.**

* **Lawful basis:** before you contact anyone, write a short *legitimate interests assessment*: why you need to
  contact them, why it's proportionate, and how you'll respect their rights.
* **Tell them (Article 14):** if you keep someone's details, tell them who you are, where you got their data and
  why, and how to opt out. Do this **within one month**, or at first contact if that's sooner.
* **Honour opt-outs straight away.** Add them to the *Do-not-contact list* below so they never appear again.
* **Keep only what you need**, for as short a time as you need it. Old exports are deleted automatically (see below).
* **Email marketing rules (PECR)** also apply to unsolicited emails, especially to personal addresses.
* **GitHub:** emails are *never* taken from GitHub. GitHub's Acceptable Use Policies forbid using GitHub data to send
  unsolicited emails to users, naming recruiters and headhunters. An email shown for a GitHub person was
  **self-published on their own website** (the source URL is recorded). Use it the way they invite on that site, and
  prefer people who marked themselves **Available for hire**.
* **Bluesky / Mastodon:** only public profiles whose own bio mentions the job and a nearby place. People are found
  through profile search, profession starter packs and custom feeds (Bluesky), and profile search on several UK and
  general servers plus the job's hashtag (Mastodon), but in every case their **own bio** must match. Mastodon results
  only include people who opted in to being discoverable, and Bluesky people who hid their profile from logged-out
  visitors are skipped. A Bluesky handle that is the person's own domain (e.g. *janesmith.co.uk*) counts as their
  website.
* **Contact details** are only ones the person **published themselves**: written in their own bio / profile fields,
  or on a page linked from their profile (their website, a link-in-bio page, or that site's contact/about page).
  Nothing is guessed. Addresses written as *name [at] domain [dot] com* are decoded the way a reader would (you can
  switch this off). Generic firm addresses (info@, enquiries@…) on an employer's site are listed as **businesses**,
  not as the person's email. The **Contact source** column records the page for every contact - keep it so you can
  tell people where you got their details. Everyone else: message them through their profile.
* **OpenStreetMap businesses** are firms and sole traders mapped by volunteers with the contact details published
  for the business (© OpenStreetMap contributors, ODbL). They are **businesses, not job seekers**; a sole trader's
  details are still personal data.
* **Team pages** are staff listings employers published on their own sites (only pages you paste; robots.txt
  respected). Use these business contacts in a way the person would reasonably expect.
* **Hiring contacts** are business addresses that employers published in their job ads or on their own careers and
  contact pages.
* **Clinical researchers (Europe PMC, off by default):** healthcare people near the postcode who co-authored recent
  research and wrote a correspondence email in **their own** author affiliation. Only an address that belongs to that
  author (their name is in it) is kept, and the article is shown as the source. These addresses were published
  **for research correspondence, not recruitment**: you need a lawful basis (legitimate interests assessment) and,
  for unsolicited email, **PECR consent or a soft opt-in**. Tell them where you found it and honour opt-outs.
* **Message links:** for people without an email, the *Message* column links to their own Bluesky / Mastodon
  profile so you can message them there yourself. Nothing is sent automatically; follow each site's rules and
  don't send bulk messages.
* **Employers to contact** (care, health, kitchen, school and family-law/barrister searches): GP practices, care
  homes, home-care providers, independent clinics, restaurants, schools, chambers and similar near the postcode, from official open-data
  registers - CQC, Care Inspectorate, NHS ODS, FSA food hygiene, DfE GIAS, Charity Commission, Bar Standards Board
  (all OGL) - and OpenStreetMap (ODbL). These are **organisations, not candidates**. Some rows also name a
  professional *from the register* (a registered manager, nurse prescriber, headteacher or barrister): they are
  **not job-seekers**, and the register is shown as the source.
* **CV databases:** for UK jobseekers who have opted in to be contacted, the proper route is a paid recruiter account
  on Reed, Totaljobs or CV-Library CV search.
"""


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=Candidate.field_names())
    if df.empty:
        return df
    df["hireable"] = df["hireable"].map({True: "Yes", False: "No"}).fillna("")
    df["experience"] = [
        (f"{y:g} yrs (estimated)" if "estimated" in (src or "") else f"{y:g} yrs") if pd.notna(y) else ""
        for y, src in zip(df["experience_years"], df["experience_source"])]
    for c in ("name", "headline", "company", "location", "email", "email_source", "website", "phone", "provider",
              "contact_source", "message_url", "message_via",
              "match_reason", "other_profiles", "contract", "fit_reason", "last_active",
              "linkedin_url", "twitter_url"):
        df[c] = df[c].fillna("")
    return df


EXPORT_COLUMNS = {
    "name": "Name", "kind": "Type", "headline": "Headline / bio", "company": "Company", "location": "Location",
    "distance_miles": "Miles away", "experience_years": "Experience (years)", "experience_source": "Experience source",
    "hireable": "Available for hire", "email": "Email", "email_source": "Email source", "phone": "Phone",
    "contact_source": "Contact source (page)",
    "profile_url": "Profile / ad link", "message_url": "Message on Bluesky/Mastodon", "message_via": "Message via", "website": "Website", "linkedin_url": "LinkedIn", "twitter_url": "X / Twitter",
    "fit": "Best-match score", "fit_reason": "Score breakdown", "last_active": "Last active",
    "contract": "Contract (from profile)", "provider": "Found via", "match_reason": "Why they match", "other_profiles": "Other profiles",
    "profile_read": "Profile read", "found_at": "Found at (UTC)", "personal_data": "Personal data",
}
LINK_HEADERS = ("Profile / ad link", "Message on Bluesky/Mastodon", "Website", "LinkedIn", "X / Twitter")
NOTICE = ("PERSONAL DATA - UK GDPR applies. Use only with a lawful basis (legitimate interests assessment), inform people "
          "within one month (Art. 14), honour opt-outs, and delete when no longer needed.")
NOTICE_LINES = [
    NOTICE, "",
    "Where the data came from:",
    "- GitHub: public profile fields only. Emails are never taken from GitHub (GitHub Acceptable Use Policies).",
    "- Bluesky / Mastodon: public bios that mention the job, found via profile search, profession starter packs / feeds"
    " or hashtags (Mastodon: only people who opted in to discovery).",
    "- OpenStreetMap: business / sole-trader listings (© OpenStreetMap contributors, ODbL) - businesses, not job seekers.",
    "- Europe PMC: research correspondence emails authors published in their own article affiliation (article link in"
    " 'Contact source'); published for research, not recruitment - PECR consent/soft opt-in needed for unsolicited email.",
    "- 'Message on Bluesky/Mastodon': the person's own public profile, for messaging them there by hand.",
    "- Team page: staff details the employer published on its own website.",
    "- 'self-published on personal website <url>': the person published it on their own site (source URL recorded).",
    "- 'published in their own Bluesky/Mastodon profile' / '... linked from their own profile': the person published it"
    " in their bio or on a page their profile links to (see 'Contact source (page)').",
    "- 'published by the employer…': business hiring contacts from job ads or company careers/contact pages.",
    "",
    "Before contacting anyone: check the do-not-contact list, follow PECR rules for unsolicited email, and tell them "
    "where you got their details and how to opt out.",
]


def export_frame(df: pd.DataFrame, friendly: bool = True) -> pd.DataFrame:
    cols = [c for c in EXPORT_COLUMNS if c in df.columns]
    out = df[cols].copy()
    return out.rename(columns=EXPORT_COLUMNS) if friendly else out


def _excel(df: pd.DataFrame) -> bytes:
    return to_excel_bytes(export_frame(df), "People", link_cols=LINK_HEADERS,
                          wide_cols={"Headline / bio": 50, "Email": 32, "Email source": 50, "Profile / ad link": 40,
                                     "Website": 32, "Experience source": 36},
                          notice_sheet=("Personal data", NOTICE_LINES))


def cand_exports(query: str) -> dict:
    """Builders for the download buttons (they run in a worker thread, so no Streamlit calls inside)."""
    return {
        "excel": _excel,
        "csv": lambda d: to_csv_bytes(export_frame(d)),
        "json": lambda d: to_json_bytes(export_frame(d, friendly=False), notice=NOTICE, key="candidates",
                                        meta={"query": query, "exported_at": datetime.now().isoformat(timespec="seconds")}),
    }

CAND_CHIPS = {   # label -> (job title, postcode / UK, contract mode)
    "🩺 Practice nurse locum · UK": ("practice nurse", "UK", "Locum / bank"),
    "🩺 Practice nurse · UK": ("practice nurse", "UK", "Any"),
    "🧑‍⚕️ HCA": ("HCA", "UK", "Any"),
    "🏡 Care worker": ("care worker", "UK", "Any"),
    "🧠 Mental health nurse": ("mental health nurse", "UK", "Any"),
    "🛏️ Nursing home nurse": ("nursing home nurse", "UK", "Any"),
    "🍎 Teacher": ("teacher", "UK", "Any"),
    "🍳 Chef": ("chef", "UK", "Any"),
    "⚡ Electrician": ("electrician", "UK", "Any"),
    "📊 Accountant": ("accountant", "UK", "Any"),
}
MODES = ["Any", "Permanent", "Locum / bank"]
_TEMP_WORD = re.compile(r"\b(locum|bank|sessional|agency|temp|temporary|ad[- ]?hoc)\b", re.I)


def _apply_chip() -> None:
    pick = st.session_state.get("c_chip")
    if pick in CAND_CHIPS:
        title, pc, mode = CAND_CHIPS[pick]
        st.session_state.update({"c_title": title, "c_postcode": pc, "c_mode": mode})
    st.session_state["c_chip"] = None


def with_mode(title: str, mode) -> str:
    """'Locum / bank' searches for the locum version of the role (the search engine puts urgent care, OOH, PCNs … first)."""
    if mode == "Locum / bank" and title and not _TEMP_WORD.search(title):
        return f"locum {title}"
    return title


CAND_DEFAULTS = {"c_emp_sector": "All", "c_mode": "Any", "c_chip": None, "c_emp_f_locum": False, "c_emp_f_email": False, "c_emp_f_phone": False,
                 "c_emp_f_page": False, "c_emp_sort": "Best match", "c_emp_type": "All", "c_github": True, "c_bsky": True, "c_masto": True, "c_osm": True, "c_epmc": False, "c_emp": True, "c_team_urls": "",
                 "c_f_contract": "Any", "c_uk_unknown": True, "c_enrich_n": 60, "c_obf": True, "c_f_contact": False, "c_f_near": False, "c_sites": True, "c_ads": True, "c_ads_presets": True, "c_hireable": False,
                 "c_unread": True, "c_unknown": False, "c_retention": 30, "c_max_profiles": 200, "c_max_accounts": 600,
                 "c_areas": 6, "c_wait": 0, "c_title": "", "c_postcode": "", "c_radius": 15, "c_exp": (3, 30),
                 "c_keywords": "", "c_f_sort": "Best match", "c_f_show": "Everyone", "c_f_text": "", "c_f_hire": False, "c_f_phone": False}


def candidates_page():
    defaults(CAND_DEFAULTS)
    finish_task("cand_task", _apply)
    st.title("🧑‍💼 Candidates & employers")
    st.caption("People near a UK postcode (or anywhere in the UK) from **lawful public sources**, plus the **employers to "
               "contact** for nursing, care, teaching, kitchen and trade roles, including the direct employers that take locum and bank staff (never other recruiters). "
               "No LinkedIn/Indeed/Facebook scraping, no guessed emails.")
    st.markdown('<div class="jf-gdpr">🔐 <b>Personal data · UK GDPR applies.</b> Every email and phone shown is one the '
                'person or organisation published themselves, with its source. Check the do-not-contact list before you '
                'get in touch.</div>', unsafe_allow_html=True)

    # ---- the search card
    with st.container(border=True, key="search_card"):
        st.caption("Quick start")
        st.pills("Quick start", list(CAND_CHIPS), key="c_chip", on_change=_apply_chip, label_visibility="collapsed",
                 selection_mode="single")
        c1, c2 = st.columns([2, 1])
        c1.text_input("Job title / skills", placeholder="e.g. practice nurse, care worker, electrician, python developer",
                      key="c_title")
        c2.text_input("Postcode or UK", placeholder="e.g. M1 1AE, or UK for everywhere", key="c_postcode",
                      help="A postcode (G1 1XQ, BT1 5GS, CF10 1EP, TR1 2HE …) or 'UK' to search England, Scotland, Wales "
                           "and Northern Ireland at once (no radius; a sample of the most contactable employers per area).")
        st.segmented_control("Contract", MODES, key="c_mode", selection_mode="single",
                             help="Locum / bank searches for the locum version of the role and puts the direct employers that take locum staff first: "
                                  "urgent care, out-of-hours providers, PCNs, GP federations, hospices and independent clinics. Recruitment agencies are never listed.")
        with st.expander("Refine · radius, experience, keywords"):
            c3, c4 = st.columns(2)
            c3.slider("Radius (miles)", 1, 50, key="c_radius",
                      help="Nearby areas are taken from about 15 miles around; distance is then checked against your radius.")
            c4.slider("Years of experience", 0, 30, key="c_exp",
                      help="For GitHub this is estimated from account age, so treat it as a rough signal. 30 = no maximum.")
            st.text_input("Extra keywords (optional)", placeholder="e.g. django, fintech", key="c_keywords",
                          help="Must appear in the person's bio/company. Commas = OR.")
        task = st.session_state.get("cand_task")
        running = bool(task and task.running)
        b1, b2 = st.columns([4, 1])
        go = b1.button("⏳ Searching…" if running else "🔎  Find people & employers", type="primary", width="stretch",
                       disabled=running, key="cand_go")
        if b2.button("🧹 Clear", width="stretch", key="cand_clear", help="Clear the results",
                     disabled=running or st.session_state.get("cand_results") is None):
            for k in ("cand_results", "cand_meta", "cand_reports", "cand_task_notice", "cand_coverage", "cand_contacts",
                      "cand_employers", "cand_research", "cand_emp_note"):
                st.session_state.pop(k, None)
            st.rerun()

    _sources_panel()

    # ---- the do-not-contact list stays one tap away (UK GDPR)
    with st.expander(f"🚫 Do-not-contact list · {len(suppression.load())} entries", expanded=False):
        st.caption(f"One email, @domain, profile URL or GitHub username per line. Saved to `{suppression.DEFAULT_PATH}`. "
                   "Anyone on this list is always removed from results.")
        txt = st.text_area("Do-not-contact list", value="\n".join(suppression.load()), height=120,
                           label_visibility="collapsed")
        up = st.file_uploader("…or upload a .txt/.csv list", type=["txt", "csv"])
        if st.button("Save list", key="cand_save_dnc"):
            entries = suppression.parse(txt)
            if up is not None:
                entries += suppression.parse(up.getvalue().decode("utf-8", "ignore").replace(",", "\n"))
            suppression.save(entries)
            st.success(f"Saved {len(set(entries))} entries.")
    with st.expander("⚖️ UK GDPR: read before you contact anyone", expanded=False):
        st.markdown(GDPR_TEXT)

    if go:
        ss = st.session_state
        team_urls = [u.strip() for u in ss["c_team_urls"].splitlines() if u.strip().startswith("http")]
        providers = ([p for p, k in (("github", "c_github"), ("bluesky", "c_bsky"), ("mastodon", "c_masto"),
                                     ("openstreetmap", "c_osm")) if ss[k]]
                     + (["team_pages"] if team_urls else []) + (["job_ads"] if ss["c_ads"] else []))
        title, postcode = with_mode(ss["c_title"].strip(), ss.get("c_mode")), ss["c_postcode"].strip()
        if not title or not postcode:
            st.error("Enter a job title and a UK postcode.")
        elif not providers:
            st.error("Tick at least one source under **Where to look & options**.")
        else:
            ss.pop("cand_task_notice", None)
            exp = ss["c_exp"]
            kwargs = dict(
                keywords=[k.strip() for k in ss["c_keywords"].split(",") if k.strip()], providers=providers,
                github_token=ss.get("GITHUB_TOKEN") or None, hireable_only=ss["c_hireable"],
                max_profiles=int(ss["c_max_profiles"]), max_towns=int(ss["c_areas"]),
                include_unknown_distance=ss["c_unknown"], include_unread=ss["c_unread"],
                fetch_websites=ss["c_sites"], max_wait_minutes=float(ss["c_wait"]),
                max_results=int(ss["c_max_accounts"]), all_presets=ss["c_ads_presets"], team_urls=team_urls,
                uk_unknown=ss["c_uk_unknown"], enrich_top=int(ss["c_enrich_n"]), enrich_seconds=120,
                decode_obfuscated=ss["c_obf"], europepmc=ss["c_epmc"], employers=ss["c_emp"],
                job_sources=["remotive", "arbeitnow", "remoteok", "himalayas", "jobicy"],
                companies=[l.strip() for l in ss.get("companies_text", "").splitlines() if l.strip()])
            radius, retention = float(ss["c_radius"]), int(ss["c_retention"])
            from recruit_scraper.candidates.geo import is_nationwide
            label = (f"'{title}' across the whole UK" if is_nationwide(postcode)
                     else f"'{title}' within {ss['c_radius']} miles of {postcode.upper()}")

            def run(t):
                t.purged = purge_old_exports(retention)
                return find_candidates(title, postcode, radius, exp[0] or None, exp[1] if exp[1] < 30 else None,
                                       progress=t.say, cancel=t.cancel, **kwargs)

            start_task(ss, "cand_task", label, run, friendly_errors={PostcodeError: ""})
            st.rerun()

    task = st.session_state.get("cand_task")
    if task and task.running:
        show_running("cand_task", f"Finding people and employers for {task.label}", expected_seconds=110)
    show_notice("cand_task")
    if st.session_state.get("cand_results") is None and not (task and task.running):
        st.markdown(empty_state("Search to see people and employers here", "Pick a quick-start chip or type a job title and a "
                                "postcode (or <b>UK</b>). For locum shifts choose <b>Locum / bank</b>: you get the direct employers that take locum staff: "
                                "urgent care, out-of-hours providers, PCNs, GP federations, hospices and independent clinics, with their published "
                                "contacts and <i>join our bank</i> pages. A search takes 1–2 minutes."),
                    unsafe_allow_html=True)
    _results()


def _apply(t) -> None:
    res = t.result
    st.session_state["cand_results"] = [c.to_dict() for c in res.candidates]
    st.session_state["cand_meta"] = f"{t.label} ({res.place.town})" if res.place else t.label
    lines_ = []
    for r in res.reports:
        icon = {"ok": "✅", "skipped": "⏭️"}.get(r.status, "❌")
        name = SOURCE_LABELS.get(r.name, r.name)
        lines_.append(f"{icon} **{name}**: {r.found} found" + (f" · ℹ️ {r.message}" if r.message else ""))
        if r.stats:
            lines_.append("<small>" + " · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in r.stats.items()) + "</small>")
    if res.suppressed:
        lines_.append(f"🚫 Removed {res.suppressed} people on your do-not-contact list")
    if getattr(t, "purged", 0):
        lines_.append(f"🗑️ Deleted {t.purged} old candidate export(s)")
    st.session_state["cand_reports"] = lines_
    st.session_state["cand_coverage"] = res.coverage
    st.session_state["cand_contacts"] = getattr(res, "contact_summary", "")
    st.session_state["cand_tech"] = res.tech_role
    st.session_state["cand_employers"] = [e.to_dict() for e in getattr(res, "employers", [])]
    st.session_state["cand_emp_note"] = getattr(res, "employers_note", "")
    st.session_state["cand_agencies"] = int(getattr(res, "agencies_removed", 0) or 0)
    st.session_state["c_emp_sector"] = "All"
    st.session_state["cand_research"] = getattr(res, "research_note", "")
    st.session_state.update({"c_f_show": "Everyone", "c_f_text": "", "c_f_hire": False, "c_f_phone": False,
                             "c_f_contract": "Any", "c_f_sort": "Best match", "c_f_near": False,
                             "c_f_contact": False})


_PARTIAL = re.compile(r"time limit|stopped at|didn't answer|did not answer|unavailable|too long|partial|skipped - the servers", re.I)


def _notes(*texts: str) -> list[str]:
    return [t for t in texts if t and _PARTIAL.search(t)]


def _results() -> None:
    rows = st.session_state.get("cand_results")
    if rows is None:
        return
    cached = st.session_state.get("_cand_df")
    if cached is None or cached[0] is not rows:
        cached = (rows, _frame(rows))
        st.session_state["_cand_df"] = cached
    df = cached[1]
    emp_rows = st.session_state.get("cand_employers") or []
    emp_df = _emp_frame(emp_rows) if emp_rows else None
    st.subheader(f"Results: {st.session_state.get('cand_meta', '')}")
    partial = _notes(st.session_state.get("cand_emp_note", ""), *(st.session_state.get("cand_reports") or []))
    if partial:
        st.markdown('<div class="jf-note">⚠️ <b>Partial results:</b> a source was slow or unavailable and was cut off at its '
                    'time limit, so the lists below may be shorter than they could be. Search again later, or see '
                    '<i>Search details</i> for what happened.</div>', unsafe_allow_html=True)
    st.warning("🔐 Personal data. Use only with a lawful basis, and don't share these exports.")
    with st.expander("ℹ️ About these results · sources, limits, notes"):
        if st.session_state.get("cand_coverage"):
            st.info("ℹ️ " + st.session_state["cand_coverage"])
        if st.session_state.get("cand_contacts"):
            st.success("📇 " + st.session_state["cand_contacts"])
        if st.session_state.get("cand_research"):
            st.warning("🔬 " + st.session_state["cand_research"])
        if st.session_state.get("cand_reports"):
            st.markdown("**Search details (sources, limits)**")
            st.markdown("  \n".join(st.session_state["cand_reports"]), unsafe_allow_html=True)

    people_label = f"🧑 People ({len(df)})"
    if emp_df is not None:
        tab_people, tab_emp = st.tabs([people_label, f"🏥 Employers ({len(emp_df)})"])
    else:
        (tab_people,), tab_emp = st.tabs([people_label]), None
    with tab_people:
        _people(df)
    if tab_emp is not None:
        with tab_emp:
            _employers(emp_df)


def _people(df: pd.DataFrame) -> None:
    if df.empty:
        st.markdown(empty_state("Nobody's public profile matched", "Try a bigger radius, a more common job title, or fewer "
                                "years of experience. The <b>Employers</b> tab (when shown) and the <b>Jobs</b> page have "
                                "hiring contacts too."), unsafe_allow_html=True)
        return
    summary_box = st.container(key="metrics")    # filled below, once the filters have been applied
    q = st.text_input("🔍 Filter these results", placeholder="name, company, location, bio or email", key="c_f_text")
    with st.expander("Filters & sort"):
        show = st.radio("Show", ["Everyone", "Only with email", "Message link only", "Candidates", "Clinical researchers",
                                 "Businesses", "Hiring contacts"],
                        horizontal=True, key="c_f_show",
                        help="Businesses = firms / sole traders listed in OpenStreetMap, not individual job seekers.")
        g1, g2 = st.columns(2)
        hire = g1.toggle("Only 'Available for hire'", key="c_f_hire")
        phone = g2.toggle("Only with phone", key="c_f_phone")
        near = g1.toggle("Only near the postcode", key="c_f_near",
                         help="Hide UK people who don't name a town near the postcode")
        has_contact = g2.toggle("Has contact (email or phone)", key="c_f_contact")
        contract = st.radio("Contract type", ["Any", "Permanent", "Locum / temp / contract"], horizontal=True,
                            key="c_f_contract",
                            help="Matches what the person's own profile says (e.g. 'locum', 'bank', 'agency', 'contract', "
                                 "'freelance' vs 'permanent'). Most profiles don't say, so this narrows a lot.")
        sort = st.radio("Sort by", ["Best match", "Nearest", "Most experience", "Recently active"], horizontal=True,
                        key="c_f_sort", help="Best match = job title in their own profile, distance, contact details, "
                                             "experience fit and recent activity (see 'Score breakdown').")

        def reset():
            st.session_state.update({"c_f_show": "Everyone", "c_f_text": "", "c_f_hire": False, "c_f_phone": False,
                                     "c_f_contract": "Any", "c_f_sort": "Best match", "c_f_near": False,
                                     "c_f_contact": False})
        st.button("Reset filters", on_click=reset, key="cand_reset_filters")

    view = df
    if show == "Only with email":
        view = view[view["email"] != ""]
    elif show == "Message link only":
        view = view[view["message_url"] != ""]
    elif show == "Candidates":
        view = view[view["kind"] == "candidate"]
    elif show == "Clinical researchers":
        view = view[view["provider"].str.contains("europepmc", regex=False)]
    elif show == "Businesses":
        view = view[view["kind"] == "business"]
    elif show == "Hiring contacts":
        view = view[view["kind"] == "hiring contact"]
    if contract != "Any":
        view = view[view["contract"] == ("permanent" if contract == "Permanent" else "temporary")]
    if q:
        ql = q.lower().strip()
        hay = (view["name"] + " " + view["company"] + " " + view["location"] + " " + view["headline"] + " "
               + view["email"]).str.lower()
        view = view[hay.str.contains(ql, regex=False)]
    if hire:
        view = view[view["hireable"] == "Yes"]
    if phone:
        view = view[view["phone"] != ""]
    if has_contact:
        view = view[(view["email"] != "") | (view["phone"] != "")]
    if near:
        view = view[~view["location"].str.contains("no nearby town", regex=False)]
    if sort == "Nearest":
        view = view.sort_values("distance_miles", na_position="last", kind="stable")
    elif sort == "Most experience":
        view = view.sort_values("experience_years", ascending=False, na_position="last", kind="stable")
    elif sort == "Recently active":
        view = view.sort_values("last_active", ascending=False, kind="stable")
    view = view.reset_index(drop=True)

    with summary_box:
        research = df["provider"] == "europepmc"     # research-only contacts, counted on their own
        n_research = int((research & (df["kind"] == "candidate")).sum())
        reach = ((df["email"] != "") | (df["phone"] != "") | (df["message_url"] != "")) & ~research
        m = st.columns(7 if n_research else 6)
        m[0].metric("People", int(((df["kind"] == "candidate") & ~research).sum()),
                    help="Individual job-seekers. The tab count also includes businesses, hiring contacts and researchers.")
        m[1].metric("Reachable", int(reach.sum()),
                    help="Have a published email or phone, or a Bluesky / Mastodon profile you can message")
        m[2].metric("With email", int(((df["email"] != "") & ~research).sum()),
                    help="Only emails people published themselves (bio, own site, link-in-bio page). "
                         "Clinical researchers' article emails are counted separately.")
        m[3].metric("Message links", int((df["message_url"] != "").sum()),
                    help="People without an email you can message on their own Bluesky/Mastodon profile")
        m[4].metric("Available for hire", int((df["hireable"] == "Yes").sum()))
        m[5].metric("Businesses", int((df["kind"] == "business").sum()),
                    help="Firms / sole traders (OpenStreetMap), not individual job seekers")
        if n_research:
            m[6].metric("Clinical researchers", n_research,
                        help="Europe PMC research contacts (corresponding authors), listed after the candidates")
    n_hiring = int((df["kind"] == "hiring contact").sum())
    if n_hiring:
        st.caption(f"Plus {n_hiring} hiring contacts published by employers.")
    if (df["provider"] == "openstreetmap").any():
        st.caption("Business listings: data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, ODbL.")
    st.caption(f"Showing **{len(view)}** of {len(df)} · 'Not read yet' rows only have a profile link (GitHub hourly limit).")

    if view.empty:
        st.markdown(empty_state("No one matches these filters", "Press <b>Reset filters</b> inside <i>Filters &amp; sort</i> "
                                "to see everyone again."), unsafe_allow_html=True)
    else:
        mode = st.segmented_control("View", ["Cards", "Table"], default="Cards", key="c_view", label_visibility="collapsed")
        if mode == "Table":
            _people_table(view)
        else:
            sig = f"{len(view)}|{q}|{show}|{contract}|{sort}|{hire}|{phone}|{near}|{has_contact}"
            n = pager("c_pager", sig, len(view))
            st.markdown("".join(person_card(r) for r in view.head(n).to_dict(orient="records")), unsafe_allow_html=True)
            more_button("c_pager", n, len(view), noun="people")

    export_panel("cands", view, df, "people", cand_exports(st.session_state.get("cand_meta", "")))
    st.caption(f"Downloads include every column (email, phone, source of each email, links, distance, experience) "
               f"and a 'Personal data' notice. CLI exports are saved under `{EXPORT_DIR}` and auto-deleted after "
               f"{int(st.session_state.get('c_retention', 30))} days.")


def _people_table(view: pd.DataFrame) -> None:
    st.caption("Click a column header to sort · hover the table for 🔍 search.")
    cols = ["name", "fit", "provider", "profile_url", "message_url", "message_via", "match_reason", "fit_reason", "email", "phone", "headline", "company", "location",
            "distance_miles", "experience", "contract", "hireable", "contact_source", "email_source", "website", "other_profiles",
            "linkedin_url", "twitter_url", "last_active", "kind", "found_at"]
    st.dataframe(view[cols], hide_index=True, width="stretch", height=min(560, 38 + 35 * len(view)),
                 key="cand_table", column_config={
        "name": st.column_config.TextColumn("Name", pinned=True),
        "profile_url": st.column_config.LinkColumn("Profile / ad", display_text="Open ↗", width="small"),
        "message_url": st.column_config.LinkColumn("Message", display_text="Message ↗",
                                                   help="No email? Message them on their own Bluesky / Mastodon "
                                                        "profile (opens the profile; nothing is sent automatically)"),
        "message_via": st.column_config.TextColumn("Message via", width="small"),
        "email": st.column_config.TextColumn("Email", width="large"),
        "phone": st.column_config.TextColumn("Phone", width="medium"),
        "headline": st.column_config.TextColumn("Headline / bio", width="large"),
        "company": "Company", "location": "Location",
        "distance_miles": st.column_config.NumberColumn("Miles away", format="%.1f"),
        "experience": st.column_config.TextColumn("Experience", help="GitHub values are estimates from account age"),
        "hireable": "Available for hire",
        "email_source": st.column_config.TextColumn("Email source", width="large"),
        "contact_source": st.column_config.LinkColumn("Contact source", help="The page where this person "
                                                      "published the email/phone (keep for UK GDPR)"),
        "website": st.column_config.LinkColumn("Website"), "kind": "Type",
        "provider": st.column_config.TextColumn("Found via", help="Which public source(s) this person came from"),
        "match_reason": st.column_config.TextColumn("Why they match", width="medium"),
        "fit": st.column_config.ProgressColumn("Best match", min_value=0, max_value=100, format="%.0f",
                                               help="0-100: job title in their own profile (45), distance (20), "
                                                    "contact details (20), experience fit (10), recent activity (5)"),
        "fit_reason": st.column_config.TextColumn("Score breakdown", width="medium"),
        "last_active": st.column_config.TextColumn("Last active", help="Last public post we saw (Mastodon, "
                                                   "Bluesky feeds); blank = not known"),
        "other_profiles": st.column_config.TextColumn("Other profiles", width="medium"),
        "contract": st.column_config.TextColumn("Contract", help="What their own profile says: temporary = "
                                                "locum / agency / bank / contract / freelance"),
        "linkedin_url": st.column_config.LinkColumn("LinkedIn", display_text="LinkedIn ↗"),
        "twitter_url": st.column_config.LinkColumn("X / Twitter", display_text="X ↗"),
        "found_at": "Found at (UTC)"})


EMPLOYER_COLUMNS = {"name": "Employer", "employer_type": "Type", "sector": "NHS / Independent", "contact_name": "Named in register",
                    "contact_role": "Their role (register)", "distance_miles": "Miles away", "address": "Address",
                    "postcode": "Postcode", "phone": "Phone", "email": "Email", "website": "Website",
                    "vacancy": "Open vacancy (own website)", "vacancy_type": "Vacancy type", "vacancy_url": "Vacancy link",
                    "bank_url": "Join the bank / locum page",
                    "careers_url": "Careers page", "source": "Source", "source_url": "Source record",
                    "ods_code": "NHS ODS code", "note": "Note"}
EMPLOYER_NOTICE = ("Employers to contact - organisations that employ this kind of worker, NOT candidates. People named "
                   "here come from an official register (registered manager, nurse prescriber, headteacher, "
                   "barrister) and are NOT job-seekers. Sources: CQC, Care Inspectorate, NHS Organisation Data "
                   "Service, Food Standards Agency, DfE GIAS, Charity Commission (all Open Government Licence v3.0), "
                   "Bar Standards Board Barristers' Register, OpenStreetMap (© OpenStreetMap contributors, ODbL). "
                   "Respect each person's objection to contact; UK GDPR/PECR apply.")


def _emp_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=EmployerLead.field_names())
    for c in ("name", "employer_type", "contact_name", "contact_role", "address", "postcode", "phone", "email",
              "website", "source", "source_url", "ods_code", "note", "careers_url", "vacancy", "vacancy_url", "vacancy_type", "bank_url", "sector"):
        df[c] = df[c].fillna("")
    return df


def emp_exports(query: str) -> dict:
    def friendly(d):
        return d[[c for c in EMPLOYER_COLUMNS if c in d.columns]].rename(columns=EMPLOYER_COLUMNS)
    return {
        "excel": lambda d: to_excel_bytes(friendly(d), "Employers", link_cols=("Website", "Source record", "Vacancy link", "Careers page", "Join the bank / locum page"),
                                          wide_cols={"Employer": 36, "Address": 44, "Source record": 50},
                                          notice_sheet=("About", [EMPLOYER_NOTICE])),
        "csv": lambda d: to_csv_bytes(friendly(d)),
        "json": lambda d: to_json_bytes(d[[c for c in EMPLOYER_COLUMNS if c in d.columns]], notice=EMPLOYER_NOTICE,
                                        key="employers", meta={"query": query,
                                                               "exported_at": datetime.now().isoformat(timespec="seconds")}),
    }


def _locum_friendly(df: pd.DataFrame) -> pd.Series:
    """Employers a locum / bank worker is booked through (urgent care, OOH, PCNs / federations, hubs, hospices, trust banks)."""
    from recruit_scraper.candidates.employers import LOCUM_EMPLOYER
    hay = df["employer_type"] + " " + df["name"]
    return hay.map(lambda t: bool(LOCUM_EMPLOYER.search(t)))


EMP_SORTS = ["Best match", "Nearest", "Most contactable", "Name A–Z"]


def _employers(df: pd.DataFrame) -> None:
    st.subheader(f"🏥 Employers to contact ({len(df)})")
    n_named = int((df["contact_name"] != "").sum())
    st.caption("**Organisations, not candidates**: places that employ this kind of worker. Ask about vacancies, "
               "bank / locum staff or referrals. " + (st.session_state.get("cand_emp_note") or ""))
    df = df.assign(_locum=_locum_friendly(df),
                   _reach=(df["phone"] != "").astype(int) + (df["email"] != "").astype(int)
                   + (df["bank_url"] != "").astype(int) + (df["careers_url"] != "").astype(int))
    locum_vac = df["vacancy_type"].str.startswith("locum")
    with st.container(key="metrics_emp"):
        m = st.columns(7)
        m[0].metric("Employers", len(df))
        m[1].metric("Employers with phone", int((df["phone"] != "").sum()))
        m[2].metric("Employers with email", int((df["email"] != "").sum()))
        m[3].metric("Locum employers", int(df["_locum"].sum()),
                    help="Urgent care, out-of-hours providers, PCNs, GP federations, hubs, hospices, independent clinics (direct employers only; recruitment agencies are never listed)")
        m[4].metric("Locum vacancies", int(locum_vac.sum()),
                    help="Locum / bank / temporary adverts read from the employers' own websites")
        m[5].metric("Join-the-bank pages", int((df["bank_url"] != "").sum()))
        m[6].metric("Independent", int((df["sector"] == "Independent").sum()),
                    help="Private / independent clinics, hospitals and GP services from the regulators' registers "
                         f"(CQC, HIW, RQIA). NHS-listed: {int((df['sector'] == 'NHS').sum())}")

    if (df["sector"] != "").any():
        st.segmented_control("NHS or independent", ["All", "NHS", "Independent"], key="c_emp_sector", selection_mode="single",
                             help="NHS = NHS-contracted GP practices and NHS bodies (NHS ODS, PHS, HSC BSO lists); "
                                  "Independent = private / independent clinics, hospitals and GP services from the "
                                  "regulators' registers (CQC, Healthcare Inspectorate Wales, RQIA) and dental practices.")
    sector = st.session_state.get("c_emp_sector") or "All"
    if sector != "All":
        df = df[df["sector"] == sector].reset_index(drop=True)
    kinds = ["All"] + list(dict.fromkeys(df["employer_type"]))
    if st.session_state.get("c_emp_type") not in kinds:
        st.session_state["c_emp_type"] = "All"
    st.radio("Type", kinds, horizontal=True, key="c_emp_type")
    pick = st.session_state.get("c_emp_type", "All")
    with st.expander("Filters & sort"):
        g1, g2 = st.columns(2)
        f_locum = g1.toggle("Locum / bank friendly only", key="c_emp_f_locum",
                            help="Agencies, urgent care, out-of-hours, PCNs, GP federations, hubs, or a locum / bank page")
        f_page = g2.toggle("Has a vacancy, bank or careers page", key="c_emp_f_page")
        f_email = g1.toggle("Has an email", key="c_emp_f_email")
        f_phone = g2.toggle("Has a phone number", key="c_emp_f_phone")
        sort = st.selectbox("Sort by", EMP_SORTS, key="c_emp_sort")
    view = df if pick == "All" else df[df["employer_type"] == pick]
    if f_locum:
        view = view[view["_locum"] | (view["bank_url"] != "") | view["vacancy_type"].str.startswith("locum")]
    if f_page:
        view = view[(view["vacancy"] != "") | (view["bank_url"] != "") | (view["careers_url"] != "")]
    if f_email:
        view = view[view["email"] != ""]
    if f_phone:
        view = view[view["phone"] != ""]
    if sort == "Nearest":
        view = view.sort_values("distance_miles", na_position="last", kind="stable")
    elif sort == "Most contactable":
        view = view.sort_values("_reach", ascending=False, kind="stable")
    elif sort == "Name A–Z":
        view = view.sort_values("name", key=lambda s_: s_.str.lower(), kind="stable")
    view = view.reset_index(drop=True)
    n_vac = int((df["vacancy"] != "").sum())
    n_car = int((df["careers_url"] != "").sum())
    n_bank = int((df["bank_url"] != "").sum())
    n_loc = int(locum_vac.sum())
    if n_vac or n_car or n_bank:
        st.caption(f"**{n_vac}** advertise this role on their own website right now (listed first; **{n_loc}** of them "
                   f"locum / bank / temporary), **{n_car}** have a careers page and **{n_bank}** a 'join our bank' / locum "
                   "page - all read from the employer's own site (robots.txt respected).")
    if n_named:
        st.caption(f"**{n_named}** rows name a professional from an official register (registered manager, nurse "
                   "prescriber, headteacher, barrister) - **not job-seekers**. Approach them as employers / for "
                   "referrals, and stop if they object.")
    n_ag = int(st.session_state.get("cand_agencies") or 0)
    if n_ag:
        st.caption(f"🚫 {n_ag} recruitment / staffing / locum agenc{'ies' if n_ag != 1 else 'y'} left out: Job Finder "
                   "lists direct employers only.")
    st.caption(f"Showing **{len(view)}** of {len(df)}.")
    if view.empty:
        st.markdown(empty_state("No employers match these filters", "Turn a filter off inside <i>Filters &amp; sort</i>, or "
                                "pick <b>All</b> types."), unsafe_allow_html=True)
    else:
        mode = st.segmented_control("View", ["Cards", "Table"], default="Cards", key="c_emp_view", label_visibility="collapsed")
        if mode == "Table":
            _employer_table(view, df, n_named, n_vac, n_car, n_bank)
        else:
            sig = f"{len(view)}|{pick}|{sort}|{f_locum}|{f_page}|{f_email}|{f_phone}"
            n = pager("emp_pager", sig, len(view), step=24)
            rows = view.head(n).to_dict(orient="records")
            st.markdown("".join(employer_card({k: v for k, v in r.items() if not k.startswith("_")},
                                              locum_friendly=bool(r["_locum"])) for r in rows), unsafe_allow_html=True)
            more_button("emp_pager", n, len(view), step=24, noun="employers")
    from recruit_scraper.candidates.registers import LICENCES
    used = [v for k, v in LICENCES.items() if df["source"].str.contains(k, regex=False).any()]
    if df["source"].str.contains("OpenStreetMap", regex=False).any():
        used.append("© [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, ODbL")
    st.caption("Data: " + " · ".join(used or ["NHS Organisation Data Service (Open Government Licence v3.0)"]) + ".")
    clean_view = view.drop(columns=[c for c in view.columns if c.startswith("_")])
    clean_all = df.drop(columns=[c for c in df.columns if c.startswith("_")])
    export_panel("emps", clean_view, clean_all, "employers", emp_exports(st.session_state.get("cand_meta", "")))


def _employer_table(view, df, n_named, n_vac, n_car, n_bank) -> None:
    cols = ["name", "employer_type"] + (["contact_name", "contact_role"] if n_named else []) + [
        "distance_miles", "phone", "website", "email"] + (["vacancy", "vacancy_type", "vacancy_url"] if n_vac else []) + (
        ["bank_url"] if n_bank else []) + (["careers_url"] if n_car else []) + ["address", "postcode", "source", "source_url"]
    st.dataframe(view[cols], hide_index=True, width="stretch",
                 height=min(460, 38 + 35 * len(view)), key="emp_table", column_config={
                     "name": st.column_config.TextColumn("Employer", pinned=True), "employer_type": "Type",
                     "contact_name": st.column_config.TextColumn("Named in register",
                                                                 help="From the official register - not a job-seeker"),
                     "contact_role": "Their role (register)",
                     "distance_miles": st.column_config.NumberColumn("Miles away", format="%.1f"),
                     "phone": "Phone", "website": st.column_config.LinkColumn("Website"), "email": "Email",
                     "vacancy": st.column_config.TextColumn("Open vacancy (own website)", width="medium"),
                     "vacancy_url": st.column_config.LinkColumn("Vacancy", display_text="Open ↗"),
                     "careers_url": st.column_config.LinkColumn("Careers page", display_text="Careers ↗"),
                     "vacancy_type": "Vacancy type",
                     "bank_url": st.column_config.LinkColumn("Join the bank / locum", display_text="Bank / locum ↗"),
                     "address": st.column_config.TextColumn("Address", width="large"), "postcode": "Postcode",
                     "source": "Source", "source_url": st.column_config.LinkColumn("Source record",
                                                                                   display_text="Record ↗")})


def _sources_panel() -> None:
    """Where to look and the other options. Folded away so the search stays simple (and phone-sized)."""
    with st.expander("⚙️ Where to look & options · sources, filters, GitHub token"):
        st.markdown("##### Where to look")
        st.checkbox("GitHub public profiles", key="c_github",
                    help="Developers/data people whose public GitHub location is near the postcode. Skipped "
                         "automatically for non-software jobs.")
        st.checkbox("Bluesky public profiles", key="c_bsky",
                    help="People whose public Bluesky bio mentions the job and a place near the postcode. Any job. Found via "
                         "profile search (the job + your town and nearby areas), profession starter packs and "
                         "profession custom feeds (e.g. 'NurseSky').")
        st.checkbox("Mastodon public profiles", key="c_masto",
                    help="People on Mastodon who opted in to being discoverable and whose bio mentions the job and "
                         "a nearby place. Any job. Searches ~15 UK and general servers (UK regional servers from "
                         "joinmastodon.org) plus recent posts under the job's hashtag, e.g. #nurse.")
        st.checkbox("OpenStreetMap businesses (trades, accountants, lawyers…)", key="c_osm",
                    help="Firms and sole traders mapped in OpenStreetMap for this kind of work (e.g. electricians, "
                         "plumbers, accountants, law firms) with the contact details published for them. Shown as "
                         "'business', not job seekers. Data © OpenStreetMap contributors (ODbL).")
        st.toggle("Clinical researchers (Europe PMC) - healthcare jobs", key="c_epmc",
                  help="Off by default. Healthcare people near the postcode who co-authored research in the last 5 "
                       "years and published a correspondence email in their own author affiliation (free Europe PMC "
                       "API, a couple of requests). Only addresses that belong to that author are kept, with the "
                       "article as the source. Published for research correspondence, not recruitment: PECR "
                       "consent / soft opt-in applies before emailing them.")
        st.toggle("Employers to contact (GP practices, care homes, schools…)", key="c_emp",
                  help="Care, health, chef/kitchen, teacher, trades, accountant, solicitor and barrister searches: employers near the "
                       "postcode from official registers (CQC, Care Inspectorate, NHS ODS, FSA, DfE GIAS, Charity "
                       "Commission, Bar Standards Board) and OpenStreetMap, with their published phone / website / "
                       "email. Shown separately - organisations and register-named professionals, not candidates. "
                       "The first search in an area downloads the registers (up to ~2 minutes); then cached 7 days.")
        st.toggle("Also show UK people who don't name a nearby town", key="c_uk_unknown",
                  help="Bluesky/Mastodon people whose bio matches the job and is clearly UK-based (e.g. mentions the "
                       "NHS, 'UK', a UK body like the NMC/SRA, or has a .uk handle) but doesn't name a town near the "
                       "postcode. Shown with location 'UK - no nearby town mentioned' and ranked below local people.")
        st.markdown("**Team / 'our people' pages (optional)**")
        with st.container(border=True):
            st.text_area("Paste firm or chambers 'our people' page links, one per line", key="c_team_urls",
                         height=90, placeholder="https://www.example-law.co.uk/our-people")
            st.caption("Reads the staff details the employer published as schema.org data (robots.txt and noindex "
                       "respected); only people whose job title matches are kept.")
        st.toggle("Check the links on their profiles for contact details", key="c_sites",
                  help="Follows the links on each person's own profile (their website, own-domain handle, "
                       "link-in-bio pages like bio.link / beacons / carrd / about.me, plus one hop to a contact or "
                       "about page) and records an email/phone THEY published there, with the page it came from. "
                       "robots.txt and noindex respected; Linktree forbids crawling so it is never read.")
        st.slider("…for the best-matching people (top N)", 0, 200, step=10, key="c_enrich_n",
                  help="Contact check runs for this many people without an email, best matches first, within "
                       "about 2 minutes. 0 = off.")
        st.toggle("Read emails written as 'name [at] domain [dot] com'", key="c_obf",
                  help="People often write their address this way so spam bots can't read it. A human reader "
                       "would, so this decodes it (only on their own pages / bio). Turn off to skip such addresses.")
        st.checkbox("Hiring contacts published by employers", key="c_ads",
                    help="Recruiter/hiring emails employers published in job ads, plus careers@/jobs@ style "
                         "addresses on company careers/contact pages. Business contacts, not candidates.")
        st.toggle("…include all preset UK-hiring companies", key="c_ads_presets")
        st.markdown("##### Search filters")
        st.toggle("Only people marked 'Available for hire'", key="c_hireable",
                  help="GitHub users can tick 'Available for hire' on their profile.")
        st.toggle("Include accounts not read yet (quick list)", key="c_unread",
                  help="Without a token GitHub allows ~60 profile reads per hour. Accounts found by the "
                       "search but not read yet are still listed with their profile link.")
        st.toggle("Include people with unclear location", key="c_unknown")
        st.number_input("Delete saved candidate exports after (days)", 1, 365, key="c_retention")
        st.markdown("**Advanced**")
        with st.container(border=True):
            st.slider("Max new GitHub profiles to read", 10, 1000, step=10, key="c_max_profiles",
                      help="Profiles already read in the last 7 days come from the cache for free.")
            st.slider("Max GitHub accounts to collect from search", 100, 2000, step=100, key="c_max_accounts")
            st.slider("Max nearby areas to search", 1, 12, key="c_areas")
            st.slider("If GitHub's hourly limit is hit, wait up to (minutes)", 0, 60, key="c_wait",
                      help="0 = stop and list the rest as 'not read yet'.")
            st.text_input("GitHub token (optional)", type="password", key="GITHUB_TOKEN",
                          help="Not required. A free token raises GitHub's limit from ~60 to 5,000 profile reads an hour. "
                               "Kept for this session only.")
            if st.button("Clear cached profiles/websites", key="cand_clear_cache"):
                from recruit_scraper.candidates.cache import clear_all
                st.success(f"Deleted {clear_all()} cached files.")
