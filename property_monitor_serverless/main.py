"""
Property Monitor – Google Cloud Function
Scrapes a property listing page, diffs against stored state,
and emails gaadaale24@outlook.com when anything changes.

Environment variables (set via gcloud / Secret Manager – never hardcoded):
    TARGET_URL      Full URL to monitor
    EMAIL_TO        Recipient address
    EMAIL_USER      Outlook sender address
    EMAIL_PASSWORD  Outlook App Password
    SMTP_HOST       (optional) default: smtp-mail.outlook.com
    SMTP_PORT       (optional) default: 587
"""

import os
import re
import hashlib
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
import functions_framework
from google.cloud import firestore

# ── Environment ───────────────────────────────────────────────────────────────
TARGET_URL  = os.environ["TARGET_URL"]
EMAIL_TO    = os.environ["EMAIL_TO"]
EMAIL_USER  = os.environ["EMAIL_USER"]
EMAIL_PASS  = os.environ["EMAIL_PASSWORD"]
SMTP_HOST   = os.environ.get("SMTP_HOST", "smtp-mail.outlook.com")
SMTP_PORT   = int(os.environ.get("SMTP_PORT", "587"))

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL = "https://rent.placesforpeople.co.uk"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Ordered by specificity – first selector with real results wins
ITEM_SELECTORS = [
    ".property-item",
    ".property-listing",
    ".search-result-item",
    ".search-result",
    ".property-card",
    ".result-item",
    "[class*='property-item']",
    "[class*='property-listing']",
    ".properties-container > div",
    "#search-results > div",
    "#propertyResults > div",
]

PRICE_RE = re.compile(
    r"£\s*[\d,]+(?:\.\d{2})?(?:\s*(?:per\s+)?(?:month|week|pcm|pw|pm))?", re.I
)
BED_RE = re.compile(r"\d+\s*(?:bed(?:room)?s?)", re.I)
NO_RESULTS = [
    "no properties found", "no results found",
    "0 properties", "sorry, there are no",
]

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ═════════════════════════════════════════════════════════════════════════════
# SCRAPER
# ═════════════════════════════════════════════════════════════════════════════

def scrape_all_pages(url: str) -> Dict[str, dict]:
    """Scrape every paginated page and merge results."""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    all_props: Dict[str, dict] = {}

    for page_num in range(1, 21):
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["pag"] = [str(page_num)]
        paged_url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))

        props = _scrape_page(paged_url)

        if not props:
            break   # no more results

        # Stop pagination if we got a hash-mode result (fallback mode) on page >1
        if page_num > 1 and "__page__" in props:
            break

        all_props.update(props)
        logger.info(f"Page {page_num}: {len(props)} properties (total {len(all_props)})")

    return all_props


def _scrape_page(url: str) -> Dict[str, dict]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"HTTP error fetching {url}: {exc}")

    soup = BeautifulSoup(resp.text, "lxml")

    # Check for "no results" before wasting effort
    page_text_lower = soup.get_text(" ", strip=True).lower()
    if any(phrase in page_text_lower for phrase in NO_RESULTS):
        return {}

    # Remove noise
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    items = _find_property_items(soup)

    if items:
        return _extract_properties(items)

    # ── Fallback: hash the entire cleaned page ───────────────────────────────
    # Any visible change on the page will trigger an alert.
    text = soup.get_text(" ", strip=True)
    page_hash = hashlib.sha256(text.encode()).hexdigest()
    logger.warning("No structured properties found – using full-page hash fallback.")
    return {
        "__page__": {
            "title": "Full page content",
            "url": url,
            "price": "",
            "bedrooms": "",
            "hash": page_hash,
            "mode": "hash",
        }
    }


def _find_property_items(soup) -> list:
    for selector in ITEM_SELECTORS:
        try:
            found = [el for el in soup.select(selector) if len(el.get_text(strip=True)) > 20]
            if found:
                return found
        except Exception:
            continue

    # Structural fallback: divs with a link + price or bedroom text
    candidates = []
    for div in soup.find_all("div"):
        text = div.get_text(" ", strip=True)
        if not (20 < len(text) < 1_200):
            continue
        if not div.find("a", href=True):
            continue
        if PRICE_RE.search(text) or BED_RE.search(text):
            candidates.append(div)

    # Keep only outermost (remove children that are already covered by a parent)
    result = [c for c in candidates if not any(c in p.descendants for p in candidates if p is not c)]
    return result[:50]


def _extract_properties(items: list) -> Dict[str, dict]:
    properties: Dict[str, dict] = {}
    for item in items:
        text = item.get_text(" ", strip=True)
        link = item.find("a", href=True)
        href = (link["href"] if link else "") or ""

        if href.startswith("http"):
            prop_url = href
        elif href.startswith("/"):
            prop_url = BASE_URL + href
        else:
            prop_url = ""

        title = _extract_title(item, link)
        price_m = PRICE_RE.search(text)
        bed_m = BED_RE.search(text)
        pid = hashlib.md5((prop_url or title).encode()).hexdigest()[:14]

        properties[pid] = {
            "title":    title[:180],
            "url":      prop_url,
            "price":    price_m.group(0).strip() if price_m else "",
            "bedrooms": bed_m.group(0).strip()   if bed_m   else "",
            "hash":     hashlib.md5(text.encode()).hexdigest(),
            "mode":     "structured",
        }
    return properties


