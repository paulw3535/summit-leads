"""
Summit County, Ohio – Motivated Seller Lead Scraper
Targets: clerk.summitoh.net (Clerk of Courts - Civil Division)
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

DISCLAIMER_PAGE = "https://clerk.summitoh.net/RecordsSearch/Disclaimer.asp?toPage=SelectDivision.asp"
CLERK_BASE      = "https://clerk.summitoh.net/RecordsSearch/"

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

TARGET_DOC_TYPES = {
    "CERTIFICATE OF JUDGMENT FOR LIEN": ("judgment",    "Certificate of Judgment for Lien"),
    "DECREE A CLOSURE":                 ("foreclosure", "Decree of Foreclosure"),
    "DELINQUENT TAX SERVICE RETURN":    ("lien",        "Delinquent Tax Lien"),
    "LIEN FILED":                       ("lien",        "Lien Filed"),
    "MECHANIC'S LIEN RELEASE BOND":     ("lien",        "Mechanic's Lien Release Bond"),
    "NOTICE OF BANKRUPTCY":             ("bankruptcy",  "Notice of Bankruptcy"),
    "NOTICE FILING DEATH CERTIFICATE":  ("probate",     "Notice Filing Death Certificate"),
    "STATE TAX LIEN FILED":             ("lien",        "State Tax Lien Filed"),
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
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip()[:10], fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return raw.strip()[:10]


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).upper().strip())


def categorize(doc_type: str) -> tuple[str, str]:
    upper = doc_type.upper().strip()
    for key, (cat, label) in TARGET_DOC_TYPES.items():
        if key in upper or upper in key:
            return cat, label
    if "LIEN" in upper:
        return "lien", doc_type.title()
    if "FORECLOS" in upper or "DECREE" in upper:
        return "foreclosure", doc_type.title()
    if "JUDGMENT" in upper or "CERTIFICATE" in upper:
        return "judgment", doc_type.title()
    if "BANKRUPTCY" in upper:
        return "bankruptcy", doc_type.title()
    if "DEATH" in upper or "PROBATE" in upper:
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
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Step 1: Disclaimer — click Agree
        log.info("Loading disclaimer …")
        await page.goto(DISCLAIMER_PAGE, timeout=60_000, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        try:
            await page.click("a:has-text('Agree')", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info("Agreed to disclaimer. URL: %s", page.url)
        except Exception as e:
            log.warning("Agree click failed: %s", e)

        await page.wait_for_timeout(2000)

        # Step 2: Click Civil
        try:
            await page.click("a:has-text('Civil')", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info("Clicked Civil. URL: %s", page.url)
        except Exception as e:
            log.warning("Civil click failed: %s", e)

        await page.wait_for_timeout(2000)

        # Step 3: Click search by date/document type
        for txt in [
            "Search By Judge / Date / Case Type / Document Type",
            "Search By Judge",
            "Document Type",
            "Search By Date",
        ]:
            try:
                await page.click(f"a:has-text('{txt}')", timeout=4000)
                await page.wait_for_load_state("networkidle", timeout=15000)
                log.info("Navigated via: %s | URL: %s", txt, page.url)
                break
            except Exception:
                pass

        await page.wait_for_timeout(2000)

        # Log what's on the page
        all_inputs = await page.evaluate("""
            Array.from(document.querySelectorAll('input,select')).map(el => ({
                tag: el.tagName, id: el.id, name: el.name,
                type: el.type || '', placeholder: el.placeholder || ''
            }))
        """)
        log.info("Form elements: %s", all_inputs)

        # Step 4: For each target document type, fill form and collect results
        for doc_type_name, (cat, cat_label) in TARGET_DOC_TYPES.items():
            log.info("Searching for: %s", doc_type_name)
            try:
                type_records = await _search_one_type(
                    page, date_from, date_to, doc_type_name, cat, cat_label
                )
                records.extend(type_records)
                log.info("  → %d records for %s", len(type_records), doc_type_name)
            except Exception as exc:
                log.warning("Failed %s: %s", doc_type_name, exc)
            await asyncio.sleep(2)

        await browser.close()

    log.info("Scrape complete: %d total records", len(records))
    return records


async def _search_one_type(
    page, date_from: str, date_to: str,
    doc_type_name: str, cat: str, cat_label: str
) -> list[dict]:
    records: list[dict] = []

    # Fill Case Filing Date
    for sel in [
        "input[name*='Date']", "input[id*='Date']",
        "input[name*='Filing']", "input[name*='CaseDate']",
        "input[type='text']:first-of-type",
    ]:
        try:
            await page.fill(sel, date_from, timeout=3000)
            log.debug("Filled date via %s", sel)
            break
        except Exception:
            pass

    # Select Document Type from dropdown
    for sel in [
        "select[name*='Doc']", "select[id*='Doc']",
        "select[name*='Type']", "select[id*='Type']",
        "select",
    ]:
        try:
            # Try exact label first, then partial match
            await page.select_option(sel, label=doc_type_name, timeout=3000)
            log.debug("Selected doc type via label: %s", doc_type_name)
            break
        except Exception:
            try:
                # Try selecting by partial text
                options = await page.evaluate(f"""
                    Array.from(document.querySelector('{sel}').options)
                    .map(o => ({{value: o.value, text: o.text}}))
                """)
                match = next(
                    (o for o in options
                     if doc_type_name.lower() in o['text'].lower() or
                        o['text'].lower() in doc_type_name.lower()),
                    None
                )
                if match:
                    await page.select_option(sel, value=match['value'], timeout=3000)
                    log.debug("Selected doc type via value: %s", match['text'])
                    break
            except Exception:
                pass

    # Click Search
    for sel in [
        "input[value='Search']", "button:has-text('Search')",
        "input[type='submit']", "button[type='submit']",
        "a:has-text('Search')",
    ]:
        try:
            await page.click(sel, timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            log.debug("Search submitted")
            break
        except Exception:
            pass

    await page.wait_for_timeout(2000)

    # Collect paginated results
    page_num = 0
    while True:
        page_num += 1
        html = await page.content()
        page_records = parse_results_html(html, cat, cat_label, page.url)
        records.extend(page_records)
        log.debug("  Page %d: %d records", page_num, len(page_records))

        # Next page
        next_btn = page.locator(
            "a:has-text('Next'), input[value='Next'], "
            "a[title*='next'], .next > a"
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

        if page_num > 50:
            break

    # Go back to search form for next type
    try:
        await page.go_back()
        await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(1000)
    except Exception:
        pass

    return records


def parse_results_html(html: str, cat: str, cat_label: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [
            th.get_text(" ", strip=True).lower()
            for th in rows[0].find_all(["th", "td"])
        ]
        if not any(h in headers for h in
                   ["case", "name", "date", "filed", "doc", "party", "type"]):
            continue

        def ci(*candidates):
            for c in candidates:
                for i, h in enumerate(headers):
                    if c in h:
                        return i
            return -1

        def cell(row, i):
            cells = row.find_all(["td", "th"])
            if i < 0 or i >= len(cells):
                return ""
            return cells[i].get_text(" ", strip=True)

        i_case    = ci("case", "number", "no")
        i_date    = ci("date", "filed")
        i_party1  = ci("plaintiff", "grantor", "name", "party")
        i_party2  = ci("defendant", "grantee")
        i_type    = ci("type", "doc", "description")
        i_amount  = ci("amount", "debt", "judgment")

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            try:
                case_num = cell(row, i_case) or cell(row, 0)
                if not case_num or not re.search(r"\w{3,}", case_num):
                    continue

                doc_type_raw = cell(row, i_type) or cat_label
                filed        = parse_date(cell(row, i_date))
                party1       = normalize(cell(row, i_party1))
                party2       = normalize(cell(row, i_party2))
                amount       = safe_float(cell(row, i_amount))

                # URL
                clerk_url = base_url
                link_cell = cells[max(i_case, 0)] if i_case < len(cells) else cells[0]
                anchor = link_cell.find("a", href=True)
                if anchor:
                    clerk_url = urljoin(base_url, anchor["href"])

                records.append({
                    "doc_num":      case_num.strip(),
                    "doc_type":     normalize(doc_type_raw),
                    "filed":        filed,
                    "cat":          cat,
                    "cat_label":    cat_label,
                    "owner":        party1,
                    "grantee":      party2,
                    "amount":       amount,
                    "legal":        "",
                    "clerk_url":    clerk_url,
                    "prop_address": "",
                    "prop_city":    "Summit County",
                    "prop_state":   "OH",
                    "prop_zip":     "",
                    "mail_address": "",
                    "mail_city":    "",
                    "mail_state":   "",
                    "mail_zip":     "",
                })
            except Exception as exc:
                log.debug("Row parse error: %s", exc)

    return records


# ---------------------------------------------------------------------------
# Scoring
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
        flags.append("Pre-foreclosure")
        score += 10

    if cat == "judgment":
        flags.append("Judgment lien")
        score += 10

    if cat == "lien":
        if any(x in dtype for x in ("STATE", "TAX", "DELINQUENT")):
            flags.append("Tax lien")
        elif "MECHANIC" in dtype:
            flags.append("Mechanic lien")
        else:
            flags.append("Lien")
        score += 10

    if cat == "probate":
        flags.append("Probate / estate")
        score += 10

    if cat == "bankruptcy":
        flags.append("Bankruptcy filed")
        score += 10

    owner_docs = [r for r in all_records if r.get("owner") == owner and r is not rec]
    has_fc = any(r.get("cat") == "foreclosure" for r in owner_docs)
    has_lien = any(r.get("cat") == "lien" for r in owner_docs)
    if has_fc and has_lien:
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
        "source":       "Summit County Clerk of Courts – Civil Division",
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
        path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
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
                "Source":                 "Summit County Clerk of Courts – Civil Division",
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

    seen: set[str] = set()
    unique: list[dict] = []
    for rec in raw:
        key = rec.get("doc_num", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(rec)
        elif not key:
            unique.append(rec)

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
