#!/usr/bin/env python3
"""
Run this LOCALLY before deploying to verify the scraper works.
It will print what properties are found without touching Firestore or sending email.

Usage:
    pip install requests beautifulsoup4 lxml
    python local_test.py
"""

import sys
import os

# ── Set test environment variables ────────────────────────────────────────────
os.environ.setdefault(
    "TARGET_URL",
    "https://rent.placesforpeople.co.uk/properties.aspx"
    "?loc=Bristol&lat=51.454513&lon=-2.58791&mil=10"
    "&max=9999&bed=1&typ=0&overfifty=2&pag=1",
)
# Dummy values so main.py imports without error
os.environ.setdefault("EMAIL_TO",       "test@example.com")
os.environ.setdefault("EMAIL_USER",     "test@example.com")
os.environ.setdefault("EMAIL_PASSWORD", "dummy")

# ── Import scraper only ───────────────────────────────────────────────────────
from main import scrape_all_pages, detect_changes

TARGET = os.environ["TARGET_URL"]

print(f"\nScraping: {TARGET}\n{'='*65}")
try:
    props = scrape_all_pages(TARGET)
except Exception as exc:
    print(f"ERROR: {exc}")
    sys.exit(1)

if not props:
    print("No properties found.")
    print("The site may need JavaScript rendering.")
    print("Open a browser and check the page loads correctly,")
    print("then look at debug_page.html (if created) for clues.")
    sys.exit(1)

mode = next(iter(props.values())).get("mode", "?")
print(f"Mode: {mode} | Found: {len(props)} item(s)\n")

for i, (pid, p) in enumerate(list(props.items())[:10], 1):
    print(f"[{i}] {p.get('title', pid)[:60]}")
    if p.get("url"):      print(f"    URL      : {p['url']}")
    if p.get("price"):    print(f"    Price    : {p['price']}")
    if p.get("bedrooms"): print(f"    Bedrooms : {p['bedrooms']}")
    print(f"    Hash     : {p.get('hash','')[:12]}...")
    print()

if len(props) > 10:
    print(f"... and {len(props)-10} more.")

print(f"{'='*65}")
print("Scraper is working correctly.")
print("You can now run deploy.sh to go live.")
