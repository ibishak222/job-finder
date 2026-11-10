"""'Find candidates' page for the Job Finder Streamlit app. Output is PERSONAL DATA (UK GDPR)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from recruit_scraper.candidates import suppression
from recruit_scraper.candidates.engine import EXPORT_DIR, SOURCE_LABELS, find_candidates, purge_old_exports
from recruit_scraper.candidates.geo import PostcodeError
from recruit_scraper.candidates.models import Candidate
from ui_common import (STICKY_CSS, defaults, export_panel, finish_task, show_notice, show_running, start_task, tip,
                       to_csv_bytes, to_excel_bytes, to_json_bytes)

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
* **Bluesky / Mastodon:** only public profiles whose own bio mentions the job and a nearby place. Emails are never
  lifted from bios; Mastodon results only include people who opted in to being discoverable, and Bluesky people who
  hid their profile from logged-out visitors are skipped. Contact them through their profile or their own website.
* **OpenStreetMap businesses** are firms and sole traders mapped by volunteers with the contact details published
  for the business (© OpenStreetMap contributors, ODbL). They are **businesses, not job seekers**; a sole trader's
  details are still personal data.
* **Team pages** are staff listings employers published on their own sites (only pages you paste; robots.txt
  respected). Use these business contacts in a way the person would reasonably expect.
* **Hiring contacts** are business addresses that employers published in their job ads or on their own careers and
  contact pages.
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
              "match_reason", "other_profiles", "contract",
              "linkedin_url", "twitter_url"):
        df[c] = df[c].fillna("")
    return df


EXPORT_COLUMNS = {
    "name": "Name", "kind": "Type", "headline": "Headline / bio", "company": "Company", "location": "Location",
    "distance_miles": "Miles away", "experience_years": "Experience (years)", "experience_source": "Experience source",
    "hireable": "Available for hire", "email": "Email", "email_source": "Email source", "phone": "Phone",
    "profile_url": "Profile / ad link", "website": "Website", "linkedin_url": "LinkedIn", "twitter_url": "X / Twitter",
    "contract": "Contract (from profile)", "provider": "Found via", "match_reason": "Why they match", "other_profiles": "Other profiles",
    "profile_read": "Profile read", "found_at": "Found at (UTC)", "personal_data": "Personal data",
}
LINK_HEADERS = ("Profile / ad link", "Website", "LinkedIn", "X / Twitter")
NOTICE = ("PERSONAL DATA - UK GDPR applies. Use only with a lawful basis (legitimate interests assessment), inform people "
          "within one month (Art. 14), honour opt-outs, and delete when no longer needed.")
NOTICE_LINES = [
    NOTICE, "",
    "Where the data came from:",
    "- GitHub: public profile fields only. Emails are never taken from GitHub (GitHub Acceptable Use Policies).",
    "- Bluesky / Mastodon: public bios that mention the job (Mastodon: only people who opted in to discovery).",
    "- OpenStreetMap: business / sole-trader listings (© OpenStreetMap contributors, ODbL) - businesses, not job seekers.",
    "- Team page: staff details the employer published on its own website.",
    "- 'self-published on personal website <url>': the person published it on their own site (source URL recorded).",
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

CAND_DEFAULTS = {"c_github": True, "c_bsky": True, "c_masto": True, "c_osm": True, "c_team_urls": "",
                 "c_f_contract": "Any", "c_sites": True, "c_ads": True, "c_ads_presets": True, "c_hireable": False,
                 "c_unread": True, "c_unknown": False, "c_retention": 30, "c_max_profiles": 200, "c_max_accounts": 600,
                 "c_areas": 6, "c_wait": 0, "c_title": "", "c_postcode": "", "c_radius": 15, "c_exp": (3, 30),
                 "c_keywords": "", "c_f_show": "Everyone", "c_f_text": "", "c_f_hire": False, "c_f_phone": False}


def candidates_page():
    defaults(CAND_DEFAULTS)
    finish_task("cand_task", _apply)
    st.markdown(STICKY_CSS, unsafe_allow_html=True)
    st.title("🧑‍💼 Find candidates")
    st.caption("Find people near a UK postcode from **lawful public sources**: public GitHub, Bluesky and Mastodon "
               "profiles whose own bio matches the job (plus contact details people published on their own "
               "websites) and hiring contacts that employers published. "
               "No LinkedIn/Indeed/Facebook scraping, no guessed emails.")
    tip("cands", "**How to use:** enter a job title/skills and a UK postcode, choose the radius and experience, then "
                 "press **Find people** (1–3 minutes; you can keep using the page). Use **Show** and the filter box "
                 "to narrow the table, then download it. Only people whose own profile mentions the job are shown. GitHub "
                 "suits developer and data roles; Bluesky and Mastodon cover other jobs too (thinly). Read the GDPR note "
                 "before contacting anyone.")

    with st.sidebar:
        st.header("Where to look")
        st.checkbox("GitHub public profiles", key="c_github",
                    help="Developers/data people whose public GitHub location is near the postcode. Skipped "
                         "automatically for non-software jobs.")
        st.checkbox("Bluesky public profiles", key="c_bsky",
                    help="People whose public Bluesky bio mentions the job and a place near the postcode. Any job.")
        st.checkbox("Mastodon public profiles", key="c_masto",
                    help="People on Mastodon who opted in to being discoverable and whose bio mentions the job and "
                         "a nearby place. Any job.")
        st.checkbox("OpenStreetMap businesses (trades, accountants, lawyers…)", key="c_osm",
                    help="Firms and sole traders mapped in OpenStreetMap for this kind of work (e.g. electricians, "
                         "plumbers, accountants, law firms) with the contact details published for them. Shown as "
                         "'business', not job seekers. Data © OpenStreetMap contributors (ODbL).")
        with st.expander("Team / 'our people' pages (optional)"):
            st.text_area("Paste firm or chambers 'our people' page links, one per line", key="c_team_urls",
                         height=90, placeholder="https://www.example-law.co.uk/our-people")
            st.caption("Reads the staff details the employer published as schema.org data (robots.txt and noindex "
                       "respected); only people whose job title matches are kept.")
        st.toggle("Check their own websites for contact details", key="c_sites",
                  help="If a person links their own website/portfolio, read it politely (robots.txt and "
                       "noindex respected) and record an email/phone they published there themselves.")
        st.checkbox("Hiring contacts published by employers", key="c_ads",
                    help="Recruiter/hiring emails employers published in job ads, plus careers@/jobs@ style "
                         "addresses on company careers/contact pages. Business contacts, not candidates.")
        st.toggle("…include all preset UK-hiring companies", key="c_ads_presets")
        st.header("Search filters")
        st.toggle("Only people marked 'Available for hire'", key="c_hireable",
                  help="GitHub users can tick 'Available for hire' on their profile.")
        st.toggle("Include accounts not read yet (quick list)", key="c_unread",
                  help="Without a token GitHub allows ~60 profile reads per hour. Accounts found by the "
                       "search but not read yet are still listed with their profile link.")
        st.toggle("Include people with unclear location", key="c_unknown")
        st.number_input("Delete saved candidate exports after (days)", 1, 365, key="c_retention")
        with st.expander("Advanced"):
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

    with st.expander("⚖️ UK GDPR: read before you contact anyone", expanded=False):
        st.markdown(GDPR_TEXT)

    c1, c2 = st.columns([2, 1])
    c1.text_input("Job title / skills", placeholder="e.g. electrician, accountant, care worker, python developer", key="c_title")
    c2.text_input("UK postcode", placeholder="e.g. M1 1AE", key="c_postcode")
    c3, c4, c5 = st.columns(3)
    c3.slider("Radius (miles)", 1, 50, key="c_radius",
              help="Nearby areas are taken from about 15 miles around; distance is then checked against your radius.")
    c4.slider("Years of experience", 0, 30, key="c_exp",
              help="For GitHub this is estimated from account age, so treat it as a rough signal. 30 = no maximum.")
    c5.text_input("Extra keywords (optional)", placeholder="e.g. django, fintech", key="c_keywords",
                  help="Must appear in the person's bio/company. Commas = OR.")

    with st.expander("🚫 Do-not-contact list", expanded=False):
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

    task = st.session_state.get("cand_task")
    running = bool(task and task.running)
    b1, b2 = st.columns([4, 1])
    go = b1.button("⏳ Searching…" if running else "🔎  Find people", type="primary", width="stretch",
                   disabled=running, key="cand_go")
    if b2.button("🧹 Clear results", width="stretch", key="cand_clear",
                 disabled=running or st.session_state.get("cand_results") is None):
        for k in ("cand_results", "cand_meta", "cand_reports", "cand_task_notice", "cand_coverage"):
            st.session_state.pop(k, None)
        st.rerun()

    if go:
        ss = st.session_state
        team_urls = [u.strip() for u in ss["c_team_urls"].splitlines() if u.strip().startswith("http")]
        providers = ([p for p, k in (("github", "c_github"), ("bluesky", "c_bsky"), ("mastodon", "c_masto"),
                                     ("openstreetmap", "c_osm")) if ss[k]]
                     + (["team_pages"] if team_urls else []) + (["job_ads"] if ss["c_ads"] else []))
        title, postcode = ss["c_title"].strip(), ss["c_postcode"].strip()
        if not title or not postcode:
            st.error("Enter a job title and a UK postcode.")
        elif not providers:
            st.error("Tick at least one source in the sidebar.")
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
                job_sources=["remotive", "arbeitnow", "remoteok", "himalayas", "jobicy"],
                companies=[l.strip() for l in ss.get("companies_text", "").splitlines() if l.strip()])
            radius, retention = float(ss["c_radius"]), int(ss["c_retention"])
            label = f"'{title}' within {ss['c_radius']} miles of {postcode.upper()}"

            def run(t):
                t.purged = purge_old_exports(retention)
                return find_candidates(title, postcode, radius, exp[0] or None, exp[1] if exp[1] < 30 else None,
                                       progress=t.say, cancel=t.cancel, **kwargs)

            start_task(ss, "cand_task", label, run, friendly_errors={PostcodeError: ""})
            st.rerun()

    task = st.session_state.get("cand_task")
    if task and task.running:
        show_running("cand_task", f"Finding people for {task.label}")
    show_notice("cand_task")
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
    st.session_state["cand_tech"] = res.tech_role
    st.session_state.update({"c_f_show": "Everyone", "c_f_text": "", "c_f_hire": False, "c_f_phone": False,
                             "c_f_contract": "Any"})


def _results() -> None:
    rows = st.session_state.get("cand_results")
    if rows is None:
        return
    cached = st.session_state.get("_cand_df")
    if cached is None or cached[0] is not rows:
        cached = (rows, _frame(rows))
        st.session_state["_cand_df"] = cached
    df = cached[1]
    st.divider()
    st.subheader(f"Results: {st.session_state.get('cand_meta', '')}")
    if st.session_state.get("cand_reports"):
        with st.expander("Search details (sources, limits)"):
            st.markdown("  \n".join(st.session_state["cand_reports"]), unsafe_allow_html=True)
    if st.session_state.get("cand_coverage"):
        st.info("ℹ️ " + st.session_state["cand_coverage"])
    st.warning("🔐 Personal data. Use only with a lawful basis, and don't share these exports.")
    if df.empty:
        st.info("Nobody's public profile matched this job near that postcode. Try a bigger radius, a more common "
                "job title, or fewer years of experience - or use the hiring contacts on the **Jobs** page.")
        return

    f1, f2 = st.columns([2, 3])
    show = f1.radio("Show", ["Everyone", "Only with email", "Candidates", "Businesses", "Hiring contacts"],
                    horizontal=True, key="c_f_show",
                    help="Businesses = firms / sole traders listed in OpenStreetMap, not individual job seekers.")
    q = f2.text_input("🔍 Filter these results", placeholder="name, company, location, bio or email", key="c_f_text")
    g1, g2, g4, g3 = st.columns([1, 1, 2, 1])
    hire = g1.toggle("Only 'Available for hire'", key="c_f_hire")
    phone = g2.toggle("Only with phone", key="c_f_phone")
    contract = g4.radio("Contract type", ["Any", "Permanent", "Locum / temp / contract"], horizontal=True,
                        key="c_f_contract",
                        help="Matches what the person's own profile says (e.g. 'locum', 'bank', 'agency', 'contract', "
                             "'freelance' vs 'permanent'). Most profiles don't say, so this narrows a lot.")

    def reset():
        st.session_state.update({"c_f_show": "Everyone", "c_f_text": "", "c_f_hire": False, "c_f_phone": False,
                                 "c_f_contract": "Any"})
    g3.button("Reset filters", on_click=reset, key="cand_reset_filters")

    view = df
    if show == "Only with email":
        view = view[view["email"] != ""]
    elif show == "Candidates":
        view = view[view["kind"] == "candidate"]
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
    view = view.reset_index(drop=True)

    with st.container(key="sticky_cands"):
        m = st.columns(5)
        m[0].metric("People", int((df["kind"] == "candidate").sum()))
        m[1].metric("Businesses", int((df["kind"] == "business").sum()),
                    help="Firms / sole traders (OpenStreetMap), not individual job seekers")
        m[2].metric("Hiring contacts", int((df["kind"] == "hiring contact").sum()))
        m[3].metric("With email", int((df["email"] != "").sum()))
        m[4].metric("Available for hire", int((df["hireable"] == "Yes").sum()))
    if (df["provider"] == "openstreetmap").any():
        st.caption("Business listings: data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, ODbL.")
    st.caption(f"Showing **{len(view)}** of {len(df)} · click a column header to sort · "
               "hover the table for 🔍 search. 'Not read yet' rows only have a profile link (GitHub hourly limit).")

    if view.empty:
        st.info("No one matches these filters. Press **Reset filters** to see everyone again.")
    else:
        cols = ["name", "provider", "profile_url", "match_reason", "email", "phone", "headline", "company", "location",
                "distance_miles", "experience", "contract", "hireable", "email_source", "website", "other_profiles",
                "linkedin_url", "twitter_url", "kind", "found_at"]
        st.dataframe(view[cols], hide_index=True, width="stretch", height=min(560, 38 + 35 * len(view)),
                     key="cand_table", column_config={
            "name": st.column_config.TextColumn("Name", pinned=True),
            "profile_url": st.column_config.LinkColumn("Profile / ad", display_text="Open ↗", width="small"),
            "email": st.column_config.TextColumn("Email", width="large"),
            "phone": st.column_config.TextColumn("Phone", width="medium"),
            "headline": st.column_config.TextColumn("Headline / bio", width="large"),
            "company": "Company", "location": "Location",
            "distance_miles": st.column_config.NumberColumn("Miles away", format="%.1f"),
            "experience": st.column_config.TextColumn("Experience", help="GitHub values are estimates from account age"),
            "hireable": "Available for hire",
            "email_source": st.column_config.TextColumn("Email source", width="large"),
            "website": st.column_config.LinkColumn("Website"), "kind": "Type",
            "provider": st.column_config.TextColumn("Found via", help="Which public source(s) this person came from"),
            "match_reason": st.column_config.TextColumn("Why they match", width="medium"),
            "other_profiles": st.column_config.TextColumn("Other profiles", width="medium"),
            "contract": st.column_config.TextColumn("Contract", help="What their own profile says: temporary = "
                                                    "locum / agency / bank / contract / freelance"),
            "linkedin_url": st.column_config.LinkColumn("LinkedIn", display_text="LinkedIn ↗"),
            "twitter_url": st.column_config.LinkColumn("X / Twitter", display_text="X ↗"),
            "found_at": "Found at (UTC)"})

    st.markdown("#### ⬇️ Download")
    export_panel("cands", view, df, "people", cand_exports(st.session_state.get("cand_meta", "")))
    st.caption(f"Downloads include every column (email, phone, source of each email, links, distance, experience) "
               f"and a 'Personal data' notice. CLI exports are saved under `{EXPORT_DIR}` and auto-deleted after "
               f"{int(st.session_state.get('c_retention', 30))} days.")