def _extract_title(item, link) -> str:
    for tag in ["h1", "h2", "h3", "h4"]:
        el = item.find(tag)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True)
    if link and link.get_text(strip=True):
        return link.get_text(" ", strip=True)
    for line in item.get_text("\n", strip=True).splitlines():
        if line.strip():
            return line.strip()[:120]
    return "Unknown property"


# ═════════════════════════════════════════════════════════════════════════════
# CHANGE DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def detect_changes(old: Dict[str, dict], new: Dict[str, dict]) -> List[dict]:
    changes: List[dict] = []
    old_ids, new_ids = set(old), set(new)

    for pid in new_ids - old_ids:
        changes.append({"type": "NEW", "pid": pid, "prop": new[pid]})

    for pid in old_ids - new_ids:
        changes.append({"type": "REMOVED", "pid": pid, "prop": old[pid]})

    for pid in old_ids & new_ids:
        if old[pid].get("hash") != new[pid].get("hash"):
            changes.append({"type": "UPDATED", "pid": pid, "old": old[pid], "new": new[pid]})

    return changes


# ═════════════════════════════════════════════════════════════════════════════
# FIRESTORE
# ═════════════════════════════════════════════════════════════════════════════

def load_state(db: firestore.Client) -> Dict[str, dict]:
    doc = db.collection("monitor").document("state").get()
    return dict(doc.to_dict()) if doc.exists else {}


def save_state(db: firestore.Client, state: Dict[str, dict]):
    # Store only essential fields to stay well within Firestore 1MB doc limit
    slim = {
        pid: {k: v for k, v in prop.items() if k in ("title", "url", "price", "bedrooms", "hash", "mode")}
        for pid, prop in state.items()
    }
    db.collection("monitor").document("state").set(slim)


def save_change_log(db: firestore.Client, changes: List[dict]):
    batch = db.batch()
    col = db.collection("change_log")
    ts = datetime.utcnow().isoformat()
    for ch in changes:
        batch.set(col.document(), {**ch, "timestamp": ts})
    batch.commit()


# ═════════════════════════════════════════════════════════════════════════════
# EMAIL
# ═════════════════════════════════════════════════════════════════════════════

def send_alert_email(changes: List[dict]):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    count = len(changes)
    subject = f"[Property Alert] {count} change{'s' if count != 1 else ''} detected – {ts}"

    lines = [
        "=" * 58,
        "  PROPERTY MONITOR ALERT",
        "=" * 58,
        f"Detected : {ts}",
        f"URL      : {TARGET_URL}",
        f"Changes  : {count}",
        "",
    ]

    for ch in changes:
        prop = ch.get("prop") or ch.get("new", {})
        title = prop.get("title") or ch["pid"]
        ctype = ch["type"]

        if ctype == "NEW":
            lines.append("🏠  NEW PROPERTY LISTED")
        elif ctype == "REMOVED":
            lines.append("❌  PROPERTY REMOVED")
        else:
            lines.append("🔄  PROPERTY UPDATED")

        lines.append(f"    Property : {title}")
        if prop.get("url"):
            lines.append(f"    Link     : {prop['url']}")
        if prop.get("price"):
            lines.append(f"    Price    : {prop['price']}")
        if prop.get("bedrooms"):
            lines.append(f"    Bedrooms : {prop['bedrooms']}")

        # Show field changes for updates
        if ctype == "UPDATED":
            old_p = ch.get("old", {})
            new_p = ch.get("new", {})
            for field in ("price", "bedrooms"):
                ov, nv = old_p.get(field, ""), new_p.get(field, "")
                if ov != nv:
                    lines.append(f"    {field.title()} change: '{ov}' → '{nv}'")

        lines.append("")

    lines.append("─" * 58)
    lines.append("Sent by Property Monitor (Google Cloud Functions)")

    body = "\n".join(lines)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
        srv.ehlo()
        srv.starttls()
        srv.login(EMAIL_USER, EMAIL_PASS)
        srv.sendmail(EMAIL_USER, EMAIL_TO, msg.as_string())

    logger.info(f"Alert email sent to {EMAIL_TO}")


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

@functions_framework.http
def monitor(request):
    """HTTP-triggered Cloud Function – called by Cloud Scheduler every 2 minutes."""
    db = firestore.Client()
    try:
        current = scrape_all_pages(TARGET_URL)

        if not current:
            msg = "No properties found. Page may be temporarily unavailable."
            logger.warning(msg)
            return msg, 200

        previous = load_state(db)

        if not previous:
            save_state(db, current)
            msg = f"Baseline saved: {len(current)} item(s). Monitoring is now ACTIVE."
            logger.info(msg)
            return msg, 200

        changes = detect_changes(previous, current)
        save_state(db, current)

        if not changes:
            logger.info(f"No changes. {len(current)} propert{'y' if len(current)==1 else 'ies'} tracked.")
            return "No changes.", 200

        save_change_log(db, changes)
        send_alert_email(changes)

        msg = f"{len(changes)} change(s) detected and emailed to {EMAIL_TO}."
        logger.info(msg)
        return msg, 200

    except Exception as exc:
        logger.exception("Monitor run failed")
        return f"Error: {exc}", 500
