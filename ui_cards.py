"""HTML cards for jobs, people and employers (pure functions: dict row in, HTML string out)."""
from __future__ import annotations

import re
from typing import Any

from ui_theme import (ago, badge, button, card, clean, closing_badge, contract_badge, email_button, esc, host, link,
                      phone_button, safe_email, safe_tel, safe_url)

DESC_MAX = 1800


def _text_html(text: str, limit: int = DESC_MAX) -> str:
    t = clean(text)
    cut = len(t) > limit
    t = esc(t[:limit]).replace("\n", "<br>")
    return t + ("…" if cut else "")


def _join(*parts: str) -> str:
    return "".join(p for p in parts if p)


def _num(v: Any) -> str:
    s = clean(v)
    try:
        return f"{float(s):.1f}"
    except ValueError:
        return ""


# ------------------------------------------------------------------ jobs
def job_card(r: dict, hide_contacts: bool = False) -> str:
    title = clean(r.get("title")) or "Untitled job"
    url = safe_url(r.get("url"))
    t_html = (f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(title)}</a>' if url else esc(title))
    ctext, ckind = contract_badge(r.get("contract"), r.get("employment_type"), title)
    badges = []
    sector = clean(r.get("sector"))
    if sector:
        badges.append(badge(sector, "info" if sector == "NHS" else "indep"))
    if ctext:
        badges.append(badge(ctext, ckind))
    if clean(r.get("remote")) == "Yes":
        badges.append(badge("Remote", "remote"))
    if clean(r.get("hours")):
        badges.append(badge(clean(r.get("hours")), "neutral"))
    cb = closing_badge(r.get("closing_date"))
    if cb:
        badges.append(cb)
    pay = clean(r.get("salary")) or clean(r.get("salary_raw"))
    meta = " · ".join(esc(x) for x in (clean(r.get("company")), clean(r.get("location"))) if x)
    posted = ago(r.get("posted_date"))
    lines = []
    if pay:
        lines.append(f'<div class="jf-line">💷 <b>{esc(pay)}</b></div>')
    contact = ""
    actions = [button(url, "Apply / view ad ↗", primary=True)]
    if not hide_contacts:
        actions += [email_button(r.get("contact_email")), phone_button(r.get("contact_phone"))]
        if safe_email(r.get("contact_email")) or safe_tel(r.get("contact_phone")):
            contact = '<div class="jf-src">Contact details were published by the employer in the ad.</div>'
    desc = clean(r.get("description"))
    details = (f'<details><summary>Read the description</summary><div class="jf-desc">{_text_html(desc)}</div></details>'
               if desc else "")
    src = clean(r.get("where_found")) or clean(r.get("source"))
    foot = f'<div class="jf-src">Found on {esc(src)}' + (f" · posted {esc(posted)}" if posted else "") + "</div>"
    body = _join(f'<p class="jf-title">{t_html}</p>', f'<div class="jf-meta">{meta}</div>' if meta else "",
                 f'<div class="jf-badges">{"".join(badges)}</div>' if badges else "", "".join(lines),
                 f'<div class="jf-actions">{"".join(actions)}</div>', contact, details, foot)
    return card(body, ckind)


# ------------------------------------------------------------------ people
KIND_LABEL = {"candidate": ("Candidate", "good"), "business": ("Business, not a job-seeker", "neutral"),
              "hiring contact": ("Hiring contact", "info")}


