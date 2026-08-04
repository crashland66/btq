"""ZIP-centroid geography for coverage-opportunity ranking.

Fully local by design: distances derive from the bundled US Census ZCTA
gazetteer (public domain, 2023 vintage, project/geodata/zcta_centroids_2023.csv)
— employee addresses never leave the machine and no third-party geocoder is
involved. Coordinates are derived at query time from postal codes; nothing
geographic is stored on canonical docs, so an address correction needs no
invalidation step. Exact-address geocoding stays an explicit operator
decision per the coverage-opportunities design spec.
"""

from __future__ import annotations

import csv
import math
import re
from functools import lru_cache
from pathlib import Path

_CENTROIDS_CSV = Path(__file__).resolve().parent / "geodata" / "zcta_centroids_2023.csv"
_ZIP5_RE = re.compile(r"\d{5}")
_EARTH_RADIUS_MILES = 3958.8


@lru_cache(maxsize=1)
def _centroids() -> dict[str, tuple[float, float]]:
    table: dict[str, tuple[float, float]] = {}
    with _CENTROIDS_CSV.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            table[row["zcta"]] = (float(row["lat"]), float(row["lon"]))
    return table


def postal_code_zip5(value: object) -> str:
    """First 5-digit run in a postal string ("15935-6416", "159356416")."""
    match = _ZIP5_RE.search(str(value or ""))
    return match.group(0) if match else ""


def zip_centroid(postal_code: object) -> tuple[float, float] | None:
    zip5 = postal_code_zip5(postal_code)
    return _centroids().get(zip5) if zip5 else None


def site_postal_code(location_doc: dict) -> str:
    """ZIP from a location doc's flat address string.

    Site addresses end in the postal component ("..., PA, 159356416"), so the
    LAST 5-digit run wins — a 5-digit street number at the front never does.
    """
    matches = _ZIP5_RE.findall(str(location_doc.get("address") or ""))
    return matches[-1] if matches else ""


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return _EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(h))
