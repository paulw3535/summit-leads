"""
Summit County, Ohio – Motivated Seller Lead Scraper
Targets: summitcountyoh-web.tylerhost.net (Tyler Technologies Eagle Web)
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RECORDER_BASE   = "https://summitcountyoh-web.tylerhost.net/web/"
RECORDER_SEARCH = "https://summitcountyoh-web.tylerhost.net/web/docSearchPOST"
RECORDER_DOC    = "https://summitcountyoh-web.tylerhost.net/web/document/"

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

# Document types we want — mapped to our categories
# These match the exact names shown in the Tyler Web left-panel filter
TARGET_DOC_TYPES: dict[str, tuple[str, str]] = {
    "LIS PENDENS":               ("foreclosure",  "Lis Pendens"),
    "NOTICE OF FORECLOSURE":     ("foreclosure",  "Notice of Foreclosure"),
    "SHERIFFS DEED":             ("foreclosure",  "Sheriffs Deed"),
    "FEDERAL TAX LIEN":          ("lien",         "Federal Tax Lien"),
    "FEDERAL LIEN":              ("lien",         "Federal Lien"),
    "STATE OF OH LIEN":          ("lien",         "State of OH Lien"),
    "ASSESSMENT LIEN":           ("lien",         "Assessment Lien"),
    "CHILD SUPPORT LIEN":        ("lien",         "Child Support Lien"),
    "LIEN":                      ("lien",         "Lien"),
    "MECHANICS LIEN":            ("lien",         "Mechanic Lien"),
    "JUDGMENT LIEN":             ("judgment",     "Judgment Lien"),
    "CERTIFICATE OF JUDGMENT":   ("judgment",     "Certificate of Judgment"),
    "TRANSFER ON DEATH":         ("probate",      "Transfer on Death"),
    "NOTICE OF COMMENCEMENT":    ("noc",          "Notice of Commencement"),
    "LIS PENDENS RELEASE":       ("release",      "Lis Pendens Release"),
    "RELEASE OF LIS PENDENS":    ("release",      "Release of Lis Pendens"),
}

REPO_ROOT      = Path(__file__).resolve().parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON      = REPO_ROOT / "data"      / "records.json"
GHL_CSV        = REPO_ROOT / "data"      / "ghl_export.csv"

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def retry_call(fn, *args, attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY, **kwargs):
    last: Exception = RuntimeError("unknown")
    for i in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last = exc
            log.warning("Attempt %d/%d failed: %s", i, attempts, exc)
            if i < attempts:
                time.sleep(delay)
    raise last


def safe_float(v: Any) -> Optional[float]:
    try:
        return float(re.sub(r"[^\d.]", "", str(v))) if v else None
    except ValueError:
        return None


def parse_date(raw: str) -> Optional[str]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(raw.strip()[:10], fmt[:8]).strftime("%Y-%m-%d")
        except Exception:
            pass
    # Handle "05/01/2026 07:51 AM" style
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y %I:%M %p").strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        return datetime.strptime(raw.strip()[:10], "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        pass
    return raw.strip()[:10] if raw else None


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).upper().strip())


def name_variants(full: str) -> list[str]:
    n = normalize(full)
    variants = {n}
    if "," in n:
        parts = [p.strip() for p in n.split(",", 1)]
        last, rest = parts[0], parts[1]
        variants.add(f"{rest} {last}")
        first = rest.split()[0] if rest.split() else rest
        variants.add(f"{first} {last}")
        variants.add(f"{last} {first}")
    else:
        tokens = n.split()
        if len(tokens) >= 2:
            first, last = tokens[0], tokens[-1]
            variants.add(f"{last}, {' '.join(tokens[1:])}")
            variants.add(f"{last} {first}")
    return list(variants)


# ---------------------------------------------------------------------------
# 1. Recorder scraper – Tyler Eagle Web
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # Hit the home page first to get any session cookies
    try:
        s.get(RECORDER_BASE, timeout=30)
    except Exception as exc:
        log.debug("Session init warning: %s", exc)
    return s


def search_recorder(
    session: requests.Session,
    date_from: str,
    date_to: str,
    doc_type: str,
) -> list[dict]:
    """
    POST a search to the Tyler Eagle Web recorder and return parsed records.
    date_from / date_to format: MM/DD/YYYY
    """
    records: list[dict] = []
    page = 1

    while True:
        payload = {
            "RecordingDateFrom": date_from,
            "RecordingDateTo":   date_to,
            "DocTypeID":         doc_type,
            "PageNum":           str(page),
            "RecordsPerPage":    "100",
            "SearchType":        "Date",
            "IndexName":         "_default_",
        }

        try:
            resp = session.post(
                RECORDER_SEARCH,
                data=payload,
                timeout=60,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Search POST failed (page %d, %s): %s", page, doc_type, exc)
            break

        page_records, has_next = parse_tyler_results(resp.text, doc_type)
        records.extend(page_records)
        log.debug("  %s page %d → %d records", doc_type, page, len(page_records))

        if not has_next or not page_records:
            break
        page += 1
        time.sleep(1)

    return records


def parse_tyler_results(html: str, doc_type_filter: str) -> tuple[list[dict], bool]:
    """Parse Tyler Eagle Web search results HTML."""
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    # Tyler renders each result as a card/row div or table row
    # Look for document entries — they typically have doc number + type
    entries = (
        soup.select("div.document-row, div.result-row, tr.docrow, div.card") or
        soup.select("div[class*='document'], div[class*='result'], div[class*='record']")
    )

    if not entries:
        # Fall back: look for any structured table
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [th.get_text(" ", strip=True).lower()
                       for th in rows[0].find_all(["th", "td"])]
            if not any(h in headers for h in
                       ["doc", "grantor", "recording", "instrument", "type"]):
                continue
            for row in rows[1:]:
                rec = _parse_table_row(row, headers, doc_type_filter)
                if rec:
                    records.append(rec)

    else:
        for entry in entries:
            rec = _parse_card_entry(entry, doc_type_filter)
            if rec:
                records.append(rec)

    # Check for next page
    has_next = bool(
        soup.find("a", string=re.compile(r"Next|>", re.I)) or
        soup.find("input", {"value": re.compile(r"Next", re.I)}) or
        soup.select_one("a.next, li.next > a, .pagination .next")
    )

    return records, has_next


def _parse_card_entry(entry, doc_type_filter: str) -> Optional[dict]:
    """Parse a Tyler card-style result entry."""
    try:
        text = entry.get_text(" ", strip=True)
        if not text:
            return None

        # Extract doc number (usually first number in the entry)
        doc_num_match = re.search(r"\b(\d{7,10})\b", text)
        doc_num = doc_num_match.group(1) if doc_num_match else ""

        # Extract document type from heading or bold text
        heading = entry.find(["h3", "h4", "strong", "b", "span"])
        doc_type_raw = heading.get_text(strip=True) if heading else doc_type_filter
        # Strip doc number from type
        doc_type_raw = re.sub(r"^\d+\s*[•·\-]\s*", "", doc_type_raw).strip()

        # Extract date
        date_match = re.search(r"\d{1,2}/\d{1,2}/\d{4}", text)
        filed = parse_date(date_match.group(0)) if date_match else ""

        # Extract grantor/grantee
        grantor, grantee = _extract_names(entry, text)

        # Extract legal description / parcel
        legal = ""
        legal_match = re.search(r"Parcel[:\s]+(\S+)", text, re.I)
        if legal_match:
            legal = "Parcel: " + legal_match.group(1)

        # Extract amount
        amount_match = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
        amount = safe_float(amount_match.group(0)) if amount_match else None

        # Build direct URL
        clerk_url = RECORDER_BASE
        link = entry.find("a", href=True)
        if link:
            clerk_url = urljoin(RECORDER_BASE, link["href"])
        elif doc_num:
            clerk_url = RECORDER_DOC + doc_num

        cat, cat_label = _categorize(doc_type_raw or doc_type_filter)

        if not doc_num and not grantor:
            return None

        return _build_record(
            doc_num, doc_type_raw or doc_type_filter,
            filed, cat, cat_label, grantor, grantee,
            amount, legal, clerk_url
        )
    except Exception as exc:
        log.debug("Card parse error: %s", exc)
        return None


def _parse_table_row(row, headers: list[str], doc_type_filter: str) -> Optional[dict]:
    """Parse a table row from Tyler results."""
    try:
        cells = row.find_all(["td", "th"])
        if not cells:
            return None

        def ci(*candidates):
            for c in candidates:
                for i, h in enumerate(headers):
                    if c in h:
                        return i
            return -1

        def cell(i):
            if i < 0 or i >= len(cells):
                return ""
            return cells[i].get_text(" ", strip=True)

        i_doc     = ci("doc", "instrument", "number", "reception")
        i_type    = ci("type", "description", "doc type")
        i_date    = ci("recording", "filed", "date")
        i_grantor = ci("grantor", "owner", "from", "seller")
        i_grantee = ci("grantee", "to", "buyer")
        i_legal   = ci("legal", "parcel", "description")
        i_amount  = ci("amount", "consideration")

        doc_num  = cell(i_doc) or cell(0)
        if not doc_num or not re.search(r"\d", doc_num):
            return None

        doc_type_raw = cell(i_type) or doc_type_filter
        filed        = parse_date(cell(i_date))
        grantor      = normalize(cell(i_grantor))
        grantee      = normalize(cell(i_grantee))
        legal        = cell(i_legal)
        amount       = safe_float(cell(i_amount))

        clerk_url = RECORDER_BASE
        link_cell = cells[max(i_doc, 0)]
        anchor = link_cell.find("a", href=True)
        if anchor:
            clerk_url = urljoin(RECORDER_BASE, anchor["href"])

        cat, cat_label = _categorize(doc_type_raw)

        return _build_record(
            doc_num, doc_type_raw, filed, cat, cat_label,
            grantor, grantee, amount, legal, clerk_url
        )
    except Exception as exc:
        log.debug("Table row parse error: %s", exc)
        return None


def _extract_names(entry, text: str) -> tuple[str, str]:
    """Try to pull Grantor/Grantee from a card entry."""
    grantor = grantee = ""

    # Look for labeled spans/divs
    for elem in entry.find_all(["span", "div", "td"]):
        label = elem.get_text(strip=True).lower()
        if "grantor" in label:
            nxt = elem.find_next_sibling()
            if nxt:
                grantor = normalize(nxt.get_text(strip=True))
        elif "grantee" in label:
            nxt = elem.find_next_sibling()
            if nxt:
                grantee = normalize(nxt.get_text(strip=True))

    # Regex fallback
    if not grantor:
        m = re.search(r"Grantor[:\s(]+([A-Z][A-Z\s,\.]+?)(?:\s{2,}|Grantee|Legal|\n)", text, re.I)
        if m:
            grantor = normalize(m.group(1))
    if not grantee:
        m = re.search(r"Grantee[:\s(]+([A-Z][A-Z\s,\.]+?)(?:\s{2,}|Legal|Recording|\n)", text, re.I)
        if m:
            grantee = normalize(m.group(1))

    return grantor, grantee


def _categorize(doc_type: str) -> tuple[str, str]:
    upper = doc_type.upper().strip()
    for key, (cat, label) in TARGET_DOC_TYPES.items():
        if key in upper:
            return cat, label
    # Fallback guesses
    if "LIEN" in upper:
        return "lien", doc_type.title()
    if "PENDENS" in upper or "FORECLOS" in upper or "SHERIFF" in upper:
        return "foreclosure", doc_type.title()
    if "JUDGMENT" in upper:
        return "judgment", doc_type.title()
    if "PROBATE" in upper or "DEATH" in upper or "ESTATE" in upper:
        return "probate", doc_type.title()
    return "other", doc_type.title()


def _build_record(
    doc_num, doc_type, filed, cat, cat_label,
    grantor, grantee, amount, legal, clerk_url
) -> dict:
    return {
        "doc_num":      str(doc_num).strip(),
        "doc_type":     normalize(doc_type),
        "filed":        filed or "",
        "cat":          cat,
        "cat_label":    cat_label,
        "owner":        normalize(grantor),
        "grantee":      normalize(grantee),
        "amount":       amount,
        "legal":        legal,
        "clerk_url":    clerk_url,
        "prop_address": "",
        "prop_city":    "Summit County",
        "prop_state":   "OH",
        "prop_zip":     "",
        "mail_address": "",
        "mail_city":    "",
        "mail_state":   "",
        "mail_zip":     "",
    }


# ---------------------------------------------------------------------------
# 2. Main recorder fetch — search by date range, filter by type on left panel
# ---------------------------------------------------------------------------

def fetch_all_records(date_from: str, date_to: str) -> list[dict]:
    """
    Fetch all motivated-seller doc types from the Tyler recorder.
    Strategy: do ONE broad date-range search, then filter by doc type
    using the left-panel category counts (more reliable than per-type POSTs).
    """
    session = make_session()
    all_records: list[dict] = []

    # First: broad search with just the date range
    log.info("Fetching broad date range %s → %s", date_from, date_to)

    broad_payload = {
        "RecordingDateFrom": date_from,
        "RecordingDateTo":   date_to,
        "SearchType":        "Date",
        "IndexName":         "_default_",
        "RecordsPerPage":    "100",
        "PageNum":           "1",
    }

    try:
        resp = retry_call(session.post, RECORDER_SEARCH, data=broad_payload, timeout=60)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Extract left-panel doc type links/counts
        # Tyler renders these as <a> tags or filter buttons with doc type names
        type_links = _extract_type_links(soup, resp.url)
        log.info("Found %d document type filters on left panel", len(type_links))

        if type_links:
            # Click each relevant doc type filter
            for type_name, type_url in type_links.items():
                upper = type_name.upper()
                if not any(key in upper for key in TARGET_DOC_TYPES):
                    continue
                log.info("Fetching type: %s", type_name)
                try:
                    type_records = _paginate_tyler(session, type_url, type_name)
                    all_records.extend(type_records)
                    log.info("  → %d records", len(type_records))
                except Exception as exc:
                    log.warning("Type '%s' failed: %s", type_name, exc)
                time.sleep(1.5)
        else:
            # No left panel found — parse the broad results and filter by type
            log.info("No type filters found, parsing all results and filtering")
            page_records, _ = parse_tyler_results(resp.text, "")
            for rec in page_records:
                if any(key in rec["doc_type"].upper() for key in TARGET_DOC_TYPES):
                    all_records.append(rec)

            # Also try per-type POST searches
            for doc_type_name in TARGET_DOC_TYPES:
                try:
                    recs = search_recorder(session, date_from, date_to, doc_type_name)
                    all_records.extend(recs)
                    log.info("Type search '%s' → %d records", doc_type_name, len(recs))
                except Exception as exc:
                    log.warning("Type search '%s' failed: %s", doc_type_name, exc)
                time.sleep(1)

    except Exception as exc:
        log.error("Broad search failed: %s", exc)

    return all_records


def _extract_type_links(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    """Extract document type filter links from the left panel."""
    links: dict[str, str] = {}

    # Tyler typically renders left panel as <ul> or <div> with filter links
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).upper()
        href = a["href"]
        # Filter links usually contain DocType or similar in the URL
        if any(key in text for key in TARGET_DOC_TYPES):
            full_url = urljoin(base_url, href)
            links[text] = full_url

    # Also check buttons with onclick or data attributes
    for btn in soup.find_all(["button", "input"], attrs={"onclick": True}):
        text = btn.get_text(strip=True).upper()
        onclick = btn.get("onclick", "")
        if any(key in text for key in TARGET_DOC_TYPES):
            url_match = re.search(r"'([^']+)'", onclick)
            if url_match:
                links[text] = urljoin(base_url, url_match.group(1))

    return links


def _paginate_tyler(session: requests.Session, start_url: str, doc_type: str) -> list[dict]:
    """Follow pagination on a Tyler results page."""
    records: list[dict] = []
    url = start_url

    for _ in range(50):  # max 50 pages per type
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            log.warning("Pagination GET failed: %s", exc)
            break

        page_records, has_next = parse_tyler_results(resp.text, doc_type)
        records.extend(page_records)

        if not has_next:
            break

        soup = BeautifulSoup(resp.text, "lxml")
        next_link = (
            soup.find("a", string=re.compile(r"Next|>", re.I)) or
            soup.select_one("a.next, li.next > a, .pagination .next")
        )
        if not next_link or not next_link.get("href"):
            break
        url = urljoin(resp.url, next_link["href"])
        time.sleep(1)

    return records


# ---------------------------------------------------------------------------
# 3. Parcel lookup (Summit County Fiscal Office property search)
# ---------------------------------------------------------------------------

def build_parcel_lookup_from_web(session: requests.Session) -> dict[str, dict]:
    """
    The Tyler system itself shows parcel numbers in results.
    We use the Summit County property search to look up addresses by parcel.
    This is a best-effort enrichment.
    """
    log.info("Parcel lookup will be done per-record from Tyler legal descriptions")
    return {}


def enrich_from_legal(record: dict) -> dict:
    """Extract parcel number from legal description and look up address."""
    legal = record.get("legal", "")
    parcel_match = re.search(r"Parcel[:\s]+(\d[\d-]+)", legal, re.I)
    if parcel_match:
        record["parcel"] = parcel_match.group(1)
    return record


# ---------------------------------------------------------------------------
# 4. Scoring
# ---------------------------------------------------------------------------

def score_record(rec: dict, all_records: list[dict]) -> tuple[int, list[str]]:
    flags: list[str] = []
    score = 30

    cat    = rec.get("cat", "")
    dtype  = rec.get("doc_type", "")
    owner  = rec.get("owner", "")
    amount = rec.get("amount")
    filed  = rec.get("filed", "")

    if cat == "foreclosure":
        flags.append("Lis pendens" if "PENDENS" in dtype else "Pre-foreclosure")
        if "SHERIFF" in dtype:
            flags.append("Sheriff sale")
        score += 10

    if cat == "judgment":
        flags.append("Judgment lien")
        score += 10

    if cat == "lien":
        if any(x in dtype for x in ("FEDERAL", "STATE", "TAX", "ASSESSMENT")):
            flags.append("Tax lien")
        elif "MECHANIC" in dtype:
            flags.append("Mechanic lien")
        elif "CHILD" in dtype:
            flags.append("Child support lien")
        else:
            flags.append("Judgment lien")
        score += 10

    if cat == "probate":
        flags.append("Probate / estate")
        score += 10

    # LP + foreclosure combo bonus
    owner_docs = [r for r in all_records if r.get("owner") == owner and r is not rec]
    has_lp = any("PENDENS" in r.get("doc_type", "") for r in owner_docs) or "PENDENS" in dtype
    has_fc = any(r.get("cat") == "foreclosure" for r in owner_docs)
    if has_lp and has_fc:
        score += 20

    if amount:
        if amount > 100_000:
            flags.append("High debt (>$100k)")
            score += 15
        elif amount > 50_000:
            score += 10

    if owner and re.search(r"\b(LLC|INC|CORP|LTD|TRUST|ESTATE)\b", owner):
        flags.append("LLC / corp owner")
        score += 10

    try:
        if (datetime.now() - datetime.strptime(filed, "%Y-%m-%d")).days <= 7:
            flags.append("New this week")
            score += 5
    except Exception:
        pass

    if rec.get("prop_address"):
        score += 5

    return min(score, 100), flags


# ---------------------------------------------------------------------------
# 5. Output writers
# ---------------------------------------------------------------------------

GHL_FIELDS = [
    "First Name", "Last Name",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Property Address", "Property City", "Property State", "Property Zip",
    "Lead Type", "Document Type", "Date Filed", "Document Number",
    "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
    "Source", "Public Records URL",
]


def _split_name(full: str) -> tuple[str, str]:
    full = full.strip()
    if "," in full:
        parts = [p.strip() for p in full.split(",", 1)]
        first = parts[1].split()[0].title() if parts[1].split() else ""
        return first, parts[0].title()
    tokens = full.split()
    if len(tokens) == 1:
        return "", tokens[0].title()
    return tokens[0].title(), " ".join(tokens[1:]).title()


def write_outputs(records, fetched_at, start_date, end_date):
    payload = {
        "fetched_at":   fetched_at,
        "source":       "Summit County Fiscal Office – Recording Division",
        "date_range":   {
            "from": start_date.strftime("%Y-%m-%d"),
            "to":   end_date.strftime("%Y-%m-%d"),
        },
        "total":        len(records),
        "with_address": sum(1 for r in records if r.get("prop_address")),
        "records":      records,
    }

    for path in (DASHBOARD_JSON, DATA_JSON):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log.info("Wrote %s", path)

    GHL_CSV.parent.mkdir(parents=True, exist_ok=True)
    with GHL_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GHL_FIELDS)
        writer.writeheader()
        for rec in records:
            first, last = _split_name(rec.get("owner", ""))
            writer.writerow({
                "First Name":             first,
                "Last Name":              last,
                "Mailing Address":        rec.get("mail_address", ""),
                "Mailing City":           rec.get("mail_city", ""),
                "Mailing State":          rec.get("mail_state", ""),
                "Mailing Zip":            rec.get("mail_zip", ""),
                "Property Address":       rec.get("prop_address", ""),
                "Property City":          rec.get("prop_city", ""),
                "Property State":         rec.get("prop_state", "OH"),
                "Property Zip":           rec.get("prop_zip", ""),
                "Lead Type":              rec.get("cat_label", ""),
                "Document Type":          rec.get("doc_type", ""),
                "Date Filed":             rec.get("filed", ""),
                "Document Number":        rec.get("doc_num", ""),
                "Amount/Debt Owed":       rec.get("amount", ""),
                "Seller Score":           rec.get("score", 0),
                "Motivated Seller Flags": "; ".join(rec.get("flags", [])),
                "Source":                 "Summit County Fiscal Office – Recording Division",
                "Public Records URL":     rec.get("clerk_url", ""),
            })
    log.info("Wrote GHL CSV: %s", GHL_CSV)


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------

def main():
    end_date   = datetime.now(timezone.utc).replace(tzinfo=None)
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    fetched_at = datetime.now(timezone.utc).isoformat()

    date_from = start_date.strftime("%m/%d/%Y")
    date_to   = end_date.strftime("%m/%d/%Y")

    log.info("Summit County Lead Scraper | %s → %s", date_from, date_to)

    # 1. Fetch records
    raw = fetch_all_records(date_from, date_to)
    log.info("Total raw records fetched: %d", len(raw))

    # 2. De-duplicate
    seen: set[str] = set()
    unique: list[dict] = []
    for rec in raw:
        key = rec.get("doc_num", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(rec)
        elif not key:
            unique.append(rec)

    # 3. Enrich legal descriptions
    for rec in unique:
        enrich_from_legal(rec)

    # 4. Score
    enriched: list[dict] = []
    for rec in unique:
        try:
            score, flags = score_record(rec, unique)
            rec["score"] = score
            rec["flags"] = flags
            enriched.append(rec)
        except Exception as exc:
            log.warning("Score error (skipped): %s", exc)

    enriched.sort(key=lambda r: r.get("score", 0), reverse=True)

    # 5. Write
    write_outputs(enriched, fetched_at, start_date, end_date)

    log.info(
        "Done. %d leads | top score: %s",
        len(enriched),
        enriched[0].get("score", "n/a") if enriched else "n/a",
    )


if __name__ == "__main__":
    main()
