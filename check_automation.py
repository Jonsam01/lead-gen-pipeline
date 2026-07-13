"""
Automation Signature Checker
------------------------------
Fetches a business's website HTML and checks it for known
automation-tool signatures (chat widgets, booking tools, CRM scripts).

If none are found, the business is flagged as a potential lead:
"has website, no automation detected."

Limitation: this only sees what's in the raw HTML response. Tools that
load dynamically via JavaScript after the page renders may be missed.
Good enough as a first-pass filter, not 100% authoritative.
"""

import json
import os
import re
import time
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials as GoogleCredentials
from dotenv import load_dotenv

load_dotenv()  # reads APIFY_API_TOKEN and APIFY_ACTOR_ID from .env

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR_ID = os.getenv("APIFY_ACTOR_ID")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

# Keywords in link text or URL path that suggest a contact/booking subpage
SUBPAGE_KEYWORDS = ["contact", "book", "booking", "schedule", "appointment"]

# Signature list: tool_name -> list of substrings to search for in raw HTML
SIGNATURES = {
    # Chat widgets
    "Intercom": ["widget.intercom.io", "intercom.io/widget"],
    "Drift": ["js.driftt.com"],
    "Tidio": ["code.tidio.co"],
    "Tawk.to": ["embed.tawk.to"],
    "Crisp": ["client.crisp.chat"],
    "LiveChat": ["cdn.livechatinc.com"],
    "Zendesk Chat": ["zdassets.com", "zopim.com"],

    # Chatbot / conversational builders
    "Typebot": ["typebot.io"],
    "Chatbot.com": ["chatbot.com"],

    # Booking / scheduling
    "Calendly": ["calendly.com"],
    "Cal.com": ["cal.com/embed", "cal.com/api"],
    "Acuity Scheduling": ["acuityscheduling.com"],
    "Setmore": ["setmore.com"],
    "SimplyBook.me": ["simplybook.me"],

    # CRM / marketing automation
    "HubSpot": ["js.hs-scripts.com", "hs-analytics.net", "hs-scripts.com"],
    "ActiveCampaign": ["trackcmp.net"],
    "Klaviyo": ["klaviyo.com"],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def check_site(url: str, timeout: int = 10, max_retries: int = 3, retry_delay: float = 1.5) -> dict:
    """
    Fetches the given URL and checks for automation signatures.
    Retries on connection/timeout errors up to max_retries times before
    giving up — this avoids marking a site "unreachable" just because of
    a transient network hiccup (which does happen, as we saw with
    Ultra Tune Geraldton failing once then succeeding on the next run).
    Returns a dict with the result.
    """
    result = {
        "url": url,
        "reachable": False,
        "status_code": None,
        "html_length": 0,
        "automation_found": [],
        "emails_found": [],
        "copyright_year": None,
        "error": None,
        "attempts": 0,
    }

    for attempt in range(1, max_retries + 1):
        result["attempts"] = attempt
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            result["reachable"] = True
            result["status_code"] = resp.status_code
            result["html_length"] = len(resp.text)
            result["error"] = None
            html = resp.text.lower()

            for tool_name, patterns in SIGNATURES.items():
                for pattern in patterns:
                    if pattern.lower() in html:
                        result["automation_found"].append(tool_name)
                        break  # no need to check other patterns for this tool

            result["emails_found"] = extract_emails(resp.text)  # original case, not lowercased
            result["copyright_year"] = find_copyright_year(resp.text)

            return result  # success — no need to retry further

        except requests.exceptions.RequestException as e:
            result["error"] = str(e)
            if attempt < max_retries:
                time.sleep(retry_delay)
            # otherwise fall through and return the failed result

    return result


def classify(result: dict) -> str:
    """Turns a check_site() result into a simple lead classification."""
    if not result["reachable"]:
        return "UNREACHABLE (request failed — treat as manual check needed)"
    if result["status_code"] != 200:
        return f"BLOCKED/ERROR (HTTP {result['status_code']}) — treat as manual check needed"
    if result.get("html_length", 0) < 200:
        return "SUSPICIOUSLY SHORT PAGE — likely blocked or empty, verify manually"
    if result["automation_found"]:
        return f"HAS AUTOMATION ({', '.join(result['automation_found'])}) — skip"
    return "NO AUTOMATION DETECTED — lead"


def find_relevant_subpage_links(base_url: str, html: str, max_links: int = 2) -> list:
    """
    Scans homepage HTML for links whose text or URL path suggests a
    contact/booking subpage (e.g. "Contact Us", "Book Now"). Returns up
    to max_links full URLs to check next.
    """
    soup = BeautifulSoup(html, "html.parser")
    found = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").lower()
        haystack = f"{href.lower()} {text}"

        if any(keyword in haystack for keyword in SUBPAGE_KEYWORDS):
            full_url = urljoin(base_url, href)
            if full_url not in seen and full_url != base_url:
                seen.add(full_url)
                found.append(full_url)
                if len(found) >= max_links:
                    break

    return found


EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9][a-zA-Z0-9._%+\-]*@[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}"
)


