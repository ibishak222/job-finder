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


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def lookup_postcode(http: HttpClient, postcode: str, radius_miles: float = 10) -> Place:
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
            raise PostcodeError(f"'{postcode}' doesn't look like a UK postcode (e.g. M1 1AE or M1)")
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
