"""
Summit County, Ohio – Motivated Seller Lead Scraper
Targets: clerk.summitoh.net/PublicSite/SearchByMixed.aspx
Searches day by day for LOOKBACK_DAYS to capture all filings.
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


async def scrape(start_date: datetime, end_date: datetime) -> list[dict]:
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

        # Step 1: Disclaimer
        log.info("Loading disclaimer ...")
        await page.goto(DISCLAIMER_PAGE, timeout=60_000, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        try:
            await page.click("a:has-text('Agree')", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info("Agreed. URL: %s", page.url)
        except Exception as e:
            log.warning("Agree failed: %s", e)

        # Step 2: Civil
        await page.wait_for_timeout(1500)
        try:
            await page.click("a:has-text('Civil')", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            log.info("Civil clicked. URL: %s", page.url)
        except Exception as e:
            log.warning("Civil click failed: %s", e)

        # Step 3: Search form
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

        # Read dropdown options once
        dropdown_options = await page.evaluate("""
            Array.from(document.querySelector('#ContentPlaceHolder1_drpDocType').options)
            .map(o => ({value: o.value, text: o.text.trim().toUpperCase()}))
        """)
        option_lookup = {opt['text']: opt['value'] for opt in dropdown_options}
        log.info("Dropdown has %d options", len(dropdown_options))

        # Build list of dates to search (one per day)
        search_dates = []
        current = start_date
        while current <= end_date:
            search_dates.append(current.strftime("%m/%d/%Y"))
            current += timedelta(days=1)

        log.info("Will search %d days x %d doc types = %d searches",
                 len(search_dates), len(TARGET_DOC_TYPES),
                 len(search_dates) * len(TARGET_DOC_TYPES))

        # Step 4: Search each doc type for each day
        for doc_type_key, (cat, cat_label) in TARGET_DOC_TYPES.items():
            opt_value = option_lookup.get(doc_type_key.upper())
            if not opt_value:
                log.warning("No match for '%s' -- skipping", doc_type_key)
                continue

            for search_date in search_dates:
                try:
                    day_records = await _search_one_type(
                        page, search_date, opt_value, doc_type_key, cat, cat_label
                    )
                    if day_records:
                        log.info("  %s | %s -> %d records",
                                 search_date, doc_type_key, len(day_records))
                        records.extend(day_records)
                except Exception as exc:
                    log.warning("Failed %s %s: %s", search_date, doc_type_key, exc)

                await asyncio.sleep(1)

        # Enrichment pass: visit each case detail page to grab property address
        try:
            await enrich_case_details(page, records)
        except Exception as exc:
            log.warning("Enrichment phase failed: %s", exc)

        await browser.close()

    log.info("Scrape complete: %d total records", len(records))
    return records


async def _search_one_type(
    page, search_date: str, opt_value: str,
    doc_type_label: str, cat: str, cat_label: str
) -> list[dict]:
    records: list[dict] = []

    # Start fresh at search form
    await page.goto(SEARCH_URL, timeout=30000, wait_until="networkidle")
    await page.wait_for_timeout(1500)
    await page.wait_for_selector(DATE_FIELD, timeout=10000)

    # Fill exact date
    try:
        await page.fill(DATE_FIELD, search_date, timeout=5000)
        await page.wait_for_timeout(300)
    except Exception as e:
        log.warning("Date fill failed: %s", e)
        return records

    # Select doc type
    try:
        await page.select_option(DOC_DROPDOWN, value=opt_value, timeout=5000)
        await page.wait_for_timeout(300)
    except Exception as e:
        log.warning("Select failed: %s", e)
        return records

    # Click Search
    try:
        await page.click(SEARCH_BTN, timeout=5000)

        # Wait for URL to become results page
        for _ in range(30):
            await page.wait_for_timeout(500)
            if "SearchByMixedResults" in page.url:
                break

        await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(1500)

    except Exception as e:
        log.warning("Search click failed: %s", e)
        return records

    # Must be on results page
    if "SearchByMixedResults" not in page.url:
        return records

    # Check for no entries
    body_text = await page.inner_text("body")
    if "No Entries Found" in body_text:
        return records

    # Collect paginated results
    page_num = 0
    while True:
        page_num += 1
        html = await page.content()
        page_records = parse_results_html(html, cat, cat_label, page.url)
        records.extend(page_records)

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

    date_pat = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
    case_pat = re.compile(r"[A-Z]{1,4}-?\d{4}", re.I)

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        c0 = cells[0].get_text(strip=True)
        c1 = cells[1].get_text(strip=True)
        c2 = cells[2].get_text(" ", strip=True)

        if not date_pat.match(c0):
            continue
        if not case_pat.search(c1):
            continue

        try:
            filed    = parse_date(c0)
            case_num = c1.strip()
            caption  = normalize(c2)

            if not case_num or not caption:
                continue

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
            anchor = cells[1].find("a", href=True)
            if anchor:
                clerk_url = urljoin(base_url, anchor["href"])

            records.append({
                "doc_num":      case_num,
                "doc_type":     normalize(cat_label),
                "filed":        filed,
                "cat":          cat,
                "cat_label":    cat_label,
                "owner":        owner,
                "grantee":      grantee,
                "amount":       None,
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


# ---------------------------------------------------------------------------
# Case-detail enrichment: pull property address from CaseDetail.aspx
# ---------------------------------------------------------------------------

# Common street suffixes seen on Summit County filings
_STREET_SUFFIXES = (
    r"ST|AVE|RD|DR|BLVD|LN|CT|PL|WAY|CIR|PKWY|TRL|HWY|TER|"
    r"STREET|AVENUE|ROAD|DRIVE|BOULEVARD|LANE|COURT|PLACE|"
    r"CIRCLE|PARKWAY|TRAIL|HIGHWAY|TERRACE"
)

_ADDR_RE = re.compile(
    # Street: number + 1..6 words + a suffix
    r"(\d{1,6}\s+(?:[A-Za-z0-9.\-']+\s+){1,6}"
    r"(?:" + _STREET_SUFFIXES + r"))\.?"
    # Separator: any whitespace OR comma (no newline required -- Summit
    # writes "3830 Nautilus Trail Aurora, OH 44202" on one line)
    r"[\s,]+"
    # City: starts with a letter, 2-40 chars
    r"([A-Za-z][A-Za-z .'\-]{1,40}?)"
    r"[\s,]+(?:OH|OHIO)\s+"
    r"(\d{5}(?:-\d{4})?)",
    re.IGNORECASE,
)


def extract_property_address(html: str, defendant: str) -> dict:
    """Find the defendant's property address on a CaseDetail.aspx Parties page.

    Strategy: isolate the DEFENDANT section, collect every street/city/zip
    found in it, and pick the address that appears most often. Co-owner
    defendants (spouse, joint owners) share the property address, while
    lender / HOA / lien-holder defendants list their business addresses --
    so frequency naturally promotes the property.
    """
    out = {
        "prop_address": "", "prop_city": "", "prop_state": "OH", "prop_zip": "",
        "mail_address": "", "mail_city": "", "mail_state": "", "mail_zip": "",
    }

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=False)

    # Isolate the DEFENDANT block. On Summit's Parties page, the headers
    # "DEFENDANT" and "DEFENDANT'S ATTORNEY" sit on adjacent lines (side-by-
    # side table columns), so anchor on the attorney header to skip past
    # both, then capture everything until the next section or end of page.
    block_match = (
        re.search(r"DEFENDANT'S\s+ATTORNEY\b(.*?)"
                  r"(?=\bDOCKETS\b|\bJUDGES\b|\bSERVICE\b|\Z)",
                  text, re.IGNORECASE | re.DOTALL)
        or
        re.search(r"\bDEFENDANT\b(.*?)"
                  r"(?=\bDOCKETS\b|\bJUDGES\b|\bSERVICE\b|\Z)",
                  text, re.IGNORECASE | re.DOTALL)
    )
    block = block_match.group(1) if block_match else text

    # Find all addresses in the defendant block
    from collections import Counter
    found = []
    for m in _ADDR_RE.finditer(block):
        street = re.sub(r"\s+", " ", m.group(1)).strip().title()
        city   = re.sub(r"\s+", " ", m.group(2)).strip().title()
        zipc   = m.group(3).strip()
        # Reject obvious courthouse / clerk addresses
        if zipc.startswith("44308") and "HIGH" in street.upper():
            continue
        found.append((street, city, zipc))

    if not found:
        return out

    # Most frequent address wins. Ties broken by first-seen order.
    counter = Counter(found)
    best, _count = counter.most_common(1)[0]

    # Soft preference: if grantee's surname appears and an address sits
    # right after it (within 200 chars), prefer that over plain frequency.
    if defendant:
        surname = defendant.upper().split()[-1] if defendant.split() else ""
        if surname:
            for m in re.finditer(re.escape(surname), block.upper()):
                window = block[m.end(): m.end() + 250]
                am = _ADDR_RE.search(window)
                if am:
                    street = re.sub(r"\s+", " ", am.group(1)).strip().title()
                    city   = re.sub(r"\s+", " ", am.group(2)).strip().title()
                    zipc   = am.group(3).strip()
                    if not (zipc.startswith("44308") and "HIGH" in street.upper()):
                        best = (street, city, zipc)
                        break

    out["prop_address"] = best[0]
    out["prop_city"]    = best[1]
    out["prop_zip"]     = best[2]
    out["mail_address"] = best[0]
    out["mail_city"]    = best[1]
    out["mail_state"]   = "OH"
    out["mail_zip"]     = best[2]
    return out


async def enrich_case_details(page, records: list[dict]) -> None:
    """Visit each foreclosure case-detail page and fill in property address."""
    targets = [r for r in records if r.get("cat") == "foreclosure" and r.get("clerk_url")]
    log.info("Enriching %d foreclosure cases with property addresses ...", len(targets))

    hits = 0
    for i, rec in enumerate(targets, 1):
        url = rec["clerk_url"]
        try:
            await page.goto(url, timeout=30000, wait_until="networkidle")
            await page.wait_for_timeout(600)
            html = await page.content()
            addr = extract_property_address(html, rec.get("grantee", ""))
            rec.update(addr)
            if addr["prop_address"]:
                hits += 1
                if i % 10 == 0 or i == len(targets):
                    log.info("  [%d/%d] enriched -- %d addresses found", i, len(targets), hits)
        except Exception as exc:
            log.warning("Enrich failed for %s: %s", rec.get("doc_num"), exc)
        await asyncio.sleep(0.4)

    log.info("Enrichment done: %d/%d addresses found", hits, len(targets))


    """Score a record. The 'lead' is the GRANTEE (defendant) -- not the
    OWNER field, which on civil filings is the plaintiff (a bank). Cross-doc
    matching and entity-type flags must therefore key off grantee."""
    flags: list[str] = []
    score = 30
    cat     = rec.get("cat", "")
    dtype   = rec.get("doc_type", "")
    grantee = rec.get("grantee", "") or rec.get("owner", "")  # fallback for non-civil docs
    amount  = rec.get("amount")
    filed   = rec.get("filed", "")

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
        score += 15

    if cat == "bankruptcy":
        flags.append("Bankruptcy filed")
        score += 10

    # Cross-doc: same defendant appears in multiple distress categories -> hot
    same_party = [
        r for r in all_records
        if r.get("grantee") and r.get("grantee") == grantee and r is not rec
    ]
    cats_seen = {r.get("cat") for r in same_party}
    if "foreclosure" in cats_seen and "lien" in cats_seen:
        flags.append("Multi-distress (foreclosure + lien)")
        score += 25
    elif len(cats_seen) >= 1:
        flags.append("Multiple filings")
        score += 10

    if amount:
        if amount > 100_000:
            flags.append("High debt (>$100k)")
            score += 15
        elif amount > 50_000:
            score += 10

    # Defendant entity type: individuals = motivated sellers, LLCs/trusts = not
    is_entity = bool(grantee) and bool(
        re.search(r"\b(LLC|INC|CORP|CORPORATION|LTD|TRUST|ESTATE OF|COMPANY|CO\.|ASSOC|ASSOCIATION|BANK|N\.A\.)\b",
                  grantee.upper())
    )
    if grantee and not is_entity:
        flags.append("Individual defendant")
        score += 15
    elif is_entity:
        flags.append("Entity defendant (low priority)")
        score -= 15

    try:
        if (datetime.now() - datetime.strptime(filed, "%Y-%m-%d")).days <= 7:
            flags.append("New this week")
            score += 5
    except Exception:
        pass

    if rec.get("prop_address"):
        flags.append("Address captured")
        score += 10

    return max(0, min(score, 100)), flags


GHL_FIELDS = [
    "First Name", "Last Name",
    "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Property Address", "Property City", "Property State", "Property Zip",
    "Lead Type", "Document Type", "Date Filed", "Document Number",
    "Amount/Debt Owed", "Seller Score", "Motivated Seller Flags",
    "Plaintiff",
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
    skipped_entity = 0
    with GHL_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=GHL_FIELDS)
        writer.writeheader()
        for rec in records:
            # The LEAD is the defendant (grantee). For non-civil records that
            # have no grantee, fall back to owner.
            lead_name = rec.get("grantee", "").strip() or rec.get("owner", "").strip()

            # Skip pure-entity defendants — they're not motivated sellers.
            # Keeps the CSV import-ready for GHL without garbage rows.
            if lead_name and re.search(
                r"\b(LLC|INC|CORP|CORPORATION|LTD|TRUST|COMPANY|CO\.|ASSOC|"
                r"ASSOCIATION|BANK|N\.A\.|AGENCY|DEPARTMENT)\b",
                lead_name.upper(),
            ):
                skipped_entity += 1
                continue

            first, last = _split_name(lead_name)
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
                "Plaintiff":              rec.get("owner", ""),
                "Source":                 "Summit County Clerk of Courts - Civil Division",
                "Public Records URL":     rec.get("clerk_url", ""),
            })
    log.info("Wrote GHL CSV: %s (%d rows, skipped %d entity defendants)",
             GHL_CSV, len(records) - skipped_entity, skipped_entity)


async def main():
    end_date   = datetime.now(timezone.utc).replace(tzinfo=None)
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    fetched_at = datetime.now(timezone.utc).isoformat()

    log.info("Summit County Lead Scraper | %s -> %s",
             start_date.strftime("%Y-%m-%d"),
             end_date.strftime("%Y-%m-%d"))

    raw = await scrape(start_date, end_date)
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
