"""Job Finder - a friendly web UI for recruit_scraper.

Run with:  ./run_ui.sh      (or: .venv/bin/streamlit run app.py)
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from jobs_view import PRESETS, SOURCE_INFO, job_exports, jobs_to_frame
from recruit_scraper.presets import PRESET_COMPANIES, PRESET_SLUGS
from recruit_scraper.core import search
from ui_common import (STICKY_CSS, defaults, export_panel, finish_task, remember_inputs, show_notice, show_running,
                       start_task, tip)

ENV_FILE = Path(__file__).with_name(".env")
PASSWORD_FILE = Path(__file__).with_name(".app_password")
KEY_NAMES = ["REED_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"]

# ---------------------------------------------------------------- helpers
def read_env() -> dict:
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(values: dict) -> None:
    existing = read_env()
    existing.update({k: v for k, v in values.items() if v})
    ENV_FILE.write_text("# Saved by Job Finder. Contains API keys - do not share or commit this file.\n"
                        + "".join(f"{k}={v}\n" for k, v in existing.items()))
    os.chmod(ENV_FILE, 0o600)


def forget_env() -> None:
    if ENV_FILE.exists():
        keep = {k: v for k, v in read_env().items() if k not in KEY_NAMES}
        if keep:
            ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in keep.items()))
        else:
            ENV_FILE.unlink()
    for k in KEY_NAMES:
        st.session_state[k] = ""


def lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").replace(",", "\n").splitlines() if l.strip()]


def add_company(slug: str) -> None:
    current = lines(st.session_state.get("companies_text", ""))
    if slug not in current:
        current.append(slug)
    st.session_state["companies_text"] = "\n".join(current)


JOB_DEFAULTS = {"j_keywords": "", "j_location": "", "j_urls": "", "j_max": 1000, "j_all_presets": True,
                "j_remote_uk": True, "j_hide_contacts": False, "j_delay": 1.0, "j_follow": 25,
                "j_f_text": "", "j_f_sources": [], "j_f_salary": False, "j_f_remote": False, "j_f_email": False, "j_f_contract": "Any",
                **{f"j_src_{n}": d for n, (_, _, d) in SOURCE_INFO.items()}}


# ---------------------------------------------------------------- page
st.set_page_config(page_title="Job Finder", page_icon="🔎", layout="wide")
saved = read_env()


def _server_secret(name: str) -> str:
    """Optional server-side keys from env vars or Streamlit secrets (cloud hosts). Never displayed."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return str(st.secrets.get(name) or "")
    except Exception:  # noqa: BLE001 - no secrets configured
        return ""


for k in KEY_NAMES:
    st.session_state.setdefault(k, saved.get(k, "") or _server_secret(k))
st.session_state.setdefault("GITHUB_TOKEN", _server_secret("GITHUB_TOKEN"))
st.session_state.setdefault("companies_text", "greenhouse:monzo\nlever:spotify\nashby:synthesia")
remember_inputs(("j_", "c_", "companies_text", "GITHUB_TOKEN", *KEY_NAMES))


def configured_password() -> str | None:
    """The app password: APP_PASSWORD env var, then Streamlit secrets (cloud / Spaces), then the .app_password file.

    Returns "" when explicitly disabled (APP_PASSWORD=""), None when nothing is configured. Never logged or shown."""
    if "APP_PASSWORD" in os.environ:  # env var wins; set it to "" to disable the gate on a private machine
        return os.environ["APP_PASSWORD"]
    try:
        val = st.secrets.get("APP_PASSWORD")  # .streamlit/secrets.toml, or the host's secrets UI
        if val is not None:
            return str(val)
    except Exception:  # noqa: BLE001 - no secrets file configured
        pass
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text().strip()
    return None


def _local_only() -> bool:
    """True when the server only listens on this machine (then running without a password is allowed)."""
    try:
        addr = st.get_option("server.address")
    except Exception:  # noqa: BLE001
        addr = None
    return addr in ("127.0.0.1", "localhost", "::1")


def check_password() -> bool:
    """Password gate. Fails closed on a public/cloud server that has no password configured."""
    import hmac
    expected = configured_password()
    if expected == "" or st.session_state.get("authed"):
        return True
    if expected is None:
        if _local_only():
            return True
        st.title("🔒 Job Finder")
        st.error("No password is set, so the app is locked. Set an **APP_PASSWORD** secret (Hugging Face: Space "
                 "Settings → Variables and secrets; Streamlit Cloud: App settings → Secrets), then restart the app.")
        return False
    st.title("🔒 Job Finder")
    with st.form("login", border=True):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Unlock", type="primary")
    if ok:
        if pw and hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password – please try again.")
    st.caption("This app handles personal data, so it is password protected.")
    return False


