"""
VIP ML — GPS coordinate → human-readable place name.

Primary:  Apple MapKit CLGeocoder via a compiled Swift helper binary.
          Zero credentials, zero rate limit, same data quality as Maps.app.
          Returns landmark/POI names (e.g. "Sydney Opera House") for
          well-known places, and structured address fields for everything else.

Fallback: Nominatim (OpenStreetMap) — free, no API key, 1 req/sec limit.
          Used automatically when the Swift binary is unavailable or returns
          no result (e.g. offline, network error, unmapped location).

GeoTag fields:
  label    — "Sydney Opera House, Sydney, NSW, Australia"
  locality — "Sydney"
  country  — "Australia"
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SEC = 10
_USER_AGENT = "VIP-VisualIntelligencePlatform/1.0 (local-use)"

# Nominatim rate-limit: OSM policy is ≤1 req/sec; use 1.1s to be safe.
_NOMINATIM_MIN_INTERVAL = 1.1

# Coordinate cache grid precision.
# 0.01° ≈ 1.1 km at the equator (slightly less at higher latitudes).
# Two photos within this grid cell share the same resolved place.
_CACHE_GRID = 2  # decimal places → 0.01°

# Locate the Swift source and compiled binary relative to this file.
# geo_resolver.py lives at  backend/ml/geo_resolver.py
# Scripts dir is at         ../../scripts  (project root / scripts)
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
_SWIFT_SOURCE = _SCRIPTS_DIR / "vip_geocode.swift"
_SWIFT_BINARY = _SCRIPTS_DIR / "vip_geocode"


@dataclass
class GeoTag:
    label: str      # e.g. "Sydney Opera House, Sydney, NSW, Australia"
    locality: str   # e.g. "Sydney"
    country: str    # e.g. "Australia"
    confidence: float = 1.0  # always 1.0 for GPS-derived places
    source: str = "unknown"  # "mapkit" | "nominatim"


class GeoResolver:
    """
    Reverse-geocodes GPS coordinates to human-readable place names.

    Uses Apple MapKit CLGeocoder as primary (via a compiled Swift CLI helper)
    with Nominatim/OSM as automatic fallback.
    """

    def __init__(self) -> None:
        self._geolocator = None       # Nominatim instance
        self._mapkit_ok: bool = False  # True once Swift binary is ready

        # Coordinate deduplication cache.
        # Key: (round(lat, 2), round(lon, 2))  → ~1 km grid cell.
        # Value: GeoTag (resolved result) or None (tried, got nothing).
        # Shared across tag_batch calls so photos from the same trip only
        # hit the API once regardless of batch order.
        self._coord_cache: dict[tuple[float, float], GeoTag | None] = {}
        self._cache_lock = threading.Lock()

        # Nominatim throttle — wall-clock time of the last Nominatim call.
        self._last_nomi_time: float = 0.0
        self._nomi_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Prepare both backends. Safe to call multiple times."""
        self._mapkit_ok = self._ensure_binary()

        # Always initialise Nominatim as fallback (import may fail on minimal
        # envs — that's fine, _try_nominatim() guards against None).
        try:
            from geopy.geocoders import Nominatim
            self._geolocator = Nominatim(
                user_agent=_USER_AGENT,
                timeout=_REQUEST_TIMEOUT_SEC,
            )
        except ImportError as e:
            logger.warning("geopy unavailable — Nominatim fallback disabled: %s", e)

        if self._mapkit_ok and self._geolocator:
            logger.info("✅  GeoResolver ready (Apple MapKit primary · Nominatim fallback)")
        elif self._mapkit_ok:
            logger.info("✅  GeoResolver ready (Apple MapKit only — geopy not installed)")
        elif self._geolocator:
            logger.info("✅  GeoResolver ready (Nominatim/OSM — Swift binary unavailable)")
        else:
            logger.warning("⚠️  GeoResolver: no backend available")

    # ── Swift binary management ──────────────────────────────────────────────

    def _ensure_binary(self) -> bool:
        """
        Compile vip_geocode.swift if the binary is missing or stale.
        Returns True if the binary exists and is ready to use.
        """
        if not _SWIFT_SOURCE.exists():
            logger.warning("vip_geocode.swift not found at %s — MapKit geocoding disabled", _SWIFT_SOURCE)
            return False

        # Binary exists and is at least as new as the source — no recompile needed.
        if (
            _SWIFT_BINARY.exists()
            and _SWIFT_BINARY.stat().st_mtime >= _SWIFT_SOURCE.stat().st_mtime
        ):
            return True

        # Check for swiftc
        check = subprocess.run(["which", "swiftc"], capture_output=True, text=True)
        if check.returncode != 0:
            logger.warning(
                "swiftc not found — install Xcode Command Line Tools for Apple MapKit geocoding. "
                "Falling back to Nominatim."
            )
            return False

        logger.info("Compiling vip_geocode Swift binary (one-time) …")
        try:
            result = subprocess.run(
                [
                    "swiftc",
                    str(_SWIFT_SOURCE),
                    "-framework", "MapKit",
                    "-framework", "CoreLocation",
                    "-o", str(_SWIFT_BINARY),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning(
                    "Failed to compile vip_geocode.swift:\n%s", result.stderr.strip()
                )
                return False
            _SWIFT_BINARY.chmod(0o755)
            logger.info("✅  vip_geocode binary compiled: %s", _SWIFT_BINARY)
            return True
        except Exception as exc:
            logger.warning("Could not compile vip_geocode.swift: %s", exc)
            return False

    # ── MapKit (primary) ─────────────────────────────────────────────────────

    def _try_mapkit(self, lat: float, lon: float) -> GeoTag | None:
        """Call CLGeocoder via the compiled Swift binary."""
        if not self._mapkit_ok or not _SWIFT_BINARY.exists():
            return None
        try:
            result = subprocess.run(
                [str(_SWIFT_BINARY), str(lat), str(lon)],
                capture_output=True,
                text=True,
                timeout=_REQUEST_TIMEOUT_SEC,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            data: dict = json.loads(result.stdout)
            return self._build_geotag_from_mapkit(data)
        except Exception as exc:
            logger.debug("MapKit geocoding failed for (%.4f, %.4f): %s", lat, lon, exc)
            return None

    def _build_geotag_from_mapkit(self, data: dict) -> GeoTag | None:
        """Convert CLGeocoder / MKReverseGeocodingRequest JSON into a GeoTag."""
        name = data.get("name", "")
        locality = data.get("locality") or data.get("subAdministrativeArea") or ""
        sub_locality = data.get("subLocality", "")

        # MKReverseGeocodingRequest (macOS 26+) returns cityWithContext ("Sydney, NSW")
        # CLGeocoder returns administrativeArea separately ("NSW").
        # We prefer cityWithContext because it already combines city + state abbreviation.
        city_with_context = data.get("cityWithContext", "")   # "Sydney, NSW"
        admin_area = data.get("administrativeArea", "")       # "NSW" (CLGeocoder fallback)

        country = data.get("country", "")
        areas_of_interest: list[str] = data.get("areasOfInterest") or []
        ocean = data.get("ocean", "")
        inland_water = data.get("inlandWater", "")

        # Photos taken at sea / on a lake
        if ocean and not locality:
            label = ", ".join(p for p in [ocean, country] if p)
            return GeoTag(label=label or ocean, locality=ocean, country=country or ocean)
        if inland_water and not locality:
            label = ", ".join(p for p in [inland_water, country] if p)
            return GeoTag(label=label or inland_water, locality=inland_water, country=country)

        # Most specific named feature — preference order:
        #   1. areasOfInterest[0]  — explicitly tagged POI (park, stadium, landmark)
        #   2. name                — if it's a named place, not a street address
        #                            (street addresses start with a digit)
        #   3. subLocality         — suburb / district
        #   4. locality            — city (last resort)
        if areas_of_interest:
            specific = areas_of_interest[0]
        elif name and not re.match(r"^\d", name.strip()):
            specific = name
        elif sub_locality:
            specific = sub_locality
        else:
            specific = locality

        # Build label: specific → cityWithContext (or locality + adminArea) → country
        # "Sydney Opera House, Sydney, NSW, Australia"
        seen: set[str] = set()
        parts: list[str] = []

        if city_with_context:
            # MKReverseGeocodingRequest path: "Sydney Opera House, Sydney, NSW, Australia"
            for part in [specific, city_with_context, country]:
                if part and part not in seen:
                    seen.add(part)
                    parts.append(part)
        else:
            # CLGeocoder fallback path
            for part in [specific, locality, admin_area, country]:
                if part and part not in seen:
                    seen.add(part)
                    parts.append(part)

        label = ", ".join(parts) or name or country
        if not label:
            return None

        return GeoTag(label=label, locality=locality or specific, country=country)

    # ── Nominatim (fallback) ─────────────────────────────────────────────────

    def _try_nominatim(self, lat: float, lon: float) -> GeoTag | None:
        """Reverse-geocode via Nominatim/OpenStreetMap."""
        if self._geolocator is None:
            return None

        # Enforce Nominatim rate limit (1 req / 1.1 s).
        # Block the calling thread (already in run_in_executor) rather than
        # the event loop, so the rest of the pipeline is not affected.
        with self._nomi_lock:
            wait = _NOMINATIM_MIN_INTERVAL - (time.monotonic() - self._last_nomi_time)
            if wait > 0:
                logger.debug("Nominatim throttle: sleeping %.2fs", wait)
                time.sleep(wait)
            self._last_nomi_time = time.monotonic()

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
            specific = (
                addr.get("tourism")
                or addr.get("amenity")
                or addr.get("leisure")
                or addr.get("natural")
                or addr.get("suburb")
                or addr.get("neighbourhood")
                or city
            )

            seen: set[str] = set()
            label_parts: list[str] = []
            for part in [specific, city, country]:
                if part and part not in seen:
                    seen.add(part)
                    label_parts.append(part)

            label = ", ".join(label_parts) if label_parts else location.address

            logger.debug("Nominatim (%.4f, %.4f) → %s", lat, lon, label)
            return GeoTag(label=label, locality=city, country=country)

        except Exception as exc:
            logger.warning("Nominatim failed for (%.4f, %.4f): %s", lat, lon, exc)
            return None

    # ── Public API ───────────────────────────────────────────────────────────

    def resolve(self, lat: float, lon: float) -> Optional[GeoTag]:
        """
        Reverse-geocode a GPS coordinate pair.

        Results are cached by ~1 km grid cell, so photos taken at the same
        location only hit the API once per process lifetime.

        Nominatim calls are throttled to ≤1 req/1.1 s to stay within OSM
        usage policy and avoid 429 errors.

        Args:
            lat: Latitude  (-90  to  90).
            lon: Longitude (-180 to 180).

        Returns:
            GeoTag with human-readable place name, or None on total failure.
        """
        key = (round(lat, _CACHE_GRID), round(lon, _CACHE_GRID))

        # Return cached result (including negative cache — None means already
        # tried this cell and got nothing; don't hammer the API again).
        with self._cache_lock:
            if key in self._coord_cache:
                cached = self._coord_cache[key]
                logger.debug(
                    "Geo cache hit (%.4f, %.4f) → key=%s label=%s",
                    lat, lon, key, cached.label if cached else None,
                )
                return cached

        # 1. Apple MapKit CLGeocoder (primary — no rate limit)
        result = self._try_mapkit(lat, lon)
        if result is not None:
            result.source = "mapkit"
            logger.debug("MapKit (%.4f, %.4f) → %s", lat, lon, result.label)
        else:
            # 2. Nominatim / OSM (rate-limited fallback)
            result = self._try_nominatim(lat, lon)
            if result is not None:
                result.source = "nominatim"

        # Store outcome (even None so we don't retry this cell).
        with self._cache_lock:
            self._coord_cache[key] = result

        return result

