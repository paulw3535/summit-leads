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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_float(v: Any) -> Optional[float]:
    try:
        cleaned = re.sub(r"[^\d.]", "", str(v))
        return float
