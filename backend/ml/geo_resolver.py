"""
VIP ML — GPS coordinate → human-readable place name.

Uses geopy Nominatim (OpenStreetMap) — free, no API key required.
Network requests are made on demand; results are not cached in-process
(SQLite is the persistent cache via the tags table).

Returns place name at multiple granularities:
  - Specific: "Darling Harbour" or "Times Square"
  - City:     "Sydney" or "New York City"
  - Country:  "Australia" or "United States"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SEC = 10
_USER_AGENT = "VIP-VisualIntelligencePlatform/1.0 (local-use)"


@dataclass
class GeoTag:
    label: str      # e.g. "Darling Harbour, Sydney, Australia"
    locality: str   # e.g. "Sydney"
    country: str    # e.g. "Australia"
    confidence: float = 1.0  # always 1.0 for GPS-derived places


class GeoResolver:
    """Reverse-geocodes GPS coordinates to place names via Nominatim."""

    def __init__(self) -> None:
        self._geolocator = None

    def load(self) -> None:
        """Initialise the geolocator. Safe to call multiple times."""
        if self._geolocator is not None:
            return
        try:
            from geopy.geocoders import Nominatim
            self._geolocator = Nominatim(
                user_agent=_USER_AGENT,
                timeout=_REQUEST_TIMEOUT_SEC,
            )
            logger.info("✅  GeoResolver ready (Nominatim/OSM)")
        except ImportError as e:
            logger.warning("geopy unavailable — install geopy: %s", e)

    def resolve(self, lat: float, lon: float) -> GeoTag | None:
        """
        Reverse-geocode a GPS coordinate pair.

        Args:
            lat: Latitude (-90 to 90).
            lon: Longitude (-180 to 180).

        Returns:
            GeoTag with place name, or None on failure / no result.
        """
        if self._geolocator is None:
            return None

        try:
            location = self._geolocator.reverse(
                f"{lat:.6f}, {lon:.6f}",
                language="en",
                addressdetails=True,
            )
            if location is None:
                return None

            addr = location.raw.get("address", {})
            country = addr.get("country", "")
            city = (
                addr.get("city")
                or addr.get("town")
                or addr.get("municipality")
                or addr.get("county")
                or ""
            )
            # Most specific named feature
            specific = (
                addr.get("tourism")        # e.g. "Sydney Opera House"
                or addr.get("amenity")
                or addr.get("leisure")
                or addr.get("natural")
                or addr.get("suburb")
                or addr.get("neighbourhood")
                or city
            )

            parts = [p for p in [specific, city, country] if p and p != specific or p == country]
            # Deduplicate while preserving order
            seen: set[str] = set()
            label_parts: list[str] = []
            for part in [specific, city, country]:
                if part and part not in seen:
                    seen.add(part)
                    label_parts.append(part)

            label = ", ".join(label_parts) if label_parts else location.address

            logger.debug("Resolved (%.4f, %.4f) → %s", lat, lon, label)
            return GeoTag(label=label, locality=city, country=country)

        except Exception as e:
            logger.warning("Geo resolution failed for (%.4f, %.4f): %s", lat, lon, e)
            return None
