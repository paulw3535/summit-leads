"""
Summit County, Ohio – Motivated Seller Lead Scraper
Tyler Self-Service: summitcountyoh-web.tylerhost.net
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

RECORDER_BASE  = "https://summitcountyoh-web.tylerhost.net/web/"
SEARCH_PAGE    = "https://summitcountyoh-web.tylerhost.net/web/search/DOCSEARCH236S2"
RESULTS_PAGE   = "https://summitcountyoh-web.tylerhost.net/web/searchResults/DOCSEARCH236S2"

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
    "DELINQUENT TAX LIEN":     ("lien",        "Delinquent Tax Lien"),
    "LIEN":                    ("lien",        "Lien"),
    "MECHANICS LIEN":          ("lien",        "Mechanic Lien"),
    "JUDGMENT LIEN":           ("judgment",    "Judgment Lien"),
    "CERTIFICATE OF JUDGMENT": ("judgment",    "Certificate of Judgment"),
    "TRANSFER ON DEATH":       ("probate",     "Transfer on Death"),
    "NOTICE OF COMMENCEMENT":  ("noc",         "Notice of Commencement"),
    "LIS PENDENS RELEASE":     ("release",     "Lis Pendens Release"),
    "RELEASE OF LIS PENDENS":  ("release",     "Release of Lis Pendens"),
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
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    try:
        return datetime.strptime(raw.strip()[:10], "%m/%d/%Y").strftime("%Y-%m-%d")
    except Exception:
        pass
    return raw.strip()[:10]


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).upper().strip())


def categorize(doc_type: str) -> tuple[str, str]:
    upper = doc_type.upper().strip()
    for key, (cat, label) in TARGET_DOC_TYPES.items():
        if key in upper:
            return cat, label
    if "LIEN" in upper:
        return "lien", doc_type.title()
    if "PENDENS" in upper or "FORECLOS" in upper or "SHERIFF" in upper:
        return "foreclosure", doc_type.title()
    if "JUDGMENT" in upper:
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

        # Step 1: Load the home page to establish session
        log.info("Establishing session …")
        await page.goto(RECORDER_BASE, timeout=60_000, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Step 2: Navigate to the search form
        log.info("Loading search form: %s", SEARCH_PAGE)
        await page.goto(SEARCH_PAGE, timeout=60_000, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Log what's on the page for debugging
        html = await page.content()
        soup = BeautifulSoup(html, "lxml")
        inputs = [i.get("id", i.get("name", "?")) for i in soup.find_all("input")]
        links  = [(a.get("id","?"), a.get("href","?")) for a in soup.find_all("a", href=True)]
        log.info("Inputs on page: %s", inputs[:10])
        log.info("Links on page: %s", links[:10])

        # Step 3: Fill date fields using JavaScript (most reliable for jQuery Mobile)
        log.info("Setting dates via JS: %s → %s", date_from, date_to)
        await page.evaluate(f"""
            (function() {{
                var inputs = document.querySelectorAll('input[type="text"], input[type="date"]');
                console.log('Found inputs:', inputs.length);
                inputs.forEach(function(inp) {{
                    var id = (inp.id || '').toLowerCase();
                    var name = (inp.name || '').toLowerCase();
                    if (id.includes('from') || name.includes('from') || id.includes('start')) {{
                        inp.value = '{date_from}';
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                    if (id.includes('to') || name.includes('to') || id.includes('end')) {{
                        inp.value = '{date_to}';
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                }});
            }})();
        """)
        await page.wait_for_timeout(1000)

        # Step 4: Click searchButton by ID using JavaScript
        log.info("Clicking search button via JS …")
        clicked = await page.evaluate("""
            (function() {
                var btn = document.getElementById('searchButton');
                if (btn) { btn.click(); return 'clicked searchButton'; }
                var btns = document.querySelectorAll('a[href*="searchResults"], a[href*="DOCSEARCH"]');
                if (btns.length > 0) { btns[0].click(); return 'clicked href match'; }
                return 'no button found';
            })();
        """)
        log.info("Click result: %s", clicked)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        current_url = page.url
        log.info("After search, URL: %s", current_url)

        # Step 5: If still on search page, try Playwright click
        if "search/" in current_url and "searchResults" not in current_url:
            log.warning("JS click didn't navigate, trying Playwright click")
            for sel in ["#searchButton", "a#searchButton", "a[href*='searchResults']"]:
                try:
                    await page.click(sel, timeout=5000)
                    await page.wait_for_load_state("networkidle", timeout=20000)
                    log.info("Playwright click worked: %s", sel)
                    break
                except Exception as e:
                    log.debug("Playwright click failed %s: %s", sel, e)
            await page.wait_for_timeout(2000)

        # Step 6: Parse results
        current_url = page.url
        log.info("Results URL: %s", current_url)
        html = await page.content()

        # Check how many results we got
        result_match = re.search(r"(\d+)\s+Total\s+Results", html, re.I)
        if result_match:
            log.info("Total results found: %s", result_match.group(1))

        # Extract left panel doc type filters
        soup = BeautifulSoup(html, "lxml")
        left_panel = _extract_left_panel(soup, page.url)
        log.info("Left panel types: %d", len(left_panel))

        if left_panel:
            for type_name, href in left_panel.items():
                upper = type_name.upper()
                is_target = (
                    any(key in upper for key in TARGET_DOC_TYPES) or
                    any(upper in key for key in TARGET_DOC_TYPES)
                )
                if not is_target:
                    continue
                log.info("Collecting: %s", type_name)
                try:
                    type_records = await _collect_type(page, href, type_name)
                    records.extend(type_records)
                    log.info("  → %d records for %s", len(type_records), type_name)
                except Exception as exc:
                    log.warning("Failed %s: %s", type_name, exc)
                await asyncio.sleep(2)
        else:
            log.info("No left panel — parsing all results and filtering")
            all_recs = await _collect_all_pages(page)
            log.info("All results parsed: %d", len(all_recs))
            for rec in all_recs:
                cat, _ = categorize(rec.get("doc_type", ""))
                if cat != "other":
                    records.append(rec)

        await browser.close()

    log.info("Scrape complete: %d total records", len(records))
    return records


def _extract_left_panel(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for a in soup.select("ul li a, div.filter a, aside a, .facet a, nav a"):
        text = re.sub(r"\s*\d+\s*$", "", a.get_text(strip=True)).strip().upper()
        href = a.get("href", "")
        if text and href and len(text) > 2:
            links[text] = urljoin(base_url, href)
    return links


async def _collect_type(page, href: str, type_name: str) -> list[dict]:
    if href.startswith("http"):
        await page.goto(href, timeout=30000, wait_until="networkidle")
    else:
        try:
            await page.click(f"text={type_name.title()}", timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception as exc:
            log.warning("Could not navigate to %s: %s", type_name, exc)
            return []
    await page.wait_for_timeout(2000)
    return await _collect_all_pages(page, doc_type_hint=type_name)


async def _collect_all_pages(page, doc_type_hint: str = "") -> list[dict]:
    records: list[dict] = []
    page_num = 0

    while True:
        page_num += 1
        html = await page.content()
        page_records = parse_tyler_html(html, doc_type_hint)
        records.extend(page_records)
        log.debug("  Page %d: %d records", page_num, len(page_records))

        next_btn = page.locator(
            "a#nextButton, a:has-text('Next'), .next > a, li.next > a"
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
            break

    return records


def parse_tyler_html(html: str, doc_type_hint: str = "") -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    # Tyler renders results as list items containing doc number • type
    containers = []
    for sel in [
        "li.ss-listview-internal",
        "li[class*='result']",
        "div.document-item",
        "div[class*='document']",
        "tbody tr",
    ]:
        found = soup.select(sel)
        if found:
            containers = found
            break

    if not containers:
        containers = [
            el for el in soup.find_all(["li", "div"])
            if re.search(r"\b\d{7,10}\s*[•·]\s*[A-Z]", el.get_text(" ", strip=True))
        ]

    for c in containers:
        try:
            rec = _parse_tyler_card(c, doc_type_hint)
            if rec:
                records.append(rec)
        except Exception as exc:
            log.debug("Card error: %s", exc)

    # Table fallback
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
                       ["doc", "grantor", "recording", "instrument"]):
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

    m = re.search(
        r"(\d{7,10})\s*[•·\-]\s*([A-Z][A-Z\s/&]+?)(?:\s{2,}|\n|Recording|$)", text
    )
    if m:
        doc_num = m.group(1)
        doc_type_raw = m.group(2).strip()
    else:
        nm = re.search(r"(\d{7,10})", text)
        doc_num = nm.group(1) if nm else ""
        doc_type_raw = doc_type_hint

    if not doc_num:
        return None

    dm = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
    filed = parse_date(dm.group(1)) if dm else ""

    grantor = ""
    gm = re.search(
        r"Grantor[^:]*[:\s]+(.*?)(?:Grantee|Legal|Recording|\n\n|$)", text, re.I | re.S
    )
    if gm:
        grantor = normalize(re.sub(r"\s+", " ", gm.group(1))[:80])

    grantee = ""
    gem = re.search(
        r"Grantee[^:]*[:\s]+(.*?)(?:Legal|Recording|Parcel|\n\n|$)", text, re.I | re.S
    )
    if gem:
        grantee = normalize(re.sub(r"\s+", " ", gem.group(1))[:80])

    legal = ""
    lm = re.search(r"(Parcel[:\s]+[\d-]+)", text, re.I)
    if lm:
        legal = lm.group(1)

    amount = None
    am = re.search(r"\$[\d,]+(?:\.\d{2})?", text)
    if am:
        amount = safe_float(am.group(0))

    clerk_url = RECORDER_BASE
    a = container.find("a", href=True)
    if a:
        clerk_url = urljoin(RECORDER_BASE, a["href"])
    elif doc_num:
        clerk_url = f"{RECORDER_BASE}document/{doc_num}"

    cat, cat_label = categorize(doc_type_raw or doc_type_hint)
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


def _parse_table_row(row, headers, doc_type_hint):
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

    i_doc     = ci("doc", "number", "instrument")
    i_type    = ci("type", "description")
    i_date    = ci("recording", "filed", "date")
    i_grantor = ci("grantor", "owner", "from")
    i_grantee = ci("grantee", "to")
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
    lc = cells[max(i_doc, 0)] if i_doc < len(cells) else cells[0]
    anc = lc.find("a", href=True)
    if anc:
        clerk_url = urljoin(RECORDER_BASE, anc["href"])

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