def person_card(r: dict) -> str:
    name = clean(r.get("name")) or "Unnamed"
    provider = clean(r.get("provider"))
    kind = clean(r.get("kind")) or "candidate"
    ktext, kk = KIND_LABEL.get(kind, (kind.capitalize(), "neutral"))
    if "europepmc" in provider:
        ktext, kk = "Clinical researcher (research email)", "info"
    badges = [badge(ktext, kk)]
    ctext, ckind = contract_badge(r.get("contract"))
    if ctext:
        badges.append(badge(ctext, ckind))
    if clean(r.get("hireable")) == "Yes":
        badges.append(badge("Available for hire", "good"))
    if clean(r.get("profile_read")) == "False":
        badges.append(badge("Profile not read yet", "neutral"))
    prof = safe_url(r.get("profile_url"))
    n_html = f'<a href="{esc(prof)}" target="_blank" rel="noopener noreferrer">{esc(name)}</a>' if prof else esc(name)
    dist = _num(r.get("distance_miles"))
    meta = " · ".join(esc(x) for x in (clean(r.get("company")), clean(r.get("location")),
                                       f"{dist} miles away" if dist else "", clean(r.get("experience"))) if x)
    head = clean(r.get("headline"))
    lines = [f'<div class="jf-line">{esc(head[:220])}{"…" if len(head) > 220 else ""}</div>'] if head else []
    fit = 0
    try:
        fit = max(0, min(100, int(float(clean(r.get("fit")) or 0))))
    except ValueError:
        pass
    score = (f'<div class="jf-src">Best match {fit}/100' + (f" · {esc(clean(r.get('match_reason')))}" if clean(r.get("match_reason")) else "")
             + f'</div><div class="jf-score"><i style="width:{fit}%"></i></div>') if fit else ""
    actions = [email_button(r.get("email")), phone_button(r.get("phone")),
               button(safe_url(r.get("message_url")), f"💬 Message on {clean(r.get('message_via')) or 'their profile'}"),
               button(prof, "Profile ↗"), button(safe_url(r.get("website")), "Website ↗"),
               button(safe_url(r.get("linkedin_url")), "LinkedIn ↗")]
    srcs = []
    if safe_email(r.get("email")) and clean(r.get("email_source")):
        srcs.append(f"Email: {esc(clean(r.get('email_source'))[:160])}")
    cs = link(r.get("contact_source"), "contact page") if safe_url(r.get("contact_source")) else ""
    if cs:
        srcs.append(f"Published on {cs}")
    srcs.append(f"Found via {esc(provider or 'public source')}")
    if clean(r.get("last_active")):
        srcs.append(f"last active {esc(clean(r.get('last_active')))}")
    body = _join(f'<p class="jf-title">{n_html}</p>', f'<div class="jf-meta">{meta}</div>' if meta else "",
                 f'<div class="jf-badges">{"".join(badges)}</div>', "".join(lines), score,
                 f'<div class="jf-actions">{"".join(a for a in actions if a)}</div>' if any(actions) else "",
                 f'<div class="jf-src">{" · ".join(srcs)}</div>')
    return card(body, ckind)


