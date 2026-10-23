"""Central Texas locating for community match.

Street rooftops are optional and server-side. The default is a ZIP or town
center, labeled as such, so a family's address is not sent to a public
geocoder unless an operator turns that on. Distances are straight-line.
"""

from __future__ import annotations

import math
import re
import time
import urllib.parse
import urllib.request
from typing import Callable

from bestfit.domain import AUSTIN, DIRECTORY_RADIUS_MILES


# Approximate civic centers. Used when a street rooftop is not resolved.
PLACES: dict[str, tuple[float, float]] = {
    "78645": (30.4602, -97.9884),
    "lago vista": (30.4602, -97.9884),
    "78701": (30.2729, -97.7444),
    "78702": (30.2636, -97.7145),
    "78703": (30.2934, -97.7664),
    "78704": (30.2430, -97.7636),
    "78705": (30.2920, -97.7380),
    "78731": (30.3470, -97.7680),
    "78746": (30.2960, -97.8110),
    "78734": (30.3780, -97.9640),
    "lakeway": (30.3638, -97.9797),
    "78738": (30.3085, -97.9450),
    "bee cave": (30.3085, -97.9450),
    "78613": (30.5052, -97.8203),
    "cedar park": (30.5052, -97.8203),
    "78664": (30.5083, -97.6789),
    "round rock": (30.5083, -97.6789),
    "78660": (30.4394, -97.6200),
    "pflugerville": (30.4394, -97.6200),
    "78626": (30.6333, -97.6772),
    "georgetown": (30.6333, -97.6772),
    "78620": (30.1902, -98.0867),
    "dripping springs": (30.1902, -98.0867),
    "78640": (29.9891, -97.8772),
    "kyle": (29.9891, -97.8772),
    "78666": (29.8833, -97.9414),
    "san marcos": (29.8833, -97.9414),
    "78602": (30.1105, -97.3153),
    "bastrop": (30.1105, -97.3153),
    "76574": (30.5708, -97.4094),
    "taylor": (30.5708, -97.4094),
    "78611": (30.7582, -98.2284),
    "burnet": (30.7582, -98.2284),
    "78130": (29.7030, -98.1245),
    "new braunfels": (29.7030, -98.1245),
    "76501": (31.0982, -97.3428),
    "temple": (31.0982, -97.3428),
    "76701": (31.5493, -97.1467),
    "waco": (31.5493, -97.1467),
    "76541": (31.1171, -97.7278),
    "killeen": (31.1171, -97.7278),
    "75201": (32.7876, -96.7990),
    "dallas": (32.7767, -96.7970),
    "austin": (30.2672, -97.7431),
}

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def haversine(origin: tuple[float, float], dest: tuple[float, float]) -> float:
    lat1, lon1 = origin
    lat2, lon2 = dest
    radius = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _zip_code(query: str) -> str | None:
    match = ZIP_RE.search(query or "")
    return match.group(1) if match else None


def _place_key(query: str) -> str | None:
    text = " ".join((query or "").lower().replace(",", " ").split())
    if text in PLACES:
        return text
    for name in sorted(PLACES, key=len, reverse=True):
        if not name.isdigit() and name in text:
            return name
    return None


class LocateResult(dict):
    """lat, lng, source, label, miles_from_austin, out_of_area, detail."""


def locate_offline(query: str) -> LocateResult | None:
    text = (query or "").strip()
    if not text:
        return None
    zip_code = _zip_code(text)
    streetish = bool(re.search(r"\d", text)) and not re.fullmatch(r"\d{5}(?:-\d{4})?", text.strip())
    if zip_code and zip_code in PLACES:
        lat, lng = PLACES[zip_code]
        source = "zip"
        label = f"ZIP {zip_code}"
        detail = (
            f"Located from ZIP {zip_code}, not the street rooftop."
            if streetish
            else f"Located at the ZIP {zip_code} center."
        )
    else:
        key = _place_key(text)
        if key is None or key.isdigit():
            if zip_code:
                return LocateResult(
                    lat=None,
                    lng=None,
                    source="unknown",
                    label=zip_code,
                    miles_from_austin=None,
                    out_of_area=False,
                    detail=f"No center is on file for ZIP {zip_code}. Try a Central Texas town.",
                )
            return LocateResult(
                lat=None,
                lng=None,
                source="unknown",
                label=text,
                miles_from_austin=None,
                out_of_area=False,
                detail="Enter a Central Texas town or ZIP. A street alone is not enough unless server geocoding is on.",
            )
        lat, lng = PLACES[key]
        source = "town"
        label = key.title()
        detail = f"Located at the {key.title()} center, not a street rooftop."
    miles = haversine(AUSTIN, (lat, lng))
    out = miles > DIRECTORY_RADIUS_MILES
    if out:
        detail = (
            f"{label} is about {round(miles)} miles from Austin, outside the "
            f"Central Texas directory (about {DIRECTORY_RADIUS_MILES} miles)."
        )
    return LocateResult(
        lat=lat,
        lng=lng,
        source=source,
        label=label,
        miles_from_austin=round(miles, 1),
        out_of_area=out,
        detail=detail,
    )


