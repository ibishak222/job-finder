# Job Finder

Search public job listings (company career sites and job boards) and find candidates near a UK postcode from lawful public sources, then export the results to Excel, CSV, or JSON.

The app lives in this GitHub repository and is meant to be deployed on [Streamlit Community Cloud](https://share.streamlit.io/). The entry point is `app.py` at the repository root (`requirements.txt` lists the Python dependencies).

## Required secret

Set **`APP_PASSWORD`** in **App settings → Secrets**. The app stays **locked** until it is set, because results can contain personal data (UK GDPR).

In the Streamlit Community Cloud secrets editor, paste TOML like this and replace the placeholder with a long random password of your own:

```toml
APP_PASSWORD = "change-me-to-a-long-random-password"
```

Save, then reboot the app if it does not pick the secret up on its own. For a local run, copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` (that file is git-ignored). Do not commit `secrets.toml`, `.env`, or `.app_password`.

Optional secrets: `GITHUB_TOKEN` (raises GitHub's limit from ~60 to 5,000 profile reads/hour), `REED_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`.

## Notes

* Community Cloud apps sleep when idle; the first visit afterwards can take a little while to wake.
* The disk is temporary: the 7-day profile cache and the do-not-contact list reset when the app restarts or is redeployed. Keep your master do-not-contact list elsewhere and re-upload it.
* **Employers to contact** use official open-data registers (CQC, Care Inspectorate, NHS ODS CSV reports, FSA food hygiene, DfE GIAS, Charity Commission, Bar Standards Board), downloaded only when the role needs them, streamed and filtered to the postcode areas searched, then cached on disk for 7 days. The first search in an area can take up to ~2 minutes; a register that is down is skipped with a note. Named people in these rows (registered managers, nurse prescribers, headteachers, barristers) come from the register and are **not job-seekers**. Not used: OSCR's download (its terms forbid direct marketing), CQC registered-manager names (only in a heavy monthly spreadsheet) and Companies House bulk data (a 472 MB file with no phone or email, too big for Streamlit Cloud's memory and time limits; its API needs a key).
* **Practice nurses, ANPs, HCAs and district nurses (perm and locum), anywhere in the UK.** Titles match the names nurses and surgeries actually use (GPN, general practice / treatment room nurse, ANP / nurse practitioner, HCSW, community staff nurse); nurse prescribers, chronic-disease and cervical-screening nurses are shown as close matches. A title with *locum* / *bank* searches for those words first, asks NHS Jobs for Locum and Bank contracts, and adds out-of-hours / urgent care centres and nursing agencies to the employers. Registers run only for the nation searched: England (CQC, NHS ODS, named nurse prescribers), Wales (NHS ODS GP practices), Scotland (Public Health Scotland GP practices, Care Inspectorate) and Northern Ireland (HSC BSO practice list, RQIA register). For the top GP practices with a website, the contact email and practice manager the practice publishes on its **own** site are added (robots.txt respected; never guessed). Type **UK** instead of a postcode for a whole-UK search: a sample of the most contactable employers in every postcode area and UK-based people from anywhere. NHS Jobs covers England and Wales only, so jobs it returns far from a Scottish / NI place are dropped. Care Inspectorate Wales has no register download, so it is not used.
* **Locum / bank vacancies (Jobs page).** Type e.g. *locum practice nurse* (or *bank* / *sessional*): NHS Jobs is asked for Locum and Bank contracts for the title and close primary / community nursing titles (ANP, nurse practitioner, district / community nurse, immunisation nurse), plus **NHS Scotland Jobs** and **HSC Northern Ireland jobs** (both public search pages, no login or key). Only ads whose contract is locum / bank / sessional / agency / ad hoc / temporary / fixed-term are kept, and hospital-only titles (ward, theatre, pre-op…) are left out. The table shows pay as written (e.g. £28 per hour), hours / sessions and the closing date. Use **UK** as the location for the whole country. Not used: LinkedIn, Indeed, Glassdoor, Reed/Totaljobs/CV-Library pages, and locum agency apps that need a login.