# ------------------------------------------------------------------ employers
def employer_card(r: dict, locum_friendly: bool = False) -> str:
    name = clean(r.get("name")) or "Employer"
    web = safe_url(r.get("website"))
    n_html = f'<a href="{esc(web)}" target="_blank" rel="noopener noreferrer">{esc(name)}</a>' if web else esc(name)
    badges = [badge(clean(r.get("employer_type")) or "Employer", "neutral")]
    if clean(r.get("sector")):
        badges.insert(0, badge(clean(r.get("sector")), "info" if clean(r.get("sector")) == "NHS" else "indep"))
    vt = clean(r.get("vacancy_type"))
    if locum_friendly:
        badges.append(badge("Locum / bank friendly", "locum"))
    if clean(r.get("vacancy")):
        badges.append(badge("Locum / bank vacancy" if vt.startswith("locum") else "Open vacancy",
                            "locum" if vt.startswith("locum") else "perm"))
    if safe_url(r.get("bank_url")):
        badges.append(badge("Join-the-bank page", "locum"))
    dist = _num(r.get("distance_miles"))
    meta = " · ".join(esc(x) for x in (", ".join(x for x in (clean(r.get("address")), clean(r.get("postcode"))) if x),
                                       f"{dist} miles away" if dist else "") if x)
    lines = []
    if clean(r.get("vacancy")):
        v = clean(r.get("vacancy"))
        vu = safe_url(r.get("vacancy_url"))
        lines.append('<div class="jf-line">📌 ' + (f'<a href="{esc(vu)}" target="_blank" rel="noopener noreferrer">{esc(v[:140])}</a>' if vu else esc(v[:140]))
                     + "</div>")
    if clean(r.get("contact_name")):
        lines.append(f'<div class="jf-line">👤 {esc(clean(r.get("contact_name")))}'
                     + (f' ({esc(clean(r.get("contact_role")))})' if clean(r.get("contact_role")) else "")
                     + ' <span class="jf-src">named in the official register, not a job-seeker</span></div>')
    actions = [phone_button(r.get("phone")), email_button(r.get("email")), button(web, "Website ↗"),
               button(safe_url(r.get("bank_url")), "Join the bank / locum ↗", primary=True),
               button(safe_url(r.get("careers_url")), "Careers ↗"), button(safe_url(r.get("vacancy_url")), "Vacancy ↗")]
    src = clean(r.get("source"))
    urls = [u for u in clean(r.get("source_url")).split() if safe_url(u)][:3]
    su = " ".join(link(u, f"record {i}" if len(urls) > 1 else "record") for i, u in enumerate(urls, 1))
    foot = f'<div class="jf-src">Found on {esc(src)}' + (f" · posted {esc(posted)}" if posted else "") + "</div>"
    body = _join(f'<p class="jf-title">{t_html}</p>', f'<div class="jf-meta">{meta}</div>' if meta else "",
                 f'<div class="jf-badges">{"".join(badges)}</div>' if badges else "", "".join(lines),
                 f'<div class="jf-actions">{"".join(actions)}</div>', contact, details, foot)
    return card(body, ckind)


# ------------------------------------------------------------------ people
KIND_LABEL = {"candidate": ("Candidate", "good"), "business": ("Business, not a job-seeker", "neutral"),
              "hiring contact": ("Hiring contact", "info")}


def person_card(r: dict) -> str:
    name = clean(r.get("name")) or "Unnamed"
    provider = clean(r.get("provider"))
    kind = clean(r.get("kind")) or "candidate"
    ktext, kk = KIND_LABEL.get(kind, (kind.capitalize(), "neutral"))
    if "europepmc" in provider:
        ktext, kk = "Clinical researcher (research email)", "info"
    badges = [badge(ktext, kk)]
    ctext, ckind = contract_badge(r.get("contract"))
    if ctext:
        badges.append(badge(ctext, ckind))
    if clean(r.get("hireable")) == "Yes":
        badges.append(badge("Available for hire", "good"))
    if clean(r.get("profile_read")) == "False":
        badges.append(badge("Profile not read yet", "neutral"))
    prof = safe_url(r.get("profile_url"))
    n_html = f'<a href="{esc(prof)}" target="_blank" rel="noopener noreferrer">{esc(name)}</a>' if prof else esc(name)
    dist = _num(r.get("distance_miles"))
    meta = " · ".join(esc(x) for x in (clean(r.get("company")), clean(r.get("location")),
                                       f"{dist} miles away" if dist else "", clean(r.get("experience"))) if x)
    head = clean(r.get("headline"))
    lines = [f'<div class="jf-line">{esc(head[:220])}{"…" if len(head) > 220 else ""}</div>'] if head else []
    fit = 0
    try:
        fit = max(0, min(100, int(float(clean(r.get("fit")) or 0))))
    except ValueError:
        pass
    score = (f'<div class="jf-src">Best match {fit}/100' + (f" · {esc(clean(r.get('match_reason')))}" if clean(r.get("match_reason")) else "")
             + f'</div><div class="jf-score"><i style="width:{fit}%"></i></div>') if fit else ""
    actions = [email_button(r.get("email")), phone_button(r.get("phone")),
               button(safe_url(r.get("message_url")), f"💬 Message on {clean(r.get('message_via')) or 'their profile'}"),
               button(prof, "Profile ↗"), button(safe_url(r.get("website")), "Website ↗"),
               button(safe_url(r.get("linkedin_url")), "LinkedIn ↗")]
    srcs = []
    if safe_email(r.get("email")) and clean(r.get("email_source")):
        srcs.append(f"Email: {esc(clean(r.get('email_source'))[:160])}")
    cs = link(r.get("contact_source"), "contact page") if safe_url(r.get("contact_source")) else ""
    if cs:
        srcs.append(f"Published on {cs}")
    srcs.append(f"Found via {esc(provider or 'public source')}")
    if clean(r.get("last_active")):
        srcs.append(f"last active {esc(clean(r.get('last_active')))}")
    body = _join(f'<p class="jf-title">{n_html}</p>', f'<div class="jf-meta">{meta}</div>' if meta else "",
                 f'<div class="jf-badges">{"".join(badges)}</div>', "".join(lines), score,
                 f'<div class="jf-actions">{"".join(a for a in actions if a)}</div>' if any(actions) else "",
                 f'<div class="jf-src">{" · ".join(srcs)}</div>')
    return card(body, ckind)


