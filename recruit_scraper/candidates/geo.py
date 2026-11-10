"""UK postcode geocoding via postcodes.io (free, open data, no key)."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import requests

from ..http import HttpClient

log = logging.getLogger(__name__)
API = "https://api.postcodes.io"
MILES = 1609.344
_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.I)
_OUTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$", re.I)


class PostcodeError(ValueError):
    pass


@dataclass
class Place:
    postcode: str
    lat: float
    lon: float
    town: str            # admin district, e.g. "Manchester"
    region: str          # e.g. "North West"
    country: str
    nearby_towns: list[str] = field(default_factory=list)  # districts within the radius, nearest/most common first
    nationwide: bool = False   # a whole-UK search ('UK' / 'United Kingdom' instead of a postcode)


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


NATIONWIDE_WORDS = {"UK", "U.K.", "UNITED KINGDOM", "GB", "GREAT BRITAIN", "ALL UK", "WHOLE UK", "UK-WIDE",
                    "UK WIDE", "NATIONWIDE", "ANYWHERE IN THE UK"}
# centre of the UK (near Dunsop Bridge) - every UK town is within ~450 miles, so 'nationwide' = a 600-mile radius
UK_CENTRE = (54.0, -2.55)
NATIONWIDE_MILES = 600.0


def is_nationwide(text: str) -> bool:
    return " ".join((text or "").strip().upper().split()) in NATIONWIDE_WORDS


def nationwide_place() -> Place:
    return Place(postcode="UK", lat=UK_CENTRE[0], lon=UK_CENTRE[1], town="United Kingdom", region="",
                 country="United Kingdom", nearby_towns=[], nationwide=True)


def lookup_postcode(http: HttpClient, postcode: str, radius_miles: float = 10) -> Place:
    if is_nationwide(postcode):
        return nationwide_place()   # no lookup needed: every UK nation, no distance limit
    pc = " ".join(postcode.strip().upper().split())
    try:
        if _POSTCODE_RE.match(pc):
            r = http.get_json(f"{API}/postcodes/{pc.replace(' ', '')}")["result"]
            town, region = r.get("admin_district") or "", r.get("region") or ""
        elif _OUTCODE_RE.match(pc):
            r = http.get_json(f"{API}/outcodes/{pc}")["result"]
            town = (r.get("admin_district") or [""])[0]
            region = (r.get("region") or [""])[0] if isinstance(r.get("region"), list) else (r.get("region") or "")
        else:
            raise PostcodeError(f"'{postcode}' doesn't look like a UK postcode (e.g. M1 1AE or M1) - or type UK "
                                "for a whole-UK search")
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 404:
            raise PostcodeError(f"postcode '{postcode}' not found") from e
        raise
    place = Place(postcode=pc, lat=r["latitude"], lon=r["longitude"], town=town, region=region or "",
                  country=r.get("country") if isinstance(r.get("country"), str) else (r.get("country") or [""])[0])
    place.nearby_towns = nearby_towns(http, place.lat, place.lon, radius_miles, home=town)
    return place


def nearby_towns(http: HttpClient, lat: float, lon: float, radius_miles: float, home: str = "") -> list[str]:
    """Local-authority names whose outcodes fall within the radius (postcodes.io caps this at 25 km)."""
    radius_m = int(min(max(radius_miles, 1) * MILES, 25000))
    try:
        res = http.get_json(f"{API}/outcodes", params={"lon": lon, "lat": lat, "radius": radius_m, "limit": 100})
        rows = res.get("result") or []
    except requests.RequestException as e:
        log.warning("nearby outcodes lookup failed: %s", e)
        rows = []
    counts = Counter(d for row in rows for d in (row.get("admin_district") or []))
    towns = [home] if home else []
    towns += [t for t, _ in counts.most_common() if t and t != home]
    return towns


_NON_UK = re.compile(
    r"\b(usa|u\.s\.a?|united states|america|canada|india|australia|new zealand|germany|france|spain|italy|"
    r"netherlands|ireland|nigeria|kenya|pakistan|brazil|mexico|china|japan|singapore|south africa|"
    r"new hampshire|connecticut|jamaica|tennessee|kentucky|iowa|ohio|texas|california|new york)\b", re.I)


def looks_non_uk(text: str) -> bool:
    """True for locations that are clearly outside the UK (e.g. 'Manchester, NH' or 'Manchester, USA')."""
    if _NON_UK.search(text or ""):
        return True
    parts = [p.strip() for p in (text or "").split(",")]
    return len(parts) > 1 and bool(re.fullmatch(r"[A-Z]{2}", parts[-1])) and parts[-1] not in ("UK", "GB")


class PlaceGeocoder:
    """Free-text UK place name -> (lat, lon) using postcodes.io /places, cached."""

    def __init__(self, http: HttpClient):
        self.http = http
        self.cache: dict[str, Optional[tuple[float, float]]] = {}

    def geocode(self, text: str) -> Optional[tuple[float, float]]:
        if not text or looks_non_uk(text):
            return None
        for part in re.split(r"[,/|;()]| - ", text):
            name = part.strip()
            if not name or name.lower() in {"uk", "united kingdom", "england", "scotland", "wales", "gb",
                                             "great britain", "remote", "earth", "worldwide", "europe"}:
                continue
            key = name.lower()
            if key not in self.cache:
                self.cache[key] = None
                try:
                    if _POSTCODE_RE.match(name) or _OUTCODE_RE.match(name):
                        url = f"{API}/postcodes/{name.replace(' ', '')}" if _POSTCODE_RE.match(name) else f"{API}/outcodes/{name}"
                        r = self.http.get_json(url)["result"]
                        self.cache[key] = (r["latitude"], r["longitude"])
                    else:
                        res = self.http.get_json(f"{API}/places", params={"q": name, "limit": 5}).get("result") or []
                        exact = [p for p in res if p.get("name_1", "").lower() == key] or res
                        pref = [p for p in exact if p.get("local_type") in ("City", "Town")] or exact
                        if pref:
                            self.cache[key] = (pref[0]["latitude"], pref[0]["longitude"])
                except requests.RequestException:
                    pass
            if self.cache.get(key):
                return self.cache[key]
        return None


# Cities / large towns people name in bios. Case-sensitive whole-word match, so ordinary words are not mistaken for
# places; ambiguous names (Reading, Bath, Sale, Wells…) are left out on purpose.
UK_TOWNS = (
    "London Birmingham Manchester Liverpool Leeds Sheffield Bristol Newcastle Sunderland Nottingham Leicester Coventry "
    "Bradford Wakefield Hull Stoke Wolverhampton Derby Plymouth Southampton Portsmouth Brighton Bournemouth Poole "
    "Exeter Truro Swindon Oxford Cambridge Norwich Ipswich Colchester Chelmsford Southend Luton Milton Keynes "
    "Northampton Peterborough Lincoln York Harrogate Scarborough Middlesbrough Durham Darlington Carlisle Preston "
    "Blackpool Blackburn Burnley Bolton Wigan Warrington Chester Stockport Oldham Rochdale Salford Huddersfield "
    "Halifax Doncaster Rotherham Barnsley Grimsby Gloucester Cheltenham Worcester Hereford Shrewsbury Telford "
    "Canterbury Maidstone Dover Folkestone Guildford Woking Crawley Hastings Eastbourne Worthing Chichester "
    "Winchester Basingstoke Salisbury Taunton Yeovil Torquay Barnstaple Watford Stevenage Slough Windsor "
    "Cardiff Swansea Newport Wrexham Aberystwyth Bangor Glasgow Edinburgh Aberdeen Dundee Inverness Stirling Perth "
    "Paisley Belfast Derry Lisburn Newry Cornwall Devon Somerset Dorset Kent Sussex Surrey Essex Suffolk Norfolk "
    "Yorkshire Lancashire Cumbria Northumberland Merseyside Cheshire Shropshire Staffordshire Derbyshire "
    "Nottinghamshire Lincolnshire Leicestershire Warwickshire Worcestershire Gloucestershire Wiltshire Hampshire "
    "Berkshire Oxfordshire Buckinghamshire Hertfordshire Bedfordshire Cambridgeshire Northamptonshire Teesside "
    "Tyneside Midlands Highlands Fife Ayrshire Lanarkshire Glamorgan Gwynedd Pembrokeshire Anglesey").split()
_MULTI = ("Milton Keynes", "Stoke-on-Trent", "Isle of Wight", "Isle of Man", "Greater Manchester", "West Midlands",
          "East Midlands", "Lake District", "Scottish Borders", "North Wales", "South Wales")
_TOWN_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(t) for t in sorted(set(UK_TOWNS) - {"Milton", "Keynes"}
                                                                        | set(_MULTI), key=len, reverse=True))
                      + r")(?![A-Za-z])")
_NATIONS = {"England": r"(?<!New )\bEngland\b|\bEnglish\b", "Scotland": r"\bScotland\b|\bScottish\b|\bAlba\b",
            "Wales": r"\bWales\b|\bWelsh\b|\bCymru\b", "Northern Ireland": r"\bNorthern Ireland\b|\bNI\b"}
_COUNTIES = {"Cornwall", "Devon", "Somerset", "Dorset", "Kent", "Sussex", "Surrey", "Essex", "Suffolk", "Norfolk",
             "Yorkshire", "Lancashire", "Cumbria", "Northumberland", "Merseyside", "Cheshire", "Shropshire",
             "Staffordshire", "Derbyshire", "Nottinghamshire", "Lincolnshire", "Leicestershire", "Warwickshire",
             "Worcestershire", "Gloucestershire", "Wiltshire", "Hampshire", "Berkshire", "Oxfordshire",
             "Buckinghamshire", "Hertfordshire", "Bedfordshire", "Cambridgeshire", "Northamptonshire", "Teesside",
             "Tyneside", "Midlands", "Highlands", "Fife", "Ayrshire", "Lanarkshire", "Glamorgan", "Gwynedd",
             "Pembrokeshire", "Anglesey", "Greater Manchester", "West Midlands", "East Midlands", "Lake District",
             "Scottish Borders", "North Wales", "South Wales", "Isle of Wight", "Isle of Man"}


def far_uk_place(text: str, place: "Place", radius_miles: float, geocoder: "PlaceGeocoder",
                 slack: float = 3) -> Optional[str]:
    """The first UK town/area named in `text` that is clearly outside the search radius (or another UK nation), so
    a 'UK-based, no nearby town' profile that actually says 'Cardiff' or 'Preston-based' is not shown for Leeds."""
    t = text or ""
    country = (place.country or "").strip()
    named = {n for n, rx in _NATIONS.items() if re.search(rx, t)}
    if country in _NATIONS and named and country not in named:
        return sorted(named)[0]
    for m in _TOWN_RE.finditer(t):
        name = m.group(1)
        if name in _COUNTIES:
            continue  # counties/regions are too big to judge distance from one point
        co = geocoder.geocode(name)
        if co and haversine_miles(place.lat, place.lon, *co) > radius_miles + slack:
            return name
    return None
