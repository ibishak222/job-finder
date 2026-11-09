"""Shared Streamlit helpers: background searches, remembered inputs, tips and exports (Excel/CSV/JSON).

Export helpers are plain functions (no Streamlit calls) so they can be unit-tested.
"""
from __future__ import annotations

import io
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional

import pandas as pd

log = logging.getLogger("job_finder.ui")

# Tests flip this to run searches synchronously (AppTest can't wait on background threads).
RUN_INLINE = False

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# ------------------------------------------------------------------ export (pure functions)
def _clean_value(v: Any) -> Any:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat()[:10] if isinstance(v, date) and not isinstance(v, datetime) else v.isoformat()
    if hasattr(v, "item") and not isinstance(v, str):  # numpy scalar -> python
        try:
            return v.item()
        except Exception:  # noqa: BLE001
            return v
    return v


def _safe_text(v: Any) -> Any:
    """Neutralise spreadsheet formulas (=, @, or +/- not followed by a digit) and strip XML-illegal characters."""
    if not isinstance(v, str):
        return v
    v = _ILLEGAL_XML.sub("", v)
    if v[:1] in ("=", "@") or (v[:1] in ("+", "-") and not re.match(r"[+-][\d\s(]", v)):
        return "'" + v
    return v


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of plain dicts (NaN -> None, dates -> ISO strings). Keeps every column."""
    return [{k: _clean_value(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """UTF-8 with BOM so Excel shows £/€ correctly; formula-like cells neutralised."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object or pd.api.types.is_string_dtype(out[c]):
            out[c] = out[c].map(_safe_text)
    return out.to_csv(index=False).encode("utf-8-sig")


def to_json_bytes(df: pd.DataFrame, notice: Optional[str] = None, key: str = "results", meta: Optional[dict] = None) -> bytes:
    rows = records(df)
    if notice or meta:
        payload: Any = {**({"notice": notice} if notice else {}), **(meta or {}), "count": len(rows), key: rows}
    else:
        payload = rows
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def to_excel_bytes(df: pd.DataFrame, sheet: str, link_cols: tuple[str, ...] = (), wide_cols: dict | None = None,
                   notice_sheet: Optional[tuple[str, list[str]]] = None) -> bytes:
    """Excel with bold frozen header, auto-filter, sensible widths and clickable links.

    notice_sheet=(name, lines) adds a second sheet (e.g. 'Personal data' notice for candidate exports)."""
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object or pd.api.types.is_string_dtype(out[c]):
            out[c] = out[c].map(lambda v: _ILLEGAL_XML.sub("", v)[:32000] if isinstance(v, str)
                                else v if isinstance(v, (date, datetime)) else _clean_value(v))
    wide_cols = wide_cols or {}
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        out.to_excel(xw, index=False, sheet_name=sheet)
        ws = xw.sheets[sheet]
        for row in ws.iter_rows(min_row=2):
            for cell in row:          # text that looks like a formula stays text (no formula injection)
                if cell.data_type == "f":
                    cell.data_type = "s"
        ws.freeze_panes = "A2"
        if len(out.columns):
            ws.auto_filter.ref = f"A1:{get_column_letter(len(out.columns))}{max(len(out) + 1, 2)}"
        head_fill = PatternFill("solid", fgColor="DBEAFE")
        for i, col in enumerate(out.columns, start=1):
            cell = ws.cell(1, i)
            cell.font, cell.fill = Font(bold=True), head_fill
            sample = out[col].head(200).map(lambda v: len(str(v)) if v is not None else 0)
            natural = max([len(str(col))] + sample.tolist()) + 2
            ws.column_dimensions[get_column_letter(i)].width = wide_cols.get(col, min(max(natural, 10), 45))
        for col in link_cols:
            if col not in out.columns:
                continue
            ci = list(out.columns).index(col) + 1
            for r in range(2, len(out) + 2):
                c = ws.cell(r, ci)
                if isinstance(c.value, str) and c.value.startswith(("http://", "https://")):
                    c.hyperlink, c.style = c.value, "Hyperlink"
        if notice_sheet:
            name, lines_ = notice_sheet
            ns = xw.book.create_sheet(name)
            for r, line in enumerate(lines_, start=1):
                ns.cell(r, 1, _safe_text(line))
            ns.cell(1, 1).font = Font(bold=True, color="B91C1C")
            ns.column_dimensions["A"].width = 140
    return buf.getvalue()


# ------------------------------------------------------------------ background searches
@dataclass
class Task:
    """A search running in a background thread, so clicking other widgets (or downloading) can't kill it."""
    label: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    log: list[str] = field(default_factory=list)
    progress: float = 0.0
    status: str = "running"        # running | done | error | cancelled
    result: Any = None
    error: str = ""
    started: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)
    applied: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    def say(self, msg: str) -> None:
        self.log.append(msg)
        del self.log[:-200]


def start_task(state: dict, key: str, label: str, fn: Callable[[Task], Any],
               friendly_errors: dict | None = None) -> Task:
    """Run fn(task) in a daemon thread (or inline in tests). Result/exception are stored on the task."""
    task = Task(label)
    state[key] = task

    def work():
        try:
            res = fn(task)
            if task.cancel.is_set():
                task.status = "cancelled"
            else:
                task.result, task.status = res, "done"
        except Exception as e:  # noqa: BLE001 - show a friendly message, log the details
            if task.cancel.is_set():
                task.status = "cancelled"
                return
            for exc_type, text in (friendly_errors or {}).items():
                if isinstance(e, exc_type):
                    task.error = text or str(e)
                    break
            else:
                log.exception("%s failed", label)
                task.error = f"Something went wrong ({type(e).__name__}: {e}). Please try again."
            task.status = "error"

    if RUN_INLINE:
        work()
    else:
        threading.Thread(target=work, name=f"search-{task.id}", daemon=True).start()
    return task


