"""
Summit County, Ohio - Motivated Seller Lead Scraper
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CLERK_BASE_URL = "https://clerkweb.summitoh.net/"
PARCEL_SEARCH_URL = (
    "https://propertyaccess.summitoh.net/search/commonsearch.aspx?mode=realprop"
)
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))

DOC_TYPES: dict[str, tuple[str, str]] = {
    "LP":       ("foreclosure", "Lis Pendens"),
    "NOFC":     ("foreclosure", "Notice of Foreclosure"),
    "TAXDEED":  ("tax",         "Tax Deed"),
    "JUD":      ("judgment",    "Judgment"),
    "CCJ":      ("judgment",    "Certified Judgment"),
    "DRJUD":    ("judgment",    "Domestic Judgment"),
    "LNCORPTX": ("lien",       "Corp Tax Lien"),
    "LNIRS":    ("lien",       "IRS Lien"),
    "LNFED":    ("lien",       "Federal Lien"),
    "LN":       ("lien",       "Lien"),
    "LNMECH":   ("lien",       "Mechanic Lien"),
    "LNHOA":    ("lien",       "HOA Lien"),
    "MEDLN":    ("lien",       "Medicaid Lien"),
    "PRO":      ("probate",    "Probate Document"),
    "NOC":      ("noc",        "Notice of Commencement"),
    "RELLP":    ("release",    "Release Lis Pendens"),
}

REPO_ROOT      = Path(__file__).resolve().parent.parent
DASHBOARD_JSON = REPO_ROOT / "dashboard" / "records.json"
DATA_JSON      = REPO_ROOT / "data"      / "records.json"
GHL_CSV        = REPO_ROOT / "data"      / "ghl_export.csv"

RETRY_ATTEMPTS = 3
RETRY_DELAY    = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def retry(fn, *args, attempts: int = RETRY_ATTEMPTS, delay: float = RETRY_DELAY, **kwargs):
    last_exc: Exception = RuntimeError("unknown")
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            log.warning("Attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(delay)
    raise last_exc


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(value))
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_date(raw: str) -> Optional[str]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass
    return None


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.upper().strip())


def name_variants(full_name: str) -> list[str]:
    n = normalize_name(full_name)
    variants = {n}
    if "," in n:
        parts = [p.strip() for p in n.split(",", 1)]
        last, rest = parts[0], parts[1]
        variants.add(f"{rest} {last}")
        variants.add(f"{last} {rest}")
        first = rest.split()[0] if rest.split() else rest
        variants.add(f"{first} {last}")
        variants.add(f"{last} {first}")
    else:
        tokens = n.split()
        if len(tokens) >= 2:
            first, last = tokens[0], tokens[-1]
            variants.add(f"{last}, {' '.join(tokens[1:])}")
            variants.add(f"{last} {first}")
            variants.add(f"{first} {last}")
    return list(variants)


# ---------------------------------------------------------------------------
# 1. Property Appraiser - bulk parcel data
# ---------------------------------------------------------------------------

def _download_parcel_zip(session: requests.Session) -> Optional[bytes]:
    log.info("Fetching parcel bulk data page ...")
    resp = session.get(PARCEL_SEARCH_URL, timeout=60)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    form_data: dict[str, str] = {}
    for inp in soup.select("input[type=hidden]"):
        name = inp.get("name", "")
        value = inp.get("value", "")
        if name:
            form_data[name] = value

    candidate_targets = [
        "ctl00$MainContent$btnExport",
        "ctl00$MainContent$lnkExport",
        "ctl00$MainContent$btnDownload",
        "ctl00$cphMain$btnExport",
    ]

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "__doPostBack" in href:
            m = re.search(r"__doPostBack\('([^']+)'", href)
            if m:
                candidate_targets.append(m.group(1).replace("\\x24", "$"))

    for target in candidate_targets:
        try:
            post_data = dict(form_data)
            post_data["__EVENTTARGET"]   = target
            post_data["__EVENTARGUMENT"] = ""
            dl_resp = session.post(
                PARCEL_SEARCH_URL, data=post_data, timeout=120, stream=True
            )
            ct = dl_resp.headers.get("Content-Type", "")
            if "zip" in ct or "octet" in ct or dl_resp.content[:2] == b"PK":
                log.info("Parcel ZIP downloaded (%d bytes)", len(dl_resp.content))
                return dl_resp.content
        except Exception as exc:
            log.debug("Target '%s' failed: %s", target, exc)

    log.warning("Could not download parcel ZIP - address enrichment disabled.")
    return None


def build_parcel_lookup(zip_bytes: bytes) -> dict[str, dict]:
    try:
        from dbfread import DBF
    except ImportError:
        log.error("dbfread not installed - skipping parcel lookup.")
        return {}

    lookup: dict[str, dict] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            dbf_names = [n for n in zf.namelist() if n.lower().endswith(".dbf")]
            if not dbf_names:
                log.warning("No DBF found in parcel ZIP.")
                return {}
            dbf_bytes = zf.read(dbf_names[0])
            log.info("Reading parcel DBF: %s", dbf_names[0])

        tmp_path = Path("/tmp/parcels.dbf")
        tmp_path.write_bytes(dbf_bytes)

        table = DBF(str(tmp_path), encoding="latin-1", ignore_missing_memofile=True)
        fields = table.field_names

        def col(*candidates: str) -> str:
            for c in candidates:
                if c in fields:
                    return c
                if c.upper() in fields:
                    return c.upper()
            return ""

        owner_col  = col("OWN1", "OWNER", "OWNERNAME")
        site_addr  = col("SITEADDR", "SITE_ADDR", "PROPADDR")
        site_city  = col("SITECITY", "SITE_CITY")
        site_zip   = col("SITEZIP",  "SITE_ZIP")
        mail_addr  = col("MAILADR1", "ADDR_1", "MAILADDR")
        mail_city  = col("MAILCITY", "CITY")
        mail_state = col("STATE",    "MAILSTATE")
        mail_zip   = col("MAILZIP",  "ZIP")

        for rec in table:
            try:
                owner_raw = str(rec.get(owner_col) or "").strip()
                if not owner_raw:
                    continue
                parcel = {
                    "prop_address": str(rec.get(site_addr)  or "").strip(),
                    "prop_city":    str(rec.get(site_city)  or "").strip(),
                    "prop_state":   "OH",
                    "prop_zip":     str(rec.get(site_zip)   or "").strip(),
                    "mail_address": str(rec.get(mail_addr)  or "").strip(),
                    "mail_city":    str(rec.get(mail_city)  or "").strip(),
                    "mail_state":   str(rec.get(mail_state) or "OH").strip(),
                    "mail_zip":     str(rec.get(mail_zip)   or "").strip(),
                }
                for variant in name_variants(owner_raw):
                    lookup.setdefault(variant, parcel)
            except Exception as row_exc:
                log.debug("Parcel row error: %s", row_exc)

        log.info("Parcel lookup built: %d owner keys", len(lookup))
    except Exception as exc:
        log.error("Parcel lookup failed: %s", exc)

    return lookup


def enrich_record(record: dict, parcel_lookup: dict[str, dict]) -> dict:
    owner = record.get("owner", "")
    if not owner:
        return record
    for variant in name_variants(owner):
        if variant in parcel_lookup:
            record.update(parcel_lookup[variant])
            return record
    return record


# ---------------------------------------------------------------------------
# 2. Clerk Portal - Playwright async scraper
# ---------------------------------------------------------------------------

async def scrape_clerk(start_date: datetime, end_date: datetime) -> list[dict]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("playwright not installed - cannot scrape clerk portal.")
        return []

    records: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        date_from = start_date.strftime("%m/%d/%Y")
        date_to   = end_date.strftime("%m/%d/%Y")

        for doc_code, (cat, cat_label) in DOC_TYPES.items():
            log.info("Searching clerk: %s (%s)", doc_code, cat_label)
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    await _search_doc_type(
                        page, doc_code, cat, cat_label,
                        date_from, date_to, records
                    )
                    break
                except Exception as exc:
                    log.warning("%s attempt %d/%d: %s", doc_code, attempt, RETRY_ATTEMPTS, exc)
                    if attempt < RETRY_ATTEMPTS:
                        await asyncio.sleep(RETRY_DELAY)

        await browser.close()

    log.info("Clerk scrape complete - %d raw records", len(records))
    return records


async def _search_doc_type(
    page, doc_code, cat, cat_label, date_from, date_to, records
):
    await page.goto(CLERK_BASE_URL, timeout=60_000)
    await page.wait_for_load_state("networkidle", timeout=30_000)

    for tab_text in ["Document Type", "Doc Type", "By Type"]:
        try:
            await page.click(f"text={tab_text}", timeout=4_000)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            break
        except Exception:
            pass

    for sel in [
        "select[name*='DocType']", "select[id*='DocType']",
        "input[name*='DocType']",  "input[id*='DocType']",
        "select[name*='Type']",    "input[name*='Type']",
    ]:
        try:
            elem = page.locator(sel).first
            tag = await elem.evaluate("el => el.tagName.toLowerCase()", timeout=2_000)
            if tag == "select":
                await elem.select_option(value=doc_code)
            else:
                await elem.clear()
                await elem.type(doc_code)
            break
        except Exception:
            pass

    for sel in [
        "input[name*='FromDate']", "input[id*='FromDate']",
        "input[name*='StartDate']","input[placeholder*='From']",
    ]:
        try:
            await page.fill(sel, date_from, timeout=3_000)
            break
        except Exception:
            pass

    for sel in [
        "input[name*='ToDate']",  "input[id*='ToDate']",
        "input[name*='EndDate']", "input[placeholder*='To']",
    ]:
        try:
            await page.fill(sel, date_to, timeout=3_000)
            break
        except Exception:
            pass

    submitted = False
    for sel in [
        "input[value='Search']", "button:has-text('Search')",
        "input[type=submit]",    "button[type=submit]",
    ]:
        try:
            await page.click(sel, timeout=5_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)
            submitted = True
            break
        except Exception:
            pass

    if not submitted:
        log.debug("Could not submit search form for %s", doc_code)
        return

    page_num = 0
    while True:
        page_num += 1
        html = await page.content()
        new_records = _parse_clerk_results_html(html, doc_code, cat, cat_label, page.url)
        records.extend(new_records)
        log.debug("  p%d -> %d records (%s)", page_num, len(new_records), doc_code)

        if not new_records and page_num > 1:
            break

        try:
            nxt = page.locator(
                "a:has-text('Next'), a:has-text('>'), input[value='Next']"
            ).first
            if not await nxt.is_visible(timeout=2_000):
                break
            await nxt.click(timeout=10_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            break


def _parse_clerk_results_html(html, doc_code, cat, cat_label, base_url) -> list[dict]:
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
                   ["doc", "document", "grantor", "filed", "date", "instrument"]):
            continue

        def ci(*candidates):
            for c in candidates:
                for i, h in enumerate(headers):
                    if c in h:
                        return i
            return -1

        i_docnum  = ci("doc #", "doc no", "document #", "instrument", "doc")
        i_doctype = ci("type", "doc type")
        i_filed   = ci("filed", "date filed", "recorded")
        i_grantor = ci("grantor", "owner", "seller", "from")
        i_grantee = ci("grantee", "buyer", "to")
        i_legal   = ci("legal", "description")
        i_amount  = ci("amount", "consideration", "debt")

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            try:
                def cell(i):
                    if i < 0 or i >= len(cells):
                        return ""
                    return cells[i].get_text(" ", strip=True)

                doc_num = cell(i_docnum) or cell(0)
                if not doc_num or doc_num.lower() in ("", "doc #", "document #"):
                    continue

                clerk_url = base_url
                link_cell = cells[max(i_docnum, 0)] if i_docnum < len(cells) else cells[0]
                anchor = link_cell.find("a", href=True)
                if anchor:
                    clerk_url = urljoin(base_url, anchor["href"])

                records.append({
                    "doc_num":      doc_num.strip(),
                    "doc_type":     (cell(i_doctype) or doc_code).upper().strip(),
                    "filed":        parse_date(cell(i_filed)) or cell(i_filed),
                    "cat":          cat,
                    "cat_label":    cat_label,
                    "owner":        normalize_name(cell(i_grantor)),
                    "grantee":      normalize_name(cell(i_grantee)),
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
                })
            except Exception as e:
                log.debug("Row parse error: %s", e)

    return records


# ---------------------------------------------------------------------------
# 3. Scoring
# ---------------------------------------------------------------------------

def score_record(rec: dict, all_records: list[dict]) -> tuple[int, list[str]]:
    flags: list[str] = []
    score = 30

    cat    = rec.get("cat", "")
    dtype  = rec.get("doc_type", "")
    owner  = rec.get("owner", "")
    amount = rec.get("amount")
    filed  = rec.get("filed", "")

    if cat == "foreclosure" and dtype != "RELLP":
        flags.append("Lis pendens")
        flags.append("Pre-foreclosure")
        score += 10

    if cat == "judgment":
        flags.append("Judgment lien")
        score += 10

    if cat in ("lien", "tax"):
        if dtype in ("LNCORPTX", "LNIRS", "LNFED", "TAXDEED"):
            flags.append("Tax lien")
        elif dtype == "LNMECH":
            flags.append("Mechanic lien")
        elif dtype == "LNHOA":
            flags.append("HOA lien")
        else:
            flags.append("Judgment lien")
        score += 10

    if cat == "probate":
        flags.append("Probate / estate")
        score += 10

    owner_docs = [r for r in all_records if r.get("owner") == owner and r is not rec]
    has_lp = any(r.get("doc_type") == "LP" for r in owner_docs) or dtype == "LP"
    has_fc = any(r.get("cat") == "foreclosure" for r in owner_docs)
    if has_lp and has_fc:
        score += 20

    if amount is not None:
        if amount > 100_000:
            flags.append("High debt (>$100k)")
            score += 15
        elif amount > 50_000:
            score += 10

    if owner and re.search(r"\b(LLC|INC|CORP|LTD|LP|TRUST|ESTATE)\b", owner):
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
# 4. Output writers
# ---------------------------------------------------------------------------

GHL_FIELDNAMES = [
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


def write_outputs(
    records: list[dict],
    fetched_at: str,
    start_date: datetime,
    end_date: datetime,
) -> None:
    payload = {
        "fetched_at":   fetched_at,
        "source":       "Summit County Clerk of Courts",
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
        writer = csv.DictWriter(f, fieldnames=GHL_FIELDNAMES)
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
                "Source":                 "Summit County Clerk of Courts",
                "Public Records URL":     rec.get("clerk_url", ""),
            })
    log.info("Wrote GHL CSV: %s", GHL_CSV)


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

async def main() -> None:
    end_date   = datetime.now(timezone.utc).replace(tzinfo=None)
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    fetched_at = datetime.now(timezone.utc).isoformat()

    log.info(
        "Summit County Lead Scraper | %s to %s",
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
    )

    # 1. Parcel lookup
    parcel_lookup: dict[str, dict] = {}
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })
        zip_bytes = retry(_download_parcel_zip, session)
        if zip_bytes:
            parcel_lookup = build_parcel_lookup(zip_bytes)
    except Exception as exc:
        log.error("Parcel data unavailable: %s", exc)

    # 2. Clerk scrape
    raw_records = await scrape_clerk(start_date, end_date)

    # De-duplicate by doc_num
    seen: set[str] = set()
    unique: list[dict] = []
    for rec in raw_records:
        key = rec.get("doc_num", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(rec)
        elif not key:
            unique.append(rec)

    # 3. Enrich + score
    enriched: list[dict] = []
    for rec in unique:
        try:
            rec = enrich_record(rec, parcel_lookup)
            score, flags = score_record(rec, unique)
            rec["score"] = score
            rec["flags"] = flags
            enriched.append(rec)
        except Exception as exc:
            log.warning("Record skipped: %s", exc)

    enriched.sort(key=lambda r: r.get("score", 0), reverse=True)

    # 4. Write outputs
    write_outputs(enriched, fetched_at, start_date, end_date)

    log.info(
        "Done. %d leads | %d with address | Top score: %s",
        len(enriched),
        sum(1 for r in enriched if r.get("prop_address")),
        enriched[0].get("score", "n/a") if enriched else "n/a",
    )


if __name__ == "__main__":
    asyncio.run(main())
