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