# Junk DOMAINS — if any of these appear anywhere in the email, it's junk
# (substring match is safe here since these are full domain names).
EMAIL_JUNK_DOMAINS = [
    "example.com", "example.org", "sentry.io", "wixpress.com",
    "godaddy.com", "domainsbyproxy.com", "2x.png", "yourdomain.com",
]

# Junk LOCAL PARTS (the part before the @) — matched EXACTLY, not as a
# substring, so we don't wrongly drop a real address like
# "poweruser@business.com.au" just because it contains "user".
EMAIL_JUNK_LOCAL_PARTS = [
    "noreply", "no-reply", "example", "test", "yourname", "youremail",
    "sample", "yourcompany", "placeholder", "user",
]


COPYRIGHT_YEAR_REGEX = re.compile(
    r"(?:copyright|©|&copy;|\(c\))\s*(?:[a-zA-Z\s]*)?(\d{4})", re.IGNORECASE
)


def find_copyright_year(html: str) -> int:
    """
    Looks for a 4-digit year near the word "copyright", a © symbol, or
    "(c)" in the page HTML — e.g. "© 2019 Graeme Hosken Autos". Returns
    the year found as an int, or None if no copyright notice was found.

    This is a soft signal, not proof: some sites genuinely omit a
    copyright year, and some update it via JavaScript (so this HTML-only
    check might miss the current year if that's the case). An old year
    found here is a hint the site hasn't been touched in a while — worth
    a manual glance, not a guaranteed fact about the business.
    """
    matches = COPYRIGHT_YEAR_REGEX.findall(html)
    if not matches:
        return None
    # If multiple years are found (e.g. "2019-2024"), take the earliest —
    # that's usually the "site built in" year, which is what signals staleness.
    years = [int(y) for y in matches if 1990 <= int(y) <= 2030]
    return min(years) if years else None


def extract_emails(html: str) -> list:
    """
    Extracts REAL, published email addresses from raw HTML — never
    guesses or invents one. Two sources, in order of reliability:

    1. mailto: links — the most deliberate signal; someone put this
       address there specifically so people could email them.
    2. Plain-text email patterns in the visible HTML (e.g. an email
       written out in a footer without a mailto link).

    Junk/placeholder addresses (tracking pixels, template defaults,
    "example@..." placeholders, no-reply addresses) are filtered out.
    Domain-based junk is matched as a substring (safe, since these are
    full domain names). Local-part junk (e.g. "example@") is matched
    EXACTLY against the part before the @ — not as a substring — so a
    real address like "poweruser@business.com.au" is never wrongly
    dropped just because it contains a junk-like word.

    Returns a deduplicated list, or an empty list if nothing genuine
    was found — it does NOT fall back to guessing a likely address.
    """
    found = set()

    for match in re.findall(r'mailto:([^"\'\s?&]+)', html, re.IGNORECASE):
        # Don't trust the raw mailto: capture verbatim — some sites have
        # unfilled template placeholders (e.g. "mailto:{{:email}}") or
        # malformed links (e.g. "mailto://name@domain.com" with a stray
        # "//"). Validate that a real email-shaped string actually
        # exists inside the capture before accepting it.
        valid = EMAIL_REGEX.search(match)
        if valid:
            found.add(valid.group(0).strip().lower())

    for match in EMAIL_REGEX.findall(html):
        found.add(match.strip().lower())

    real_emails = []
    for email in found:
        if any(domain in email for domain in EMAIL_JUNK_DOMAINS):
            continue
        local_part = email.split("@")[0] if "@" in email else email
        if local_part in EMAIL_JUNK_LOCAL_PARTS:
            continue
        real_emails.append(email)

    return sorted(real_emails)


