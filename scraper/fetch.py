"""
Summit County, Ohio – Motivated Seller Lead Scraper
Uses Playwright to browse summitcountyoh-web.tylerhost.net like a real user.
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

from bs4 import BeautifulSoup

RECORDER_BASE = "https://summitcountyoh-web.tylerhost.net/web/"

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

TARGET_DOC_TYPES = {
    "LIS PENDENS":             ("foreclosure", "Lis Pendens"),
    "NOTICE OF FORECLOSURE":   ("foreclosure", "Notice of Foreclosure"),
    "SHERIFFS DEED":           ("foreclosure", "Sheriffs Deed"),
    "FEDERAL TAX LIEN":        ("lien",        "Federal Tax Lien"),
    "FEDERAL LIEN":            ("lien",        "Federal Lien"),
    "STATE OF OH LIEN":        ("lien",        "State of OH Lien"),
    "ASSESSMENT LIEN":         ("lien",        "Assessment Lien"),
    "CHILD SUPPORT LIEN":      ("lien",        "Child Support Lien"),
    "LIEN":                    ("lien",        "Lien"),
    "MECHANICS LIEN":          ("lien",        "Mechanic Lien"),
    "JUDGMENT LIEN":           ("judgment",    "Judgment Lien"),
    "CERTIFICATE OF JUDGMENT": ("judgment",    "Certificate of Judgment"),
    "TRANSFER ON DEATH":       ("probate",     "Transfer on Death"),
    "NOTICE OF COMMENCEMENT":  ("noc",         "Notice of Commencement"),
    "LIS PENDENS RELEASE":     ("release",     "Lis Pendens Release"),
    "RELEASE OF LIS PENDENS":  ("release",     "Release of Lis Pendens"),
    "DELINQUENT TAX LIEN":     ("lien",        "Delinquent Tax Lien"),
}

REPO_ROOT      = Path(__file__).resolve().parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON      = REPO_ROOT / "data"      / "records.json"
GHL_CSV        = REPO_ROOT / "data"      / "ghl_export.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_float(v: Any) -> Optional[float]:
    try:
        cleaned = re.sub(r"[^\d.]", "", str(v))
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_date(raw: str) -> str:
    if not raw:
        return ""
    # Handle "05/01/2026 07:51 AM"
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    # Try just first 10 chars
    try:
        return datetime.strptime(raw.strip()[:10], "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        pass
    return raw.strip()[:10]


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


def categorize(doc_type: str) -> tuple[str, str]:
    upper = doc_type.upper().strip()
    for key, (cat, label) in TARGET_DOC_TYPES.items():
        if key in upper:
            return cat, label
    if "LIEN" in upper:
        return "lien", doc_type.title()
    if "PENDENS" in upper or "FORECLOS" in upper or "SHERIFF" in upper:
        return "foreclosure", doc_type.title()
    if "JUDGMENT" in upper or "CERTIFICATE OF J" in upper:
        return "judgment", doc_type.title()
    if "DEATH" in upper or "PROBATE" in upper or "ESTATE" in upper:
        return "probate", doc_type.title()
    return "other", doc_type.title()


# ---------------------------------------------------------------------------
# Playwright scraper
# ---------------------------------------------------------------------------

async def scrape(date_from: str, date_to: str) -> list[dict]:
    from playwright.async_api import async_playwright

    records: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        log.info("Loading recorder search page …")
        await page.goto(RECORDER_BASE, timeout=60_000, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Fill Recording Date Start
        log.info("Filling date range %s → %s", date_from, date_to)
        for sel in [
            "input[id*='RecordingDateFrom']",
            "input[name*='RecordingDateFrom']",
            "input[placeholder*='Start']",
            "input[placeholder*='From']",
            "input[type='date']:first-of-type",
        ]:
            try:
                await page.fill(sel, date_from, timeout=3000)
                log.debug("Filled start date via: %s", sel)
                break
            except Exception:
                pass

        # Fill Recording Date End
        for sel in [
            "input[id*='RecordingDateTo']",
            "input[name*='RecordingDateTo']",
            "input[placeholder*='End']",
            "input[placeholder*='To']",
        ]:
            try:
                await page.fill(sel, date_to, timeout=3000)
                log.debug("Filled end date via: %s", sel)
                break
            except Exception:
                pass

        # Click Search
        searched = False
        for sel in [
            "button:has-text('Search')",
            "input[value='Search']",
            "button[type='submit']",
            "input[type='submit']",
            "a:has-text('Search')",
        ]:
            try:
                await page.click(sel, timeout=5000)
                await page.wait_for_load_state("networkidle", timeout=30000)
                searched = True
                log.info("Search submitted")
                break
            except Exception:
                pass

        if not searched:
            log.error("Could not submit search form")
            await browser.close()
            return records

        await page.wait_for_timeout(3000)

        # Read left panel doc type filters and click each target type
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")

        # Find left panel filter links
        left_panel_links = await _get_left_panel_links(page, soup)
        log.info("Left panel doc types found: %d", len(left_panel_links))

        if left_panel_links:
            for type_name, href in left_panel_links.items():
                upper = type_name.upper()
                is_target = any(key in upper for key in TARGET_DOC_TYPES)
                if not is_target:
                    # Also check reverse — target key contains type name
                    is_target = any(upper in key for key in TARGET_DOC_TYPES)
                if not is_target:
                    continue

                log.info("Collecting type: %s", type_name)
                try:
                    type_records = await _collect_type(
                        page, context, href, type_name
                    )
                    records.extend(type_records)
                    log.info("  → %d records for %s", len(type_records), type_name)
                except Exception as exc:
                    log.warning("Failed collecting %s: %s", type_name, exc)
                await asyncio.sleep(2)
        else:
            # No left panel — parse all results and keep target types
            log.info("No left panel found, parsing all results")
            all_recs = await _collect_all_pages(page)
            for rec in all_recs:
                cat, _ = categorize(rec.get("doc_type", ""))
                if cat != "other":
                    records.append(rec)

        await browser.close()

    log.info("Scrape complete: %d total records", len(records))
    return records


async def _get_left_panel_links(page, soup: BeautifulSoup) -> dict[str, str]:
    """Extract document type filter links from the left panel."""
    links: dict[str, str] = {}
    base_url = page.url

    # Tyler renders left panel as list of clickable filter labels
    # Each has the doc type name and a count badge
    for elem in soup.select("ul li a, div.filter a, aside a, .facet a, .left-panel a"):
        text = elem.get_text(strip=True)
        # Strip count numbers like "ASSESSMENT LIEN 21"
        clean = re.sub(r"\s*\d+\s*$", "", text).strip().upper()
        href = elem.get("href", "")
        if clean and href:
            links[clean] = urljoin(base_url, href)

    # Also check buttons/spans that might be clickable filters
    if not links:
        # Try to find any element with target doc type text that's clickable
        for key in TARGET_DOC_TYPES:
            try:
                elem = page.locator(f"text={key.title()}").first
                is_visible = await elem.is_visible(timeout=1000)
                if is_visible:
                    href = await elem.get_attribute("href") or ""
                    links[key] = urljoin(page.url, href) if href else key
            except Exception:
                pass

    return links


async def _collect_type(page, context, href_or_key: str, type_name: str) -> list[dict]:
    """Navigate to a doc type filter and collect all paginated results."""
    records: list[dict] = []

    if href_or_key.startswith("http"):
        await page.goto(href_or_key, timeout=30000, wait_until="networkidle")
    else:
        # Click the element with this text
        try:
            await page.click(f"text={type_name.title()}", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            try:
                await page.click(f"text={type_name}", timeout=5000)
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception as exc:
                log.warning("Could not navigate to type %s: %s", type_name, exc)
                return records

    await page.wait_for_timeout(2000)
    records = await _collect_all_pages(page, doc_type_hint=type_name)
    return records


async def _collect_all_pages(page, doc_type_hint: str = "") -> list[dict]:
    """Read all pages of results from the current Tyler results view."""
    records: list[dict] = []
    page_num = 0

    while True:
        page_num += 1
        html = await page.content()
        page_records = parse_tyler_html(html, doc_type_hint)
        records.extend(page_records)
        log.debug("  Page %d: %d records", page_num, len(page_records))

        # Check for Next page
        next_btn = page.locator(
            "a:has-text('Next'), button:has-text('Next'), "
            "a:has-text('>'), .next > a, li.next > a, "
            "a[title*='next'], a[aria-label*='next']"
        ).first

        try:
            visible = await next_btn.is_visible(timeout=2000)
            if not visible:
                break
            await next_btn.click(timeout=10000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(1500)
        except Exception:
            break

        if page_num > 100:
            log.warning("Hit 100 page limit, stopping pagination")
            break

    return records


def parse_tyler_html(html: str, doc_type_hint: str = "") -> list[dict]:
    """
    Parse Tyler Eagle Web results HTML.
    Tyler renders results as card-style divs, each containing:
      - Document number • Document Type
      - Recording Date | Grantor | Grantee | Legal
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    # Strategy 1: Tyler card layout
    # Each doc is a div containing the doc number and type in a heading
    # Look for divs that contain a 7-9 digit doc number
    result_containers = []

    # Common Tyler selectors
    for sel in [
        "div.document-item", "div.record-item", "div.result-item",
        "div[class*='document']", "div[class*='record']",
        "tbody tr", "table.results tr",
    ]:
        found = soup.select(sel)
        if found and len(found) > 1:
            result_containers = found
            break

    # Fallback: find all divs containing a doc number pattern
    if not result_containers:
        for div in soup.find_all("div"):
            text = div.get_text(" ", strip=True)
            if re.search(r"\b\d{7,10}\s*[•·]\s*[A-Z]", text):
                result_containers.append(div)

    for container in result_containers:
        try:
            rec = _parse_tyler_card(container, doc_type_hint)
            if rec:
                records.append(rec)
        except Exception as exc:
            log.debug("Card parse error: %s", exc)

    # Strategy 2: table fallback
    if not records:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [
                th.get_text(" ", strip=True).lower()
                for th in rows[0].find_all(["th", "td"])
            ]
            if not any(h in headers for h in
                       ["doc", "grantor", "recording", "instrument", "type"]):
                continue
            for row in rows[1:]:
                rec = _parse_table_row(row, headers, doc_type_hint)
                if rec:
                    records.append(rec)

    return records