def _source_line(label: str, r) -> str:
    if r.status == "ok":
        return f"✅ **{label}** – {r.matched} matching jobs (of {r.fetched} checked)" + (f" · ⚠️ {r.message}" if r.message else "")
    if r.status == "skipped":
        return f"⏭️ **{label}** skipped: {r.message}"
    return f"❌ **{label}** had a problem: {r.message or 'unknown error'}"


def jobs_page():
    defaults(JOB_DEFAULTS)
    finish_task("jobs_task", _apply_jobs)
    st.markdown(STICKY_CSS, unsafe_allow_html=True)
    st.title("🔎 Job Finder")
    st.caption("Search public job listings from company career sites and job boards, then download them as Excel, CSV or JSON. "
               "Only publicly posted job ads are collected – never candidates' personal profiles.")
    tip("jobs", "**How to use:** 1) type job keywords and a location, 2) press **Search jobs** (about a minute – "
                "you can keep using the page), 3) narrow the table with the filters, click a row to read the ad, "
                "4) download what you see as **Excel/CSV/JSON**. Results stay until you run a new search.")

    # ---- sidebar: where to look + options
    with st.sidebar:
        st.header("1 · Where to look")
        chosen = [name for name, (label, hint, _) in SOURCE_INFO.items()
                  if st.checkbox(label, help=hint, key=f"j_src_{name}")]

        st.header("2 · Options")
        st.slider("Maximum jobs", 50, 5000, step=50, key="j_max")
        st.toggle(f"Search all {len(PRESET_COMPANIES)} preset UK-hiring companies", key="j_all_presets",
                  help="Adds every preset company (Monzo, Deliveroo, Stripe, Wayve, ASOS…) to the search. "
                       "Takes about a minute.")
        st.toggle("Include remote jobs open to UK candidates", key="j_remote_uk",
                  help="When you filter by a UK place, also keep remote roles open to UK/Europe/worldwide.")
        st.toggle("Hide recruiter emails/phones", key="j_hide_contacts",
                  help="Hides (and leaves out of downloads) contact details employers published in their ads.")

        with st.expander("🔑 API keys for Reed / Adzuna (optional)"):
            st.caption("Keys are used for this session only, unless you tick 'Remember'.")
            st.text_input("Reed API key", type="password", key="REED_API_KEY",
                          help="Free from https://www.reed.co.uk/developers/jobseeker")
            st.text_input("Adzuna app ID", key="ADZUNA_APP_ID", help="Free from https://developer.adzuna.com/")
            st.text_input("Adzuna app key", type="password", key="ADZUNA_APP_KEY")
            remember = st.checkbox("Remember these keys on this computer")
            if remember:
                st.warning(f"Keys will be saved in plain text to `{ENV_FILE}`. Anyone with access to this computer can read them.")
            if ENV_FILE.exists() and any(saved.get(k) for k in KEY_NAMES):
                st.caption("✅ Saved keys loaded from .env")
                if st.button("Forget saved keys"):
                    forget_env()
                    st.rerun()

        with st.expander("Advanced"):
            st.number_input("Seconds between requests to the same site", 0.2, 10.0, step=0.1, key="j_delay")
            st.number_input("Careers pages: max job links to open per page", 1, 200, key="j_follow")

    # ---- main: what to look for
    c1, c2 = st.columns(2)
    c1.text_input("Job keywords", placeholder="e.g. data engineer, product manager", key="j_keywords",
                  help="Separate alternatives with commas. Words in one phrase must all appear.")
    c2.text_input("Location", placeholder="e.g. London, Manchester, remote", key="j_location",
                  help="Separate alternatives with commas. 'remote' also matches remote jobs.")

    n_companies = len(lines(st.session_state["companies_text"]))
    with st.expander(f"🏢 Companies to search ({n_companies} added"
                     + (f" + {len(PRESET_COMPANIES)} presets)" if st.session_state["j_all_presets"] else ")")):
        st.caption("One per line as **site:company** – e.g. `greenhouse:monzo`. Find it in the company's job page link: "
                   "job-boards.greenhouse.io/**monzo**, jobs.lever.co/**spotify**, jobs.ashbyhq.com/**synthesia**.")
        st.text_area("Companies", key="companies_text", height=110, label_visibility="collapsed")
        st.write("Quick add:")
        items = list(PRESETS.items())
        for row in range(0, len(items), 8):
            cols = st.columns(8)
            for col, (nm, slug) in zip(cols, items[row: row + 8]):
                col.button(nm, key=f"preset_{slug}", on_click=add_company, args=(slug,), width="stretch", help=slug)

    with st.expander("🌐 Careers page links (optional)"):
        st.text_area("Paste careers or job page links, one per line", height=90, key="j_urls",
                     placeholder="https://jobs.lever.co/spotify\nhttps://www.example.com/careers/senior-engineer")
        st.caption("Works for sites that publish standard job data (most modern careers sites). "
                   "Sites that ask not to be scraped (robots.txt) are skipped automatically.")

    task = st.session_state.get("jobs_task")
    running = bool(task and task.running)
    b1, b2 = st.columns([4, 1])
    go = b1.button("⏳ Searching…" if running else "🔎  Search jobs", type="primary", width="stretch",
                   disabled=running, key="jobs_go")
    if b2.button("🧹 Clear results", width="stretch", disabled=running or "results" not in st.session_state,
                 key="jobs_clear"):
        for k in ("results", "searched_at", "jobs_sources", "jobs_task_notice"):
            st.session_state.pop(k, None)
        st.rerun()

    if go:
        if not chosen:
            st.error("Pick at least one place to look in the sidebar.")
        else:
            st.session_state.pop("jobs_task_notice", None)
            ss = st.session_state
            companies = lines(ss["companies_text"])
            if ss["j_all_presets"]:
                companies += [c for c in PRESET_SLUGS if c not in companies]
            urls = [u for u in lines(ss["j_urls"]) if u.startswith("http")]
            keys = {k: ss.get(k, "").strip() for k in KEY_NAMES}
            if remember and any(keys.values()):
                write_env(keys)
            params = dict(keywords=lines(ss["j_keywords"]), locations=lines(ss["j_location"]), companies=companies,
                          urls=urls, max_results=int(ss["j_max"]), delay=float(ss["j_delay"]),
                          follow_links=int(ss["j_follow"]), api_keys=keys, remote_uk=bool(ss["j_remote_uk"]))
            summary = " · ".join(filter(None, [ss["j_keywords"] or "any job", ss["j_location"]]))

            def run(t, chosen=list(chosen), params=params):
                total, done = len(chosen), [0]
                t.lines = []

                def progress(name, event, r):
                    label = SOURCE_INFO[name][0] if name in SOURCE_INFO else name
                    if event == "start":
                        return
                    done[0] += 1
                    t.progress = done[0] / total
                    t.lines.append(_source_line(label, r))
                    t.say(f"{done[0]} of {total} sources done – {label}")
                return search(chosen, progress=progress, cancel=t.cancel, **params)

            start_task(st.session_state, "jobs_task", summary, run)
            st.session_state["jobs_pending_label"] = summary
            st.rerun()

    task = st.session_state.get("jobs_task")
    if task and task.running:
        show_running("jobs_task", f"Searching for {task.label}")
    show_notice("jobs_task")
    show_job_results()