# ------------------------------------------------------------------ Streamlit widgets
def remember_inputs(prefixes: tuple[str, ...]) -> None:
    """Keep widget values when switching pages (Streamlit drops state of widgets that aren't rendered)."""
    import streamlit as st
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith(prefixes):
            try:
                st.session_state[k] = st.session_state[k]
            except Exception:  # noqa: BLE001 - some widget types (buttons) can't be set
                pass


def defaults(values: dict) -> None:
    import streamlit as st
    for k, v in values.items():
        st.session_state.setdefault(k, v)


def tip(key: str, text: str) -> None:
    """A dismissable 'how to use' box (stays hidden for this browser session once dismissed)."""
    import streamlit as st
    flag = f"tip_hidden_{key}"
    if st.session_state.get(flag):
        return
    with st.container(border=True):
        a, b = st.columns([12, 1])
        a.markdown(text)
        b.button("✕", key=f"tipbtn_{key}", help="Hide this tip", on_click=lambda: st.session_state.update({flag: True}))


STICKY_CSS = """
<style>
div[class*="st-key-sticky_"] {position: sticky; top: 3.5rem; z-index: 50; background: var(--background-color, #fff);
  padding: .35rem .5rem; border-bottom: 1px solid rgba(49,51,63,.15); border-radius: .4rem;}
div[class*="st-key-sticky_"] [data-testid="stMetricValue"] {font-size: 1.5rem;}
</style>
"""


def export_panel(key: str, shown: pd.DataFrame, everything: pd.DataFrame, noun: str, build: dict) -> None:
    """Download buttons for the visible (filtered) table, or all results.

    build = {"excel": fn(df)->bytes, "csv": fn, "json": fn}. Files are generated only when clicked
    (download data callables), and clicking never clears the results."""
    import streamlit as st

    filtered = len(shown) != len(everything)
    scope = "shown"
    if filtered:
        scope = st.radio("What to export", ["shown", "all"], horizontal=True, key=f"{key}_scope",
                         format_func=lambda s: f"Rows shown ({len(shown)})" if s == "shown"
                         else f"All results ({len(everything)})")
    data = shown if scope == "shown" else everything
    n = len(data)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base = f"{noun}_{stamp}" + ("" if scope == "all" or not filtered else "_filtered")

    def done(fmt):
        st.session_state[f"{key}_last_download"] = f"Downloaded {n} {noun} as {fmt}"

    cols = st.columns(3)
    specs = [("Excel (.xlsx)", "excel", "xlsx", XLSX_MIME, "primary"),
             ("CSV", "csv", "csv", "text/csv", "secondary"),
             ("JSON", "json", "json", "application/json", "secondary")]
    for col, (label, fmt, ext, mime, kind) in zip(cols, specs):
        fn = build[fmt]
        col.download_button(f"⬇️ {label} · {n} rows", data=(lambda fn=fn, d=data: fn(d)), file_name=f"{base}.{ext}",
                            mime=mime, key=f"{key}_dl_{fmt}", type=kind, width="stretch", disabled=n == 0,
                            on_click=done, args=(label.split(" ")[0],))
    msg = st.session_state.pop(f"{key}_last_download", None)
    if msg:
        st.toast("✅ " + msg)
        st.success("✅ " + msg + " – check your browser's downloads.")


def show_running(task_key: str, title: str) -> None:
    """Live progress box for a background search; refreshes every second and reloads the page when finished."""
    import streamlit as st

    @st.fragment(run_every=1.0)
    def live():
        t = st.session_state.get(task_key)
        if t is None or not t.running:
            st.rerun()
            return
        with st.container(border=True):
            a, b = st.columns([5, 1])
            a.progress(min(max(t.progress, 0.02), 1.0),
                       text=f"⏳ {title} – {int(time.time() - t.started)}s so far. You can keep using the page.")

            def cancel():
                t.cancel.set()
                t.status = "cancelled"
            b.button("✖ Cancel", key=f"{task_key}_cancel", on_click=cancel, width="stretch")
            for line in t.log[-6:]:
                st.markdown(f"<small>{line}</small>", unsafe_allow_html=True)
    live()


def finish_task(task_key: str, apply) -> None:
    """Apply a finished background search to the page's results exactly once; report errors kindly."""
    import streamlit as st
    t = st.session_state.get(task_key)
    if t is None or t.running or t.applied:
        return
    t.applied = True
    if t.status == "done":
        apply(t)
    elif t.status == "cancelled":
        st.session_state[f"{task_key}_notice"] = ("info", "Search cancelled. Your previous results (if any) are kept.")
    else:
        st.session_state[f"{task_key}_notice"] = ("error", t.error or "The search failed. Please try again.")


def show_notice(task_key: str) -> None:
    import streamlit as st
    n = st.session_state.get(f"{task_key}_notice")
    if n:
        getattr(st, n[0])(n[1])
