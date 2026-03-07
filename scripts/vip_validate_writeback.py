"""Quick validation for writeback merge logic."""
import ast, sys, os
os.chdir("/Users/ak./Study/VIP")
sys.path.insert(0, "/Users/ak./Study/VIP")

for f in [
    "backend/writeback/fields.py",
    "backend/writeback/exiftool.py",
    "backend/writeback/engine.py",
]:
    try:
        ast.parse(open(f).read())
        print(f"OK  {f}")
    except SyntaxError as e:
        print(f"ERR {f}: {e}")
        sys.exit(1)

from backend.writeback.fields import VIP_SUBJECT_PREFIXES, build_field_map
from backend.writeback.exiftool import ExifToolWriter
from backend.writeback.engine import _merge_with_existing_xmp, execute_writes, write_single_file
print("Imports OK")
print("VIP_SUBJECT_PREFIXES:", VIP_SUBJECT_PREFIXES)

# Scenario: Alice is in VIP, Bob was added by Lightroom.
# Previous VIP tags had obj:Bicycle + geo:Ocean; new ones are obj:Car + geo:Mountains.
# Holiday2023 is a Lightroom keyword that must survive.
vip = {
    "XMP:PersonInImage": ["Alice"],
    "XMP:Subject":       ["Alice", "obj:Car", "geo:Mountains"],
    "IPTC:Keywords":     ["Alice", "obj:Car", "geo:Mountains"],
}
existing = {
    "XMP:PersonInImage": ["Alice", "Bob"],
    "XMP:Subject":       ["Alice", "obj:Bicycle", "geo:Ocean", "Holiday2023"],
}
merged = _merge_with_existing_xmp(vip, existing)

assert "Bob"         in merged["XMP:PersonInImage"], "Bob (external) must be preserved"
assert "Alice"       in merged["XMP:PersonInImage"], "Alice must be present"
assert "Holiday2023" in merged["XMP:Subject"],       "External keyword must be preserved"
assert "obj:Car"     in merged["XMP:Subject"],       "New VIP obj: keyword must be present"
assert "obj:Bicycle" not in merged["XMP:Subject"],   "Stale VIP obj: keyword must be gone"
assert "geo:Mountains" in merged["XMP:Subject"],     "New VIP geo: keyword must be present"
assert "geo:Ocean"   not in merged["XMP:Subject"],   "Stale VIP geo: keyword must be gone"
assert merged["XMP:Subject"] == merged["IPTC:Keywords"], "Subject and Keywords must be in sync"
print("Merge logic: all assertions passed")
print("  PersonInImage:", merged["XMP:PersonInImage"])
print("  Subject:      ", merged["XMP:Subject"])

# Scenario: no existing file data (first write) — should return VIP fields unchanged
merged2 = _merge_with_existing_xmp(vip, {})
assert merged2["XMP:PersonInImage"] == ["Alice"]
assert merged2["XMP:Subject"] == ["Alice", "obj:Car", "geo:Mountains"]
print("First-write scenario: passed")

# Scenario: VIP has no persons/tags, file has external keywords — preserve them
merged3 = _merge_with_existing_xmp({"XMP:Identifier": "abc"}, existing)
assert "XMP:PersonInImage" in merged3
assert "Bob" in merged3["XMP:PersonInImage"]
assert "Holiday2023" in merged3.get("XMP:Subject", [])
print("Preserve-only scenario: passed")
print("All validation passed.")
