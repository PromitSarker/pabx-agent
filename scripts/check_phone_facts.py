"""Proves the condensed phone facts add nothing that is not in the official page text.
Every number must appear in knowledge/upay_dps.md, and the key numbers of the source must all appear in the phone facts."""
import re
import sys

root = sys.argv[1] if len(sys.argv) > 1 else "."
src = open(f"{root}/knowledge/upay_dps.md", encoding="utf-8").read()
phone = open(f"{root}/knowledge/upay_dps_phone.md", encoding="utf-8").read()
nums = lambda t: set(re.findall(r"\d[\d,]*%?", t.replace("’", "'")))
extra = {n for n in nums(phone) - nums(src) if n.strip(",")}
missing = {n for n in nums(src) - nums(phone) if n.strip(",")} - {"1", "2", "3"} | ({"3"} if "3 months" not in phone else set())
print("chars: source", len(src), "| phone", len(phone), f"| saved {100 - 100 * len(phone) // len(src)}%")
print("numbers in phone facts but NOT in the source (must be empty):", sorted(extra))
print("source numbers missing from phone facts:", sorted(missing))
for phrase in ("3 months", "2 days", "1 day", "10%", "15%", "12-month slab", "24-month slab", "36-month slab", "e-TIN", "NBR"):
    assert phrase in phone, f"phone facts lost: {phrase}"
sys.exit(1 if extra else 0)