def check_site_thoroughly(url: str, timeout: int = 10) -> dict:
    """
    Checks the homepage, then also finds and checks up to 2 contact/booking
    subpages (e.g. "Contact Us", "Book Now"). Combines automation findings
    AND any real published emails found across all pages checked. This
    catches widgets — and emails — that only appear on a subpage rather
    than the homepage itself.
    """
    homepage_result = check_site(url, timeout=timeout)
    pages_checked = [url]
    all_automation_found = list(homepage_result["automation_found"])
    all_emails_found = list(homepage_result.get("emails_found", []))

    if homepage_result["reachable"] and homepage_result["status_code"] == 200:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            subpage_links = find_relevant_subpage_links(url, resp.text)

            for subpage_url in subpage_links:
                sub_result = check_site(subpage_url, timeout=timeout)
                pages_checked.append(subpage_url)
                for tool in sub_result["automation_found"]:
                    if tool not in all_automation_found:
                        all_automation_found.append(tool)
                for email in sub_result.get("emails_found", []):
                    if email not in all_emails_found:
                        all_emails_found.append(email)
        except requests.exceptions.RequestException:
            pass  # if subpage discovery fails, we still have the homepage result

    combined = dict(homepage_result)
    combined["automation_found"] = all_automation_found
    combined["emails_found"] = all_emails_found
    combined["pages_checked"] = pages_checked
    return combined


def run_apify_search(search_term: str, location: str, max_places: int = 10) -> list:
    """
    Triggers a NEW Apify Google Maps Extractor run with the given search
    term and location, waits for it to finish, and returns the results
    directly. This is the fully automated path — no need to visit the
    Apify website at all.

    Note: this call blocks (waits) until the Apify run completes, which
    matched our test runs taking about 40-50 seconds. If a run takes much
    longer than that, this could take a while to return — that's normal,
    it's just waiting on Apify's servers.
    """
    if not APIFY_API_TOKEN or not APIFY_ACTOR_ID:
        raise RuntimeError(
            "Missing APIFY_API_TOKEN or APIFY_ACTOR_ID. "
            "Make sure your .env file is set up (see .env.example)."
        )

    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    params = {"token": APIFY_API_TOKEN}

    # Same input fields as the ones we set manually in the Apify Console form
    payload = {
        "searchStringsArray": [search_term],
        "locationQuery": location,
        "maxCrawledPlacesPerSearch": max_places,
        "website": "withWebsite",  # matches "Scrape only places with a website"
        "language": "en",
    }

    resp = requests.post(url, params=params, json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()


def fetch_leads_from_apify_api() -> list:
    """
    Fetches results from the most recent SUCCEEDED run of the Apify actor,
    using the API token and actor ID stored in .env. No manual export needed.
    Returns the raw list of records (same shape as a JSON export would give).
    """
    if not APIFY_API_TOKEN or not APIFY_ACTOR_ID:
        raise RuntimeError(
            "Missing APIFY_API_TOKEN or APIFY_ACTOR_ID. "
            "Make sure your .env file is set up (see .env.example)."
        )

    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/runs/last/dataset/items"
    params = {"token": APIFY_API_TOKEN, "status": "SUCCEEDED"}

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()  # raises an error if the request failed
    return resp.json()


def _normalize_website(website: str) -> str:
    """
    Normalizes a website URL for dedup comparison — strips protocol,
    'www.', trailing slashes, and query strings, so
    'https://www.example.com.au/?utm_source=google' and
    'http://example.com.au' are recognized as the same site.
    """
    url = website.lower().strip()
    url = url.split("?")[0]  # drop query string
    url = url.replace("https://", "").replace("http://", "")
    url = url.replace("www.", "")
    url = url.rstrip("/")
    return url


def _parse_apify_records(records: list) -> list:
    """
    Shared parsing logic: pulls out the fields we need from raw Apify
    records (name, website, phone, place_id, maps_url). Skips records
    with no website, or where "website" is really just a Facebook/
    Instagram link (not a real site with a homepage to scan).

    Also deduplicates within this run — Apify can occasionally return
    the same business twice (e.g. two Maps listings for one business,
    or overlap between search terms). This is separate from the
    cross-run Supabase dedup — this just prevents processing/printing
    the same business twice within a single run.
    """
    leads = []
    seen_websites = set()

    for r in records:
        website = r.get("website")
        if not website:
            continue

        if "facebook.com" in website.lower() or "instagram.com" in website.lower():
            continue

        normalized = _normalize_website(website)
        if normalized in seen_websites:
            continue
        seen_websites.add(normalized)

        leads.append({
            "name": r.get("title", "Unknown"),
            "website": website,
            "phone": r.get("phone", ""),
            "place_id": r.get("placeId", ""),
            "maps_url": r.get("url", ""),
            "rating": r.get("totalScore"),
            "reviews_count": r.get("reviewsCount"),
        })

    return leads


def load_leads_from_apify_export(json_path: str) -> list:
    """
    Loads an Apify dataset export (JSON file) and parses it into leads.
    Fallback for when you don't want to use the live API (e.g. testing
    against an old export, or offline).
    """
    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)
    return _parse_apify_records(records)