# ------------------------------------------------------------------ employers
def employer_card(r: dict, locum_friendly: bool = False) -> str:
    name = clean(r.get("name")) or "Employer"
    web = safe_url(r.get("website"))
    n_html = f'<a href="{esc(web)}" target="_blank" rel="noopener noreferrer">{esc(name)}</a>' if web else esc(name)
    badges = [badge(clean(r.get("employer_type")) or "Employer", "neutral")]
    vt = clean(r.get("vacancy_type"))
    if locum_friendly:
        badges.append(badge("Locum / bank friendly", "locum"))
    if clean(r.get("vacancy")):
        badges.append(badge("Locum / bank vacancy" if vt.startswith("locum") else "Open vacancy",
                            "locum" if vt.startswith("locum") else "perm"))
    if safe_url(r.get("bank_url")):
        badges.append(badge("Join-the-bank page", "locum"))
    dist = _num(r.get("distance_miles"))
    meta = " · ".join(esc(x) for x in (", ".join(x for x in (clean(r.get("address")), clean(r.get("postcode"))) if x),
                                       f"{dist} miles away" if dist else "") if x)
    lines = []
    if clean(r.get("vacancy")):
        v = clean(r.get("vacancy"))
        vu = safe_url(r.get("vacancy_url"))
        lines.append('<div class="jf-line">📌 ' + (f'<a href="{esc(vu)}" target="_blank" rel="noopener noreferrer">{esc(v[:140])}</a>' if vu else esc(v[:140]))
                     + "</div>")
    if clean(r.get("contact_name")):
        lines.append(f'<div class="jf-line">👤 {esc(clean(r.get("contact_name")))}'
                     + (f' ({esc(clean(r.get("contact_role")))})' if clean(r.get("contact_role")) else "")
                     + ' <span class="jf-src">named in the official register, not a job-seeker</span></div>')
    actions = [phone_button(r.get("phone")), email_button(r.get("email")), button(web, "Website ↗"),
               button(safe_url(r.get("bank_url")), "Join the bank / locum ↗", primary=True),
               button(safe_url(r.get("careers_url")), "Careers ↗"), button(safe_url(r.get("vacancy_url")), "Vacancy ↗")]
    src = clean(r.get("source"))
    su = link(r.get("source_url"), "record") if safe_url(str(r.get("source_url") or "").split()[0] if clean(r.get("source_url")) else "") else ""
    foot = f'<div class="jf-src">Source: {esc(src)}' + (f" · {su}" if su else "") + (
        f" · {esc(clean(r.get('note')))}" if clean(r.get("note")) else "") + "</div>"
    body = _join(f'<p class="jf-title">{n_html}</p>', f'<div class="jf-meta">{meta}</div>' if meta else "",
                 f'<div class="jf-badges">{"".join(badges)}</div>', "".join(lines),
                 f'<div class="jf-actions">{"".join(a for a in actions if a)}</div>' if any(actions) else "", foot)
    return card(body, "locum" if (locum_friendly or vt.startswith("locum") or safe_url(r.get("bank_url"))) else "")