def nominatim_search(query: str, opener: Callable | None = None) -> tuple[float, float] | None:
    params = urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"}
    )
    request = urllib.request.Request(
        "https://nominatim.openstreetmap.org/search?" + params,
        headers={"User-Agent": "BestFitSeniorPlacement/1.0 (private placement CRM; server-side)"},
    )
    open_fn = opener or urllib.request.urlopen
    with open_fn(request, timeout=8) as response:
        import json

        payload = json.loads(response.read().decode())
    if not payload:
        return None
    return float(payload[0]["lat"]), float(payload[0]["lon"])


class Geocoder:
    """Caches rooftop lookups. At most one network call per second."""

    def __init__(self, enabled: bool, cache: dict[str, tuple[float, float]], opener=None):
        self.enabled = enabled
        self.cache = cache
        self.opener = opener
        self._last_call = 0.0

    def _remember(self, key: str, text: str, point: tuple[float, float], detail: str) -> LocateResult:
        self.cache[key] = point
        lat, lng = point
        miles = haversine(AUSTIN, point)
        out = miles > DIRECTORY_RADIUS_MILES
        if out:
            detail = (
                f"That point is about {round(miles)} miles from Austin, outside the "
                f"Central Texas directory (about {DIRECTORY_RADIUS_MILES} miles)."
            )
        return LocateResult(
            lat=lat,
            lng=lng,
            source="nominatim",
            label=text,
            miles_from_austin=round(miles, 1),
            out_of_area=out,
            detail=detail,
            store_cache=True,
        )

    def _network(self, key: str, text: str) -> LocateResult | None:
        wait = 1.0 - (time.monotonic() - self._last_call)
        if wait > 0 and self._last_call:
            time.sleep(wait)
        self._last_call = time.monotonic()
        try:
            point = nominatim_search(text, self.opener)
        except Exception:
            point = None
        if point is None:
            return None
        return self._remember(key, text, point, "Located on the server from the address.")

    def locate(self, query: str) -> LocateResult | None:
        text = (query or "").strip()
        if not text:
            return None
        key = text.lower()
        if self.enabled and key in self.cache:
            lat, lng = self.cache[key]
            miles = haversine(AUSTIN, (lat, lng))
            out = miles > DIRECTORY_RADIUS_MILES
            detail = "Located from the cached server geocode."
            if out:
                detail = (
                    f"That point is about {round(miles)} miles from Austin, outside the "
                    f"Central Texas directory (about {DIRECTORY_RADIUS_MILES} miles)."
                )
            return LocateResult(
                lat=lat,
                lng=lng,
                source="cache",
                label=text,
                miles_from_austin=round(miles, 1),
                out_of_area=out,
                detail=detail,
            )
        streetish = bool(re.search(r"\d", text)) and not re.fullmatch(r"\d{5}(?:-\d{4})?", text)
        # A known town or ZIP stays on the built-in center. A street, or a place
        # that is not on file, can use the server geocoder when it is enabled.
        if self.enabled and streetish:
            located = self._network(key, text)
            if located is not None:
                return located
        result = locate_offline(text)
        if self.enabled and (result is None or result.get("lat") is None):
            located = self._network(key, text)
            if located is not None:
                return located
        return result


def rank_communities(lead: tuple[float, float], communities: list[dict], radius: float, care: str, name: str) -> list[dict]:
    care_key = (care or "").strip().upper()
    name_key = (name or "").strip().lower()
    ranked = []
    for community in communities:
        if community.get("lat") is None or community.get("lng") is None:
            continue
        types = [part.strip().upper() for part in (community.get("care_types") or "").split(",") if part.strip()]
        if care_key and care_key not in types:
            continue
        if name_key and name_key not in community["name"].lower():
            continue
        miles = haversine(lead, (community["lat"], community["lng"]))
        if miles > radius:
            continue
        ranked.append({**community, "miles": round(miles, 1)})
    ranked.sort(key=lambda row: (row["miles"], row["name"]))
    return ranked[:20]


def project(lead: tuple[float, float], points: list[tuple[float, float]], radius_miles: float) -> list[tuple[float, float]]:
    """Map lat/lng into a 0..100 box that includes the lead and the radius."""
    lat_pad = radius_miles / 69.0
    lng_pad = radius_miles / max(1.0, 69.0 * math.cos(math.radians(lead[0])))
    lats = [lead[0] - lat_pad, lead[0] + lat_pad] + [point[0] for point in points]
    lngs = [lead[1] - lng_pad, lead[1] + lng_pad] + [point[1] for point in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    if max_lat == min_lat:
        max_lat += 0.01
    if max_lng == min_lng:
        max_lng += 0.01

    def xy(lat: float, lng: float) -> tuple[float, float]:
        x = (lng - min_lng) / (max_lng - min_lng) * 100
        y = (max_lat - lat) / (max_lat - min_lat) * 100
        return round(x, 2), round(y, 2)

    return [xy(lat, lng) for lat, lng in [lead, *points]]