def load_leads_from_apify_live() -> list:
    """
    Fetches the latest run's results directly from Apify's API and
    parses them into leads. This is the production path — no manual
    export/download step needed.
    """
    records = fetch_leads_from_apify_api()
    return _parse_apify_records(records)


def _supabase_headers() -> dict:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_SERVICE_KEY. "
            "Make sure your .env file is set up (see .env.example)."
        )
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def is_lead_already_processed(place_id: str) -> bool:
    """
    Checks the processed_leads table for this place_id. Returns True if
    we've already handled this business in a past run — meaning we
    should skip it this time rather than re-scrape/re-check it.
    If place_id is missing/empty, treats it as NOT processed (better to
    risk a duplicate than silently skip a lead we can't identify).
    """
    if not place_id:
        return False

    url = f"{SUPABASE_URL}/rest/v1/processed_leads"
    params = {"place_id": f"eq.{place_id}", "select": "place_id"}

    resp = requests.get(url, headers=_supabase_headers(), params=params, timeout=15)
    resp.raise_for_status()
    return len(resp.json()) > 0


def mark_lead_processed(place_id: str, name: str, website: str, qualified: bool) -> None:
    """
    Records this business as processed in Supabase, so future runs
    skip it. Uses upsert so re-running against the same place_id never
    errors on a duplicate primary key.
    """
    if not place_id:
        return  # can't reliably dedupe without a place_id, so nothing to record

    url = f"{SUPABASE_URL}/rest/v1/processed_leads"
    headers = _supabase_headers()
    headers["Prefer"] = "resolution=merge-duplicates"  # upsert behavior

    payload = {
        "place_id": place_id,
        "business_name": name,
        "website": website,
        "qualified": qualified,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()


def format_lead_message(lead: dict) -> str:
    """
    Formats a qualified lead into a Telegram message using everything
    we've collected: name, website, phone, email(s), review data,
    copyright staleness signal, and a direct link to the Google Maps
    listing (via place_id, which — as we confirmed earlier tonight —
    lands on the full business profile, not just a bare pin).
    """
    lines = [f"🆕 New Lead: {lead['name']}"]
    lines.append(f"🌐 Website: {lead['website']}")

    if lead.get("phone"):
        lines.append(f"📞 Phone: {lead['phone']}")

    emails = lead.get("emails") or []
    if emails:
        lines.append(f"✉️ Email: {', '.join(emails)}")
    else:
        lines.append("✉️ Email: not found — manual lookup needed")

    if lead.get("reviews_count") is not None:
        lines.append(f"⭐ Reviews: {lead['reviews_count']} ({lead.get('rating', 'N/A')}★)")

    copyright_year = lead.get("copyright_year")
    if copyright_year:
        current_year = time.localtime().tm_year
        age = current_year - copyright_year
        if age >= 2:
            lines.append(f"🕰️ Site footer shows © {copyright_year} ({age} years old)")

    place_id = lead.get("place_id")
    if place_id:
        maps_link = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
        lines.append(f"📍 Listing: {maps_link}")

    return "\n".join(lines)


_sheet_connection_cache = None  # cached so we authenticate only once per script run


def _get_sheet():
    """
    Connects to the configured Google Sheet using the service account
    credentials. Cached after the first call so we don't re-authenticate
    for every single lead — only once per script run.
    """
    global _sheet_connection_cache
    if _sheet_connection_cache is not None:
        return _sheet_connection_cache

    if not GOOGLE_SHEET_ID:
        raise RuntimeError(
            "Missing GOOGLE_SHEET_ID. Make sure your .env file is set up "
            "(see .env.example)."
        )
    if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
        raise RuntimeError(
            f"Missing service account file: {GOOGLE_SERVICE_ACCOUNT_FILE}. "
            "Download it from Google Cloud Console and place it in this folder."
        )

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = GoogleCredentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

    _sheet_connection_cache = sheet
    return sheet


def append_lead_to_sheet(lead: dict) -> bool:
    """
    Appends a new row to the Google Sheet for this qualified lead —
    the dedicated email-campaign staging list, separate from Supabase
    (which just tracks "have we seen this business before").
    Column order: Name | Website | Phone | Email | Reviews | Rating |
    Copyright Year | Maps Link | Date Added
    Returns True on success, False on failure (printed, not fatal —
    same pattern as the other integrations tonight).
    """
    try:
        sheet = _get_sheet()

        emails = lead.get("emails") or []
        email_str = ", ".join(emails) if emails else ""

        place_id = lead.get("place_id", "")
        maps_link = f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else ""

        row = [
            lead.get("name", ""),
            lead.get("website", ""),
            lead.get("phone", ""),
            email_str,
            lead.get("reviews_count", ""),
            lead.get("rating", ""),
            lead.get("copyright_year", ""),
            maps_link,
            time.strftime("%Y-%m-%d"),
        ]

        sheet.append_row(row)
        return True
    except Exception as e:
        print(f"  [Google Sheets append failed: {e}]")
        return False


def send_telegram_message(text: str, max_retries: int = 3, retry_delay: float = 2.0) -> bool:
    """
    Sends a message to the configured Telegram chat. Retries on
    connection/timeout errors up to max_retries times before giving up —
    same pattern as check_site's retry logic, since we saw a real
    transient timeout on a Telegram send (Cassowary Coast Veterinary
    Services) during testing. Returns True on success, False if all
    attempts fail (and prints the error rather than crashing the whole
    run — a failed notification for one lead shouldn't stop the rest
    of the pipeline).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram not sent — missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env]")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            if attempt > 1:
                print(f"  [Telegram send succeeded on attempt {attempt}]")
            return True
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                print(f"  [Telegram send failed after {max_retries} attempts: {e}]")
                return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        # Usage: python check_automation.py "auto repair shop" "Australia" [max_places]
        search_term = sys.argv[1]
        location = sys.argv[2]
        max_places = int(sys.argv[3]) if len(sys.argv) > 3 else 10

        print(f"Running a NEW Apify search: '{search_term}' in '{location}' "
              f"(up to {max_places} places)...")
        print("This triggers a live scrape — it will take about 30-60 seconds.\n")
        raw_records = run_apify_search(search_term, location, max_places)
        leads = _parse_apify_records(raw_records)
    else:
        # No arguments given — fall back to whatever the last run was
        print("No search term/location given — using results from your most "
              "recent Apify run instead.")
        print('(To run a fresh search: python check_automation.py "search term" "location")\n')
        leads = load_leads_from_apify_live()

    print(f"Loaded {len(leads)} leads with real websites (Facebook/Instagram-only excluded)\n")
    print(f"{'Business':<35} | {'Result'}")
    print("-" * 110)

    qualified_leads = []
    skipped_count = 0

    for lead in leads:
        if is_lead_already_processed(lead.get("place_id", "")):
            skipped_count += 1
            print(f"{lead['name'][:33]:<35} | ALREADY PROCESSED (seen in a past run) — skipped")
            continue

        result = check_site_thoroughly(lead["website"])
        classification = classify(result)
        print(f"{lead['name'][:33]:<35} | {classification}")
        pages = result.get("pages_checked", [lead["website"]])
        if len(pages) > 1:
            print(f"{'':<35} |   Checked {len(pages)} pages (homepage + {len(pages)-1} subpage(s))")
        if result.get("attempts", 1) > 1:
            print(f"{'':<35} |   Needed {result['attempts']} attempts (retried after failure)")
        if result["error"]:
            print(f"{'':<35} |   Error: {result['error']}")

        emails = result.get("emails_found", [])
        lead["emails"] = emails  # attach to lead regardless of classification
        if emails:
            print(f"{'':<35} |   Email(s) found: {', '.join(emails)}")
        else:
            print(f"{'':<35} |   No published email found — manual lookup needed")

        copyright_year = result.get("copyright_year")
        lead["copyright_year"] = copyright_year
        if copyright_year:
            current_year = time.localtime().tm_year
            age = current_year - copyright_year
            if age >= 2:
                print(f"{'':<35} |   Site footer shows © {copyright_year} "
                      f"({age} years old — possible staleness signal)")
            else:
                print(f"{'':<35} |   Site footer shows © {copyright_year} (recently updated)")
        else:
            print(f"{'':<35} |   No copyright year found on page")

        if lead.get("reviews_count") is not None:
            print(f"{'':<35} |   Reviews: {lead['reviews_count']} "
                  f"(rating {lead.get('rating', 'N/A')})")

        if classification == "NO AUTOMATION DETECTED — lead":
            qualified_leads.append(lead)
            message = format_lead_message(lead)
            sent = send_telegram_message(message)
            if sent:
                print(f"{'':<35} |   Sent to Telegram ✓")
            else:
                print(f"{'':<35} |   NOT marking as processed — will retry next run "
                      f"since you were never notified")

            sheet_ok = append_lead_to_sheet(lead)
            if sheet_ok:
                print(f"{'':<35} |   Added to Google Sheet ✓")
            else:
                print(f"{'':<35} |   NOT marking as processed — will retry next run "
                      f"since it wasn't added to the campaign sheet")

        # Skip marking as processed if this was a qualified lead where
        # EITHER the Telegram notification or the Sheet append failed —
        # otherwise it would be silently skipped forever on future runs
        # despite never actually reaching you or your campaign queue.
        if classification == "NO AUTOMATION DETECTED — lead" and not (sent and sheet_ok):
            continue

        mark_lead_processed(
            place_id=lead.get("place_id", ""),
            name=lead["name"],
            website=lead["website"],
            qualified=(classification == "NO AUTOMATION DETECTED — lead"),
        )

    if skipped_count:
        print(f"\n({skipped_count} lead(s) skipped — already processed in a past run)")

    print(f"\n{len(qualified_leads)} qualified leads (has website, no automation detected):")
    for lead in qualified_leads:
        email_str = ", ".join(lead["emails"]) if lead["emails"] else "no email found"
        year_str = f"© {lead['copyright_year']}" if lead.get("copyright_year") else "no copyright year"
        reviews_str = (f"{lead['reviews_count']} reviews, {lead.get('rating', 'N/A')}★"
                        if lead.get("reviews_count") is not None else "no review data")
        print(f"  - {lead['name']} | {lead['website']} | {lead['phone']} | {email_str} | {year_str} | {reviews_str}")