def _parse_tyler_card(container, doc_type_hint: str) -> Optional[dict]:
    text = container.get_text(" ", strip=True)
    if not text or len(text) < 10:
        return None

    # Doc number and type — "57018950 • DEED" or "57018950 · MORTGAGE"
    num_type_match = re.search(r"(\d{7,10})\s*[•·\-]\s*([A-Z][A-Z\s/]+?)(?:\s{2,}|\n|Recording)", text)
    if not num_type_match:
        num_type_match = re.search(r"(\d{7,10})", text)
        doc_num = num_type_match.group(1) if num_type_match else ""
        doc_type_raw = doc_type_hint
    else:
        doc_num = num_type_match.group(1)
        doc_type_raw = num_type_match.group(2).strip()

    if not doc_num:
        return None

    # Recording date
    date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
    filed = parse_date(date_match.group(1)) if date_match else ""

    # Grantor / Grantee
    grantor = ""
    grantee = ""

    gran_match = re.search(
        r"Grantor[^:]*:\s*(.*?)(?:Grantee|Legal|Recording|\n\n)", text, re.I | re.S
    )
    if gran_match:
        grantor = normalize(re.sub(r"\s+", " ", gran_match.group(1)))

    gran_match2 = re.search(
        r"Grantee[^:]*:\s*(.*?)(?:Legal|Recording|Parcel|\n\n)", text, re.I | re.S
    )
    if gran_match2:
        grantee = normalize(re.sub(r"\s+", " ", gran_match2.group(1)))

    # If labeled extraction failed, try structured child elements
    if not grantor:
        for elem in container.find_all(["td", "span", "div"]):
            label = elem.get_text(strip=True).lower()
            if label.startswith("grantor"):
                nxt = elem.find_next_sibling()
                if nxt:
                    grantor = normalize(nxt.get_text(strip=True))
                    break

    # Legal / parcel
    legal = ""
    legal_match = re.search(r"(Parcel[:\s]+[\d-]+)", text, re.I)
    if legal_match:
        legal = legal_match.group(1)
    else:
        legal_match2 = re.search(r"Legal[^:]*:\s*(.{10,80}?)(?:\n|Parcel|$)", text, re.I)
        if legal_match2:
            legal = legal_match2.group(1).strip()

    # Amount
    amount = None
    amt_match = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
    if amt_match:
        amount = safe_float(amt_match.group(0))

    # URL
    clerk_url = RECORDER_BASE
    anchor = container.find("a", href=True)
    if anchor:
        clerk_url = urljoin(RECORDER_BASE, anchor["href"])
    elif doc_num:
        clerk_url = f"{RECORDER_BASE}document/{doc_num}"

    cat, cat_label = categorize(doc_type_raw or doc_type_hint)

    # Only keep target document types
    if cat == "other" and not doc_type_hint:
        return None

    return {
        "doc_num":      doc_num,
        "doc_type":     normalize(doc_type_raw or doc_type_hint),
        "filed":        filed,
        "cat":          cat,
        "cat_label":    cat_label,
        "owner":        grantor,
        "grantee":      grantee,
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


def _parse_table_row(row, headers: list[str], doc_type_hint: str) -> Optional[dict]:
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

    i_doc     = ci("doc", "number", "instrument", "reception")
    i_type    = ci("type", "description")
    i_date    = ci("recording", "filed", "date")
    i_grantor = ci("grantor", "owner", "from")
    i_grantee = ci("grantee", "to", "buyer")
    i_legal   = ci("legal", "parcel")
    i_amount  = ci("amount", "consideration")

    doc_num = cell(i_doc) or cell(0)
    if not doc_num or not re.search(r"\d{5,}", doc_num):
        return None

    doc_type_raw = cell(i_type) or doc_type_hint
    cat, cat_label = categorize(doc_type_raw)
    if cat == "other" and not doc_type_hint:
        return None

    clerk_url = RECORDER_BASE
    anchor = (cells[max(i_doc, 0)] if i_doc < len(cells) else cells[0]).find("a", href=True)
    if anchor:
        clerk_url = urljoin(RECORDER_BASE, anchor["href"])

    return {
        "doc_num":      doc_num.strip(),
        "doc_type":     normalize(doc_type_raw),
        "filed":        parse_date(cell(i_date)),
        "cat":          cat,
        "cat_label":    cat_label,
        "owner":        normalize(cell(i_grantor)),
        "grantee":      normalize(cell(i_grantee)),
        "amount":       safe_float(cell(i_amount)),
        "legal":        cell(i_legal),
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
# Scoring
# ---------------------------------------------------------------------------

def score_record(rec: dict, all_records: list[dict]) -> tuple[int, list[str]]:
    flags: list[str] = []
    score = 30
    cat   = rec.get("cat", "")
    dtype = rec.get("doc_type", "")
    owner = rec.get("owner", "")
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
        if any(x in dtype for x in ("FEDERAL", "STATE", "TAX", "ASSESSMENT", "DELINQUENT")):
            flags.append("Tax lien")
        elif "MECHANIC" in dtype:
            flags.append("Mechanic lien")
        elif "CHILD" in dtype:
            flags.append("Child support lien")
        else:
            flags.append("Lien")
        score += 10

    if cat == "probate":
        flags.append("Probate / estate")
        score += 10

    owner_docs = [r for r in all_records if r.get("owner") == owner and r is not rec]
    has_lp = any("PENDENS" in r.get("doc_type","") for r in owner_docs) or "PENDENS" in dtype
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
# Output
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
# Main
# ---------------------------------------------------------------------------

async def main():
    end_date   = datetime.now(timezone.utc).replace(tzinfo=None)
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    fetched_at = datetime.now(timezone.utc).isoformat()

    date_from = start_date.strftime("%m/%d/%Y")
    date_to   = end_date.strftime("%m/%d/%Y")

    log.info("Summit County Lead Scraper | %s → %s", date_from, date_to)

    raw = await scrape(date_from, date_to)
    log.info("Raw records: %d", len(raw))

    # De-duplicate
    seen: set[str] = set()
    unique: list[dict] = []
    for rec in raw:
        key = rec.get("doc_num", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(rec)
        elif not key:
            unique.append(rec)

    # Score
    enriched: list[dict] = []
    for rec in unique:
        try:
            score, flags = score_record(rec, unique)
            rec["score"] = score
            rec["flags"] = flags
            enriched.append(rec)
        except Exception as exc:
            log.warning("Score error: %s", exc)

    enriched.sort(key=lambda r: r.get("score", 0), reverse=True)

    write_outputs(enriched, fetched_at, start_date, end_date)

    log.info(
        "Done. %d leads | top score: %s",
        len(enriched),
        enriched[0].get("score", "n/a") if enriched else "n/a",
    )


if __name__ == "__main__":
    asyncio.run(main())