def _apply_jobs(t) -> None:
    res = t.result
    st.session_state["results"] = [j.to_dict() for j in res.jobs]
    st.session_state["searched_at"] = f"{t.label} · {datetime.now().strftime('%d %b %Y %H:%M')}"
    st.session_state["jobs_sources"] = getattr(t, "lines", [])
    for k in ("j_f_text", "j_f_sources", "j_f_contract"):  # new search -> clear old table filters
        st.session_state[k] = JOB_DEFAULTS[k]
    for k in ("j_f_salary", "j_f_remote", "j_f_email"):
        st.session_state[k] = False


def _results_frame() -> pd.DataFrame:
    """Build the results table once per search (not on every click) and keep it in the session."""
    results = st.session_state["results"]
    cached = st.session_state.get("_jobs_df")
    if cached is None or cached[0] is not results:
        cached = (results, jobs_to_frame(results))
        st.session_state["_jobs_df"] = cached
    return cached[1]


def show_job_results() -> None:
    results = st.session_state.get("results")
    if results is None:
        return
    df = _results_frame()
    hide_contacts = bool(st.session_state.get("j_hide_contacts"))
    st.divider()
    st.subheader(f"Results · {st.session_state.get('searched_at', '')}")
    if st.session_state.get("jobs_sources"):
        with st.expander("Where the jobs came from"):
            st.markdown("  \n".join(st.session_state["jobs_sources"]))
    if df.empty:
        st.info("No jobs matched. Try fewer keywords, a broader location (or none), or tick more sources in the sidebar.")
        return

    # ---- filters (instant, no new search)
    f1, f2 = st.columns([2, 1])
    q = f1.text_input("🔍 Filter these results", placeholder="type to filter by title, company, location or description",
                      key="j_f_text")
    srcs = f2.multiselect("Found on", sorted(df["where_found"].unique()), key="j_f_sources",
                          placeholder="All sources")
    g1, g2, g3, g4 = st.columns(4)
    only_salary = g1.toggle("Only with salary", key="j_f_salary")
    only_remote = g2.toggle("Only remote", key="j_f_remote")
    only_email = g3.toggle("Only with contact email", key="j_f_email", disabled=hide_contacts)

    contract = st.radio("Contract type", ["Any", "Permanent", "Locum / temp / contract"], horizontal=True,
                        key="j_f_contract",
                        help="Uses the ad's own contract type (e.g. NHS Jobs: Permanent / Fixed-Term / Bank / Locum) "
                             "or words in the title/ad like 'locum', 'bank', 'agency', 'fixed term', 'permanent'. "
                             "Ads that don't say are hidden when this is set.")

    def reset_filters():
        st.session_state.update({"j_f_text": "", "j_f_sources": [], "j_f_salary": False, "j_f_remote": False,
                                 "j_f_email": False, "j_f_contract": "Any"})
    g4.button("Reset filters", on_click=reset_filters, key="jobs_reset_filters")

    view = df
    if q:
        ql = q.lower().strip()
        mask = (view["title"].str.lower().str.contains(ql, regex=False)
                | view["company"].str.lower().str.contains(ql, regex=False)
                | view["location"].str.lower().str.contains(ql, regex=False)
                | view["description"].str.lower().str.contains(ql, regex=False))
        view = view[mask]
    if srcs:
        view = view[view["where_found"].isin(srcs)]
    if contract != "Any" and "contract" in view.columns:
        view = view[view["contract"] == ("permanent" if contract == "Permanent" else "temporary")]
    if only_salary:
        view = view[view["salary"] != ""]
    if only_remote:
        view = view[view["remote"] == "Yes"]
    if only_email and not hide_contacts:
        view = view[view["contact_email"] != ""]
    view = view.reset_index(drop=True)

    # ---- sticky summary
    with st.container(key="sticky_jobs"):
        m = st.columns(5)
        m[0].metric("Jobs shown", f"{len(view):,}", help=f"{len(df):,} found in total")
        m[1].metric("Found in total", f"{len(df):,}")
        m[2].metric("With salary", f"{int((view['salary'] != '').sum()):,}")
        m[3].metric("With contact email", "hidden" if hide_contacts else f"{int((view['contact_email'] != '').sum()):,}")
        m[4].metric("Companies", f"{view['company'].nunique():,}")

    if view.empty:
        st.info("No results match these filters. Press **Reset filters** to see everything again.")
    else:
        st.caption("Click a column header to sort · tick a row's box to read the full ad below · "
                   "hover the table for 🔍 search and ⬇ quick download.")
        cols = ["title", "url", "company", "location", "salary", "posted_date", "employment_type", "remote"]
        if not hide_contacts:
            cols += ["contact_email", "contact_phone"]
        cols += ["where_found"]
        event = st.dataframe(
            view[cols], hide_index=True, width="stretch", height=min(560, 38 + 35 * len(view)),
            on_select="rerun", selection_mode="single-row", key="jobs_table",
            column_config={
                "title": st.column_config.TextColumn("Job title", width="large", pinned=True),
                "url": st.column_config.LinkColumn("Job ad", display_text="Open ad ↗", width="small"),
                "company": "Company", "location": st.column_config.TextColumn("Location", width="medium"),
                "salary": "Salary",
                "posted_date": st.column_config.DateColumn("Posted", format="D MMM YYYY"),
                "employment_type": "Type", "remote": "Remote", "where_found": "Found on",
                "contact_email": st.column_config.TextColumn("Contact email", width="large"),
                "contact_phone": st.column_config.TextColumn("Contact phone", width="medium"),
            })
        sel = list(event.selection.rows) if event is not None and hasattr(event, "selection") else []
        sel = [i for i in sel if 0 <= i < len(view)]  # selection can be stale after the filters change
        if sel:
            row = view.iloc[sel[0]]
            with st.container(border=True):
                st.markdown(f"#### 📄 {row['title']} — {row['company']}")
                a, b, c, d = st.columns(4)
                a.markdown(f"**Location:** {row['location'] or '—'}")
                b.markdown(f"**Salary:** {row['salary'] or 'not stated'}")
                c.markdown(f"**Posted:** {row['posted_date'] if pd.notna(row['posted_date']) else '—'}")
                if not hide_contacts and (row["contact_email"] or row["contact_phone"]):
                    d.markdown(f"**Contact:** {row['contact_email']} {row['contact_phone']}")
                st.link_button("Open the original job ad ↗", row["url"])
                st.text(row["description"] or "(no description)")
        else:
            st.caption("👆 Tick a row in the table to read the full job ad here.")

    st.markdown("#### ⬇️ Download")
    export_panel("jobs", view, df, "jobs", job_exports(hide_contacts))
    st.caption("Excel and CSV include every column: title, company, location, salary (text + numbers), posted date, "
               "type, remote, contact email/phone, link, source and the full description.")


from candidates_page import candidates_page  # noqa: E402

if check_password():
    nav = st.navigation([
        st.Page(jobs_page, title="Find jobs", icon="💼", default=True),
        st.Page(candidates_page, title="Find candidates", icon="🧑‍💼", url_path="candidates"),
    ])
    nav.run()
