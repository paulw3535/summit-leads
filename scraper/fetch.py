"""
Summit County, Ohio – Motivated Seller Lead Scraper
Targets: clerk.summitoh.net/PublicSite/SearchByMixed.aspx
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
SEARCH_URL      = "https://clerk.summitoh.net/PublicSite/SearchByMixed.aspx"
CLERK_BASE      = "https://clerk.summitoh.net/PublicSite/"

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

TARGET_DOC_TYPES = {
    "DECREE OF FORECLOSURE.":                                ("foreclosure", "Decree of Foreclosure"),
    "FORECLOSURE COMPLAINT":                                 ("foreclosure", "Foreclosure Complaint"),
    "CLERK'S CERTIFICATE FOR PENDING SUITE FOR LIS PENDENS": ("foreclosure", "Lis Pendens"),
    "DELINQUENT TAX SHERIFF'S RETURN":                       ("lien",        "Delinquent Tax Lien"),
    "STATE TAX LIEN FILED.":                                 ("lien",        "State Tax Lien Filed"),
    "MECHANIC'S LIEN RELEASE BOND":                          ("lien",        "Mechanic's Lien Release Bond"),
    "AKRON MUNI CERT. OF JUDGMENT LIEN FILED":               ("judgment",    "Certificate of Judgment Lien"),
    "BARBERTON MUNI CERT. OF JUDGMENT LIEN FILED":           ("judgment",    "Certificate of Judgment Lien"),
    "CUYA. FALLS MUNI CERT. OF JUDGMENT LIEN FILED":         ("judgment",    "Certificate of Judgment Lien"),
    "NOTICE OF FILING DEATH CERTIFICATE":                    ("probate",     "Notice of Filing Death Certificate"),
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

DATE_FIELD   = "#ContentPlaceHolder1_tbFilingDate"
DOC_DROPDOWN = "#ContentPlaceHolder1_drpDocType"
SEARCH_BTN   = "#ContentPlaceHolder1_btnSearch"


def safe_float(v: Any) -> Optional[float]:
    try:
        cleaned = re.sub(r"[^\d.]", "", str(v))
        if cleaned:
            return float(cleaned)
        return None
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
        if key == upper:
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

        log.info("Loading disclaimer ...")
        await page.goto(DISCLAIMER_PAGE, timeout=60_000, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        try:
            await page.click("a:has-text('Agree')", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info("Agreed. URL: %s", page.url)
        except Exception as e:
            log.warning("Agree failed: %s", e)

        await page.wait_for_timeout(1500)
        try:
            await page.click("a:has-text('Civil')", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info("Civil clicked. URL: %s", page.url)
        except Exception as e:
            log.warning("Civil click failed: %s", e)

        await page.wait_for_timeout(1500)
        try:
            await page.click(
                "a:has-text('Search By Judge / Date / Case Type / Document Type')",
                timeout=5000
            )
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info("Search form URL: %s", page.url)
        except Exception as e:
            log.warning("Nav failed, going direct: %s", e)
            await page.goto(SEARCH_URL, timeout=30000, wait_until="networkidle")

        await page.wait_for_timeout(2000)

        dropdown_options = await page.evaluate("""
            Array.from(document.querySelector('#ContentPlaceHolder1_drpDocType').options)
            .map(o => ({value: o.value, text: o.text.trim().toUpperCase()}))
        """)

        option_lookup = {opt['text']: opt['value'] for opt in dropdown_options}
        log.info("Dropdown has %d options", len(dropdown_options))

        for doc_type_key, (cat, cat_label) in TARGET_DOC_TYPES.items():
            opt_value = option_lookup.get(doc_type_key.upper())
            if not opt_value:
                log.warning("No exact match for '%s' -- skipping", doc_type_key)
                continue

            log.info("Searching: %s", doc_type_key)
            try:
                type_records = await _search_one_type(
                    page, date_from, opt_value, doc_type_key, cat, cat_label
                )
                records.extend(type_records)
                log.info("  -> %d records for %s", len(type_records), doc_type_key)
            except Exception as exc:
                log.warning("Failed %s: %s", doc_type_key, exc)

            await asyncio.sleep(2)

        await browser.close()

    log.info("Scrape complete: %d total records", len(records))
    return records


async def _search_one_type(
    page, date_from: str, opt_value: str,
    doc_type_label: str, cat: str, cat_label: str
) -> list[dict]:
    records: list[dict] = []

    await page.goto(SEARCH_URL, timeout=30000, wait_until="networkidle")
    await page.wait_for_timeout(2000)
    await page.wait_for_selector(DATE_FIELD, timeout=10000)

    try:
        await page.fill(DATE_FIELD, date_from, timeout=5000)
    except Exception as e:
        log.warning("Date fill failed: %s", e)

    try:
        await page.select_option(DOC_DROPDOWN, value=opt_value, timeout=5000)
    except Exception as e:
        log.warning("Select failed: %s", e)

    try:
        await page.click(SEARCH_BTN, timeout=5000)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        body_text = await page.inner_text("body")
        log.info("Results (first 200): %s", body_text[:200].replace("\n", " "))

        # Log all table headers to debug parsing
        html_snippet = await page.content()
        soup_debug = BeautifulSoup(html_snippet, "lxml")
        for t in soup_debug.find_all("table"):
            rows = t.find_all("tr")
            if rows:
                hdrs = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
                if hdrs:
                    log.info("TABLE HEADERS FOUND: %s", hdrs)
                    if len(rows) > 1:
                        first_row = [td.get_text(strip=True) for td in rows[1].find_all(["td", "th"])]
                        log.info("TABLE FIRST ROW: %s", first_row)

    except Exception as e:
        log.warning("Search click failed: %s", e)
        return records

    page_num = 0
    while True:
        page_num += 1
        html = await page.content()
        page_records = parse_results_html(html, cat, cat_label, page.url)
        records.extend(page_records)
        log.info("  Page %d: %d records parsed", page_num, len(page_records))

        next_btn = page.locator(
            "a:has-text('Next'), input[value='Next'], .next > a"
        ).first
        try:
            if not await next_btn.is_visible(timeout=2000):
                break
            await next_btn.click(timeout=10000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(1500)
        except Exception:
            break

        if page_num > 50:
            break

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
                   ["case", "filing", "caption", "number",
                    "date", "plaintiff", "name", "party"]):
            continue

        log.info("Results table headers: %s", headers)

        i_date    = -1
        i_case    = -1
        i_caption = -1
        i_amount  = -1

        for i, h in enumerate(headers):
            if "filing date" in h or h == "date":
                i_date = i
            elif "case number" in h or "case no" in h:
                i_case = i
            elif "case caption" in h or "caption" in h:
                i_caption = i
            elif "amount" in h or "debt" in h:
                i_amount = i

        if i_date < 0:
            i_date = 0
        if i_case < 0:
            i_case = 1
        if i_caption < 0:
            i_caption = 2

        log.info("Column mapping -- date:%d case:%d caption:%d", i_date, i_case, i_caption)

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            def cell(i):
                if i < 0 or i >= len(cells):
                    return ""
                return cells[i].get_text(" ", strip=True)

            try:
                case_num = cell(i_case)
                if not case_num or not re.search(r"\w{3,}", case_num):
                    continue
                if case_num.lower() in (
                    "case number", "case no", "number", "no.",
                    "filing date", "case caption", "caption", "date"
                ):
                    continue

                filed   = parse_date(cell(i_date))
                caption = normalize(cell(i_caption))
                amount  = safe_float(cell(i_amount))

                owner   = caption
                grantee = ""
                if " VS " in caption:
                    parts   = caption.split(" VS ", 1)
                    owner   = parts[0].strip()
                    grantee = parts[1].strip()
                elif " V " in caption:
                    parts   = caption.split(" V ", 1)
                    owner   = parts[0].strip()
                    grantee = parts[1].strip()

                clerk_url = base_url
                lc = cells[i_case] if i_case < len(cells) else cells[0]
                anchor = lc.find("a", href=True)
                if anchor:
                    clerk_url = urljoin(base_url, anchor["href"])

                records.append({
                    "doc_num":      case_num.strip(),
                    "doc_type":     normalize(cat_label),
                    "filed":        filed,
                    "cat":          cat,
                    "cat_label":    cat_label,
                    "owner":        owner,
                    "grantee":      grantee,
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
                log.debug("Row error: %s", exc)

    return records


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
    if (any(r.get("cat") == "foreclosure" for r in owner_docs) and
            any(r.get("cat") == "lien" for r in owner_docs)):
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
        "source":       "Summit County Clerk of Courts - Civil Division",
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
                "Source":                 "Summit County Clerk of Courts - Civil Division",
                "Public Records URL":     rec.get("clerk_url", ""),
            })
    log.info("Wrote GHL CSV: %s", GHL_CSV)


async def main():
    end_date   = datetime.now(timezone.utc).replace(tzinfo=None)
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    fetched_at = datetime.now(timezone.utc).isoformat()

    date_from = start_date.strftime("%m/%d/%Y")
    date_to   = end_date.strftime("%m/%d/%Y")

    log.info("Summit County Lead Scraper | %s -> %s", date_from, date_to)

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
