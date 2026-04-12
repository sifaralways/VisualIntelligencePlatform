#!/usr/bin/env swift
/**
 * vip_geocode — Apple MapKit reverse-geocoding helper.
 *
 * Uses MKReverseGeocodingRequest (macOS 26.0+) with automatic fall-back to
 * CLGeocoder (macOS 10.9–25.x) so the binary works on all supported systems.
 *
 * Usage:  vip_geocode <lat> <lon>
 * Output: JSON object on stdout, non-zero exit on failure.
 *
 * Example:
 *   vip_geocode -33.8568 151.2153
 *   → {"name":"Sydney Opera House","locality":"Sydney","country":"Australia",
 *      "cityWithContext":"Sydney, NSW","fullAddress":"..."}
 *
 * Compilation (done automatically by GeoResolver.load()):
 *   swiftc vip_geocode.swift -framework MapKit -framework CoreLocation -o vip_geocode
 *
 * No Apple Developer account or API key required.
 */

import CoreLocation
import Foundation
import MapKit

// ── Argument validation ──────────────────────────────────────────────────────

guard CommandLine.arguments.count == 3,
      let lat = Double(CommandLine.arguments[1]),
      let lon = Double(CommandLine.arguments[2]) else {
    fputs("Usage: vip_geocode <lat> <lon>\n", stderr)
    exit(1)
}

// ── Output container & completion flag ───────────────────────────────────────

var output: [String: Any] = [:]

// ── Reverse geocode — MKReverseGeocodingRequest (macOS 26+) ─────────────────

let clLocation = CLLocation(latitude: lat, longitude: lon)

if #available(macOS 26.0, *) {
    guard let request = MKReverseGeocodingRequest(location: clLocation) else {
        fputs("MKReverseGeocodingRequest init failed\n", stderr)
        exit(1)
    }
    request.preferredLocale = Locale(identifier: "en_US")

    request.getMapItems { mapItems, error in
        defer { CFRunLoopStop(CFRunLoopGetMain()) }
        guard let item = mapItems?.first, error == nil else {
            if let e = error { fputs("MapKit error: \(e.localizedDescription)\n", stderr) }
            return
        }

        var result: [String: Any] = [:]

        // POI / venue name (e.g. "Sydney Opera House", "Bondi Beach")
        if let v = item.name { result["name"] = v }

        // Structured address fields (macOS 26.0+)
        if let repr = item.addressRepresentations {
            if let v = repr.cityName        { result["locality"] = v }        // "Sydney"
            if let v = repr.cityWithContext { result["cityWithContext"] = v } // "Sydney, NSW"
            if let v = repr.regionName      { result["country"] = v }        // "Australia"
        }

        // Full formatted address (multi-line → join to single line)
        if let addr = item.address {
            result["fullAddress"] = addr.fullAddress
                .components(separatedBy: "\n")
                .joined(separator: ", ")
            if let v = addr.shortAddress { result["shortAddress"] = v }
        }

        // Point-of-interest category (e.g. "landmark", "restaurant")
        if let poi = item.pointOfInterestCategory {
            result["pointOfInterestCategory"] = poi.rawValue
        }

        output = result
    }

} else {
    // ── Fallback: CLGeocoder (macOS 10.9 – 25.x) ────────────────────────────
    let geocoder = CLGeocoder()
    geocoder.reverseGeocodeLocation(
        clLocation,
        preferredLocale: Locale(identifier: "en_US")
    ) { placemarks, error in
        defer { CFRunLoopStop(CFRunLoopGetMain()) }
        guard let placemark = placemarks?.first, error == nil else {
            if let e = error { fputs("CLGeocoder error: \(e.localizedDescription)\n", stderr) }
            return
        }

        var result: [String: Any] = [:]
        if let v = placemark.name                   { result["name"] = v }
        if let v = placemark.locality               { result["locality"] = v }
        if let v = placemark.subLocality            { result["subLocality"] = v }
        if let v = placemark.administrativeArea     { result["administrativeArea"] = v }
        if let v = placemark.subAdministrativeArea  { result["subAdministrativeArea"] = v }
        if let v = placemark.country                { result["country"] = v }
        if let v = placemark.isoCountryCode         { result["countryCode"] = v }
        if let v = placemark.ocean                  { result["ocean"] = v }
        if let v = placemark.inlandWater            { result["inlandWater"] = v }
        if let aoi = placemark.areasOfInterest, !aoi.isEmpty {
            result["areasOfInterest"] = aoi
        }
        output = result
    }
}

// ── Run the main run loop until the completion handler fires ─────────────────
// CLGeocoder and MKReverseGeocodingRequest both deliver their callback on the
// main queue.  CFRunLoopRun() keeps the process alive while that queue is
// processed; the callback calls CFRunLoopStop() when done.
CFRunLoopRun()

// ── Emit JSON result ─────────────────────────────────────────────────────────
guard !output.isEmpty,
      let data = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]),
      let str = String(data: data, encoding: .utf8) else {
    exit(1)
}
print(str)

