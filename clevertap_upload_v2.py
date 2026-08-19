import requests
import time
import os
import re
import json
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# =====================================================
# 1. CONFIGURATION
# =====================================================
MB_BASE_URL = "https://mb.givadiva.co"
MB_USERNAME = "pramatheshray.ray@giva.co"
MB_PASSWORD = os.getenv("MB_PASSWORD")  # Set in repo Settings > Secrets and variables > Actions
MB_CARD_ID = 23051

DISCOVERY_CARD_ID = 23081  # cohort_v2_combo_discovery.sql, saved as a Metabase question

# "manual" -> uses FILTERS_TO_PROCESS below, you fill in specific values,
#             same as before.
# "auto"   -> discovers every real (tactic, discount) pair that exists in
#             cohort_mapping_v2 (via DISCOVERY_CARD_ID) and runs one segment
#             per pair automatically -- no typing needed. NA/NA is already
#             excluded by the discovery query itself.
MODE = "auto"

# Only used in "auto" mode. Which day to run is now DYNAMIC, not a static
# list -- see get_target_day() below. Two ways to control it:
#   - Scheduled runs (the GitHub Actions cron): no input, so this always
#     resolves to whatever day it actually is in IST when the job fires.
#   - Manual runs (workflow_dispatch): the "day" input, if filled in,
#     overrides the actual day -- read via the OVERRIDE_DAY env var.
# There's a second, independent override for WHICH cohorts run on that day
# (all discovered ones by default, or a specific list) -- see
# get_cohort_override() and OVERRIDE_COHORTS below.

# Automatically grab today's date
# Automatically grab today's date in IST, not UTC
START_DATE = datetime.now(IST).strftime("%Y-%m-%d")  # Metabase format
CT_DATE = datetime.now(IST).strftime("%d%b%y")        # CleverTap format

CT_ACCOUNT_ID = "R78-Z5K-847Z"
CT_PASSCODE = os.getenv("CT_PASSCODE")  # Set in repo Settings > Secrets and variables > Actions
CT_REGION = "in1"
CT_ADMIN_EMAIL = "shah.neil@giva.co"
CT_CREATOR_NAME = "Pramathesh Ray"
CT_REPLACE_EXISTING = False

# Promo coin tracking: phone + Shopify ID for every customer added to a
# CleverTap segment this run, one sheet per segment, emailed as one
# workbook after the whole run finishes. Separate from CT_ADMIN_EMAIL
# (that's CleverTap segment-creation attribution, not an inbox).
GMAIL_SENDER = "pramatheshray.ray@giva.co"
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")  # Set in repo Settings > Secrets and variables > Actions
# PROMO_COIN_RECIPIENTS = ["soumya.jain@giva.co", "preeti.chougale@giva.co"]
PROMO_COIN_RECIPIENTS = ["pramatheshray.ray@giva.co"]
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# Maps the lowercase BigQuery column name (matches cohort_mapping_v2 and the
# query's own WHERE clause) to the Metabase template-tag name used in the
# SQL's {{ }} placeholders. Metabase's API needs the template-tag name, not
# the BigQuery column name -- this is what makes that translation invisible
# below, so FILTERS_TO_PROCESS can just use the column names directly.
TEMPLATE_TAG_MAP = {
    "monday_p1_p2_p3":    "Monday_P1_P2_P3",
    "monday_discount":    "Monday_Discount",
    "tuesday_p1_p2_p3":   "Tuesday_P1_P2_P3",
    "tuesday_discount":   "Tuesday_Discount",
    "wednesday_p1_p2_p3": "Wednesday_P1_P2_P3",
    "wednesday_discount": "Wednesday_Discount",
    "thursday_p1_p2_p3":  "Thursday_P1_P2_P3",
    "thursday_discount":  "Thursday_Discount",
    "friday_p1_p2_p3":    "Friday_P1_P2_P3",
    "friday_discount":    "Friday_Discount",
    "saturday_p1_p2_p3":  "Saturday_P1_P2_P3",
    "saturday_discount":  "Saturday_Discount",
    "sunday_p1_p2_p3":    "Sunday_P1_P2_P3",
    "sunday_discount":    "Sunday_Discount",
    "oc_status":          "OC_status",
}

# Tactics that get split into OC1 / OC2 / rest (OC3+) as three separate
# CleverTap segments instead of one undivided one. Every other tactic is
# unaffected -- runs exactly as before, single segment, no OC_status filter
# applied at all.
#
# "Online to Offline - Silver" was listed twice when this was requested;
# treated as one tactic here, not two -- there's nothing to distinguish a
# second entry from the first.
SPLIT_BY_OC_TACTICS = {
    "Online to Offline - Gold",
    "Online to Offline - Silver",
    "Next Repeat Order Any - Online",
    "Offline to Online - App New",
    "Offline to Online - App Repeat",
    "Next Repeat Order Any - Offline Gold",
    "Next Repeat Order Any - Offline Silver",
}

# (oc_bucket_label, oc_status values to pass in the OC_status filter)
# "Unknown" folded into the OC3Plus/"rest" bucket -- lifetime_orders should
# never actually be 0/NULL for anyone in the base population this query
# draws from, but including it costs nothing and closes the gap if that
# assumption is ever wrong.
OC_BUCKETS = [
    ("OC1", ["OC1"]),
    ("OC2", ["OC2"]),
    ("OC3Plus", ["OC3", "OC4", "OC4+", "Unknown"]),
]

# =====================================================
# FILTERS TO PROCESS
# =====================================================
# One slot per DAY, not per column. Fill in p1_p2_p3 and/or discount for
# whichever day you're running -- if you fill in BOTH for the same day, they
# combine into ONE segment filtered by both together (AND), same as filling
# in both boxes in the Metabase query at once. Fill in just one of the two
# and only that filter applies. Leave both blank and that day is skipped
# entirely -- no query is run for it.
#
# A value can be a single string or a list of strings (multi-select, same as
# the query's IN() filters), e.g. "p1_p2_p3": ["Next New Order App", "Offline New"].
#
# Need two SEPARATE segments for the same day (e.g. two different Friday
# tactics run independently rather than combined)? List "friday" twice with
# different values -- each entry is processed on its own regardless of
# whether the day name repeats.
FILTERS_TO_PROCESS = [
    {"day": "monday",    "p1_p2_p3": "", "discount": ""},
    {"day": "tuesday",   "p1_p2_p3": "", "discount": ""},
    {"day": "wednesday", "p1_p2_p3": "", "discount": ""},
    {"day": "thursday",  "p1_p2_p3": "", "discount": ""},
    {"day": "friday",    "p1_p2_p3": "", "discount": ""},
    {"day": "saturday",  "p1_p2_p3": "", "discount": ""},
    {"day": "sunday",    "p1_p2_p3": "", "discount": ""},
    # Optional: add "label": "..." to any entry above to override the
    # auto-built segment/file name, e.g.:
    # {"day": "friday", "p1_p2_p3": "...", "discount": "...", "label": "Friday_Push"},
]

VALID_DAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
}

# Tactics (P1_P2_P3 values) that need to be split into separate OC1 / OC2 /
# Rest CleverTap segments instead of one segment for the whole cohort.
# Applies regardless of which day or discount the tactic shows up under --
# matched against the tactic VALUE, not any particular filter combination.
# Requires OC_Status to be a real output column in the SQL (uncommented,
# not the auditing-only commented-out version).
OC_SPLIT_TACTICS = {
    "Online to Offline - Gold",
    "Online to Offline - Silver",
    "Next Repeat Order Any - Online",
    "Offline to Online - App New",
    "Offline to Online - App Repeat",
    "Next Repeat Order Any - Offline Gold",
    "Next Repeat Order Any - Offline Silver",
}

# "Rest" = every OC_Status that isn't OC1 or OC2, collapsed into one bucket
# (OC3, OC4, OC4+, Unknown -- not split further).
OC_SPLIT_BUCKETS = ["OC1", "OC2"]  # checked in order; anything left over is "Rest"


# =====================================================
# 2. CORE FUNCTIONS
# =====================================================
def mb_authenticate():
    resp = requests.post(
        f"{MB_BASE_URL}/api/session",
        json={"username": MB_USERNAME, "password": MB_PASSWORD},
        timeout=60,
    )
    resp.raise_for_status()
    return {"X-Metabase-Session": resp.json()["id"]}


def build_day_filters(entry):
    """Turns one {"day": ..., "p1_p2_p3": ..., "discount": ...} entry into
    (day, filters_dict), including only the fields that were actually
    filled in. Both filled in -> both keys present -> combined AND when
    passed to fetch_metabase_csv. Neither filled in -> empty dict, caller
    skips this day."""
    day = str(entry.get("day", "")).strip().lower()
    if day not in VALID_DAYS:
        raise ValueError(
            f"Entry has missing/invalid 'day': {entry!r}. "
            f"Must be one of: {', '.join(sorted(VALID_DAYS))}"
        )

    filters = {}
    tactic = entry.get("p1_p2_p3")
    if tactic not in (None, "", []):
        filters[f"{day}_p1_p2_p3"] = tactic

    discount = entry.get("discount")
    if discount not in (None, "", []):
        filters[f"{day}_discount"] = discount

    return day, filters


def build_label(day, filters, entry):
    """Uses entry['label'] if given; otherwise Day + the filter values
    joined together, e.g. Friday_Online_to_Offline_-_Silver_1500_Promo_coins."""
    if entry.get("label"):
        return entry["label"]

    parts = [day.capitalize()]
    for value in filters.values():
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        else:
            parts.append(str(value))
    return "_".join(parts)


def sanitize_for_filename(text):
    """Segment names can keep spaces/punctuation (CleverTap just displays
    them), but a temp file on disk can't safely contain '/', so this is only
    used for the local CSV path, not the CleverTap segment name itself."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def fetch_metabase_csv(headers, filters, output_file):
    """filters: dict of {bigquery_column_name: value_or_list_of_values}.
    Only the columns present here get filtered -- everything else in the
    query's 14 optional filters stays blank/skipped, same as leaving a box
    empty in the Metabase UI.

    Fetches via /query/json rather than /query/csv, then writes the CSV
    ourselves via pandas. Metabase's own CSV export doesn't reliably quote
    field values containing a raw comma or newline -- and this query's
    output always carries all 14 day-tactic/discount columns for every
    matched customer regardless of which day is being filtered, so even a
    Thursday-targeted fetch can be corrupted by a comma buried in some
    customer's Tuesday label. That misaligns field boundaries partway
    through large files and crashes pandas' downstream read_csv with an
    "Expected N fields, saw M" error. JSON has no such ambiguity -- string
    values are explicitly delimited regardless of content -- so converting
    to CSV ourselves sidesteps the failure mode rather than working around
    one bad row (which would mean silently dropping real customers).

    Trade-off: the full response is now held in memory before writing,
    rather than streamed straight to disk. For row/column counts seen so
    far (hundreds of thousands of rows, ~20 columns) this is still modest,
    comfortably within a GitHub Actions runner's limits -- worth
    re-checking if a cohort ever grows dramatically larger."""
    parameters = [
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", "Start_date"]],
            "value": START_DATE,
        }
    ]

    for column, value in filters.items():
        if column not in TEMPLATE_TAG_MAP:
            raise ValueError(
                f"Unknown filter column '{column}'. Must be one of: "
                f"{', '.join(TEMPLATE_TAG_MAP.keys())}"
            )
        tag = TEMPLATE_TAG_MAP[column]
        values = value if isinstance(value, list) else [value]
        parameters.append({
            "type": "category",
            "target": ["variable", ["template-tag", tag]],
            "value": values,
        })

    url = f"{MB_BASE_URL}/api/card/{MB_CARD_ID}/query/json"
    resp = requests.post(
        url, headers=headers, json={"parameters": parameters},
        timeout=900,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Query failed ({resp.status_code}): {resp.text[:500]}")

    rows = resp.json()

    if not rows:
        # 0 rows matched. A bare pd.DataFrame([]).to_csv() would write a
        # genuinely empty (zero-byte) file, which pd.read_csv can't parse
        # at all (raises EmptyDataError) rather than cleanly reporting 0
        # rows downstream -- so write a minimal valid header instead.
        with open(output_file, "w") as f:
            f.write("Customer_Phone\n")
        return

    df = pd.DataFrame(rows)
    df.to_csv(output_file, index=False)


def fetch_combinations(headers):
    """Calls the discovery question (DISCOVERY_CARD_ID) and returns every
    real (day, tactic, discount) row it finds -- no parameters needed,
    cohort_mapping_v2 is a static table so this returns the same thing
    every time until the table itself is rebuilt. NA/NA is already excluded
    by the discovery SQL itself, not filtered here."""
    if DISCOVERY_CARD_ID is None:
        raise ValueError(
            "DISCOVERY_CARD_ID is not set. Save cohort_v2_combo_discovery.sql "
            "as a Metabase question first, then fill in its card ID at the "
            "top of this file."
        )

    url = f"{MB_BASE_URL}/api/card/{DISCOVERY_CARD_ID}/query/json"
    resp = requests.post(url, headers=headers, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Discovery query failed ({resp.status_code}): {resp.text[:500]}")

    rows = resp.json()
    combos = [
        {"day": r["day"], "p1_p2_p3": r["p1_p2_p3"], "discount": r["discount"]}
        for r in rows
    ]
    return combos


def get_target_day():
    """OVERRIDE_DAY env var wins if set (from workflow_dispatch's "day"
    input); otherwise today's actual day-of-week in IST. Scheduled runs
    never set OVERRIDE_DAY, so they always fall through to "today"."""
    override = os.getenv("OVERRIDE_DAY", "").strip().lower()
    if override:
        if override not in VALID_DAYS:
            raise ValueError(
                f"OVERRIDE_DAY={override!r} is not a valid day. "
                f"Must be one of: {', '.join(sorted(VALID_DAYS))}"
            )
        return override
    return datetime.now(IST).strftime("%A").lower()


def get_cohort_override():
    """OVERRIDE_COHORTS env var (from workflow_dispatch's "cohorts" input).

    GitHub's manual-trigger form renders string inputs as a single-line box
    -- pasting multi-line text into it collapses everything onto one line,
    so newline-separated entries don't survive that UI. Cohorts are
    therefore semicolon-separated instead, all on one line:
        tactic A | discount A; tactic B | discount B; tactic C | discount C
    Newlines are ALSO accepted as a separator (split on ';' or '\n'), so
    this still works unchanged if triggered another way where a real
    newline does survive (the GitHub API/CLI, for instance).

    Returns None if unset/blank -- the caller should fall back to full
    auto-discovery in that case. Blank entries are skipped; a malformed
    entry (missing the "|") raises rather than silently dropping a cohort
    you thought you'd included."""
    raw = os.getenv("OVERRIDE_COHORTS", "").strip()
    if not raw:
        return None

    entries = re.split(r"[;\n]", raw)

    combos = []
    for entry_num, entry in enumerate(entries, start=1):
        entry = entry.strip()
        if not entry:
            continue
        if "|" not in entry:
            raise ValueError(
                f"OVERRIDE_COHORTS entry {entry_num} is missing '|': {entry!r}. "
                f"Expected format: tactic | discount"
            )
        tactic, discount = entry.split("|", 1)
        combos.append({"p1_p2_p3": tactic.strip(), "discount": discount.strip()})

    if not combos:
        return None  # e.g. input was whitespace-only

    return combos


def split_by_oc_status(csv_path):
    """Splits a fetched CSV into up to 5 sub-CSVs based on OC_Status and ARPU_Tier:
    OC1 High ARPU, OC1 Low/Medium ARPU, OC2 High ARPU, OC2 Low/Medium ARPU, and Rest."""
    df = pd.read_csv(csv_path)

    # Safety check: ensure both columns exist in the downloaded CSV
    if "OC_Status" not in df.columns or "ARPU_Tier" not in df.columns:
        raise ValueError(
            "split_by_oc_status called but the fetched CSV is missing OC_Status or ARPU_Tier "
            "-- confirm BOTH are uncommented in the SQL's SELECT list."
        )

    base_path = csv_path[:-4] if csv_path.endswith(".csv") else csv_path
    buckets = []

    # Define our boolean masks for slicing the pandas DataFrame
    is_oc1 = df["OC_Status"] == "OC1"
    is_oc2 = df["OC_Status"] == "OC2"
    is_high_arpu = df["ARPU_Tier"] == "High"
    is_low_med_arpu = df["ARPU_Tier"].isin(["Low", "Medium"])

    # 1. OC1 High ARPU
    mask_oc1_high = is_oc1 & is_high_arpu
    if mask_oc1_high.any():
        sub_path = f"{base_path}_OC1_High_ARPU.csv"
        df[mask_oc1_high].to_csv(sub_path, index=False)
        buckets.append(("OC1_High_ARPU", sub_path))

    # 2. OC1 Low/Medium ARPU
    mask_oc1_low_med = is_oc1 & is_low_med_arpu
    if mask_oc1_low_med.any():
        sub_path = f"{base_path}_OC1_Low_Medium_ARPU.csv"
        df[mask_oc1_low_med].to_csv(sub_path, index=False)
        buckets.append(("OC1_Low_Medium_ARPU", sub_path))

    # 3. OC2 High ARPU
    mask_oc2_high = is_oc2 & is_high_arpu
    if mask_oc2_high.any():
        sub_path = f"{base_path}_OC2_High_ARPU.csv"
        df[mask_oc2_high].to_csv(sub_path, index=False)
        buckets.append(("OC2_High_ARPU", sub_path))

    # 4. OC2 Low/Medium ARPU
    mask_oc2_low_med = is_oc2 & is_low_med_arpu
    if mask_oc2_low_med.any():
        sub_path = f"{base_path}_OC2_Low_Medium_ARPU.csv"
        df[mask_oc2_low_med].to_csv(sub_path, index=False)
        buckets.append(("OC2_Low_Medium_ARPU", sub_path))

    # 5. Rest (Catches OC3+, Unknown, and any weird edge cases)
    mask_rest = ~(mask_oc1_high | mask_oc1_low_med | mask_oc2_high | mask_oc2_low_med)
    if mask_rest.any():
        sub_path = f"{base_path}_Rest.csv"
        df[mask_rest].to_csv(sub_path, index=False)
        buckets.append(("Rest", sub_path))

    return buckets


def capture_promo_coin_data(csv_path):
    """Reads Customer_Phone + Shopify_ID from a freshly fetched CSV, BEFORE
    transform_csv_for_clevertap runs on it -- that function overwrites the
    same file path with just type/identity columns, which would destroy
    this data if called first. Returns None (not an exception) if the
    columns are missing or there's nothing usable, since a problem in this
    side feature shouldn't take down the actual CleverTap upload it's
    piggybacking on."""
    try:
        df = pd.read_csv(csv_path)
        if "Customer_Phone" not in df.columns or "Shopify_ID" not in df.columns:
            print("    ⚠️ Customer_Phone/Shopify_ID missing -- skipping promo coin capture for this segment.")
            return None
        out = df[["Customer_Phone", "Shopify_ID"]].dropna(subset=["Customer_Phone"]).copy()
        return out if not out.empty else None
    except Exception as e:
        print(f"    ⚠️ Could not capture promo coin data: {e}")
        return None


def write_promo_coin_workbook(segment_data, output_path):
    """segment_data: dict of {segment_name: DataFrame(Customer_Phone,
    Shopify_ID)}. Sheet names are short and purely sequential (Segment_01,
    Segment_02, ...) rather than truncated segment names -- truncating a
    long name to Excel's 31-character limit tends to cut off exactly the
    part that distinguishes one bucket from another (e.g. three sheets from
    the same split cohort would all truncate to the same shared prefix,
    with the OC1/OC2/Rest suffix that actually distinguishes them cut off).
    A first "Index" sheet lists each Segment_NN against its real, full,
    untruncated segment name and row count, so the workbook stays readable
    without needing to guess from a chopped-off sheet name."""
    index_rows = []
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for i, (segment_name, df) in enumerate(segment_data.items(), start=1):
            sheet_name = f"Segment_{i:02d}"
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            index_rows.append({
                "Sheet": sheet_name,
                "Segment_Name": segment_name,
                "Row_Count": len(df),
            })

        # Index written last so it lands as the LAST sheet in file order,
        # but re-ordered to the FRONT below so it's the first thing anyone
        # sees when they open the workbook.
        index_df = pd.DataFrame(index_rows)
        index_df.to_excel(writer, sheet_name="Index", index=False)
        writer.book.move_sheet("Index", offset=-len(segment_data))


def send_promo_coin_email(attachment_path, segment_count):
    """Sends the finished workbook via Gmail/Google Workspace SMTP with an
    App Password. Raises on failure rather than silently swallowing it --
    a failed send should be visible in the run's logs, not just missing
    from someone's inbox with no explanation."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders

    if not GMAIL_APP_PASSWORD:
        raise ValueError(
            "GMAIL_APP_PASSWORD is not set. Generate an App Password for "
            f"{GMAIL_SENDER} in its Google Account security settings, then "
            "add it as a GitHub Secret named GMAIL_APP_PASSWORD."
        )

    msg = MIMEMultipart()
    msg["From"] = GMAIL_SENDER
    msg["To"] = ", ".join(PROMO_COIN_RECIPIENTS)
    msg["Subject"] = f"Promo Coin Eligible Customers -- {CT_DATE}"

    body = (
        f"Attached: phone number + Shopify ID for every customer added to a "
        f"CleverTap segment in today's run ({CT_DATE}), one sheet per "
        f"segment ({segment_count} sheet(s) total)."
    )
    msg.attach(MIMEText(body, "plain"))

    with open(attachment_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f"attachment; filename={os.path.basename(attachment_path)}",
    )
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_SENDER, PROMO_COIN_RECIPIENTS, msg.as_string())


def transform_csv_for_clevertap(file_path):
    """Unchanged from the previous pipeline -- same column name
    (Customer_Phone) and same +91/type=i shape CleverTap expects."""
    try:
        df = pd.read_csv(file_path)
        if df.empty:
            return 0

        df = df.dropna(subset=['Customer_Phone'])
        df['type'] = 'i'
        df['identity'] = '+91' + df['Customer_Phone'].astype(str).str.replace(r'\.0$', '', regex=True)

        df_final = df[['type', 'identity']]
        df_final.to_csv(file_path, index=False)
        return len(df_final)
    except Exception as e:
        print(f"    ❌ Error transforming CSV: {e}")
        return -1


def upload_to_clevertap(file_path, segment_name):
    """Unchanged from the previous pipeline."""
    filename = os.path.basename(file_path)
    base_url = f"https://{CT_REGION}.api.clevertap.com"
    ct_headers = {
        'Content-Type': 'application/json',
        'X-CleverTap-Account-Id': CT_ACCOUNT_ID,
        'X-CleverTap-Passcode': CT_PASSCODE
    }

    res1 = requests.post(f"{base_url}/get_custom_list_segment_url", headers=ct_headers)
    if res1.status_code != 200 or res1.json().get("status") != "success":
        print(f"    ❌ CT Step 1 Failed: {res1.text}")
        return False
    presigned_url = res1.json().get("presignedS3URL")

    with open(file_path, 'rb') as file_data:
        res2 = requests.put(presigned_url, data=file_data)
    if res2.status_code != 200:
        print(f"    ❌ CT Step 2 Failed: {res2.text}")
        return False

    payload = {
        "name": segment_name,
        "email": CT_ADMIN_EMAIL,
        "filename": filename,
        "creator": CT_CREATOR_NAME,
        "url": presigned_url,
        "replace": CT_REPLACE_EXISTING
    }
    res3 = requests.post(f"{base_url}/upload_custom_list_segment_completed", json=payload, headers=ct_headers)

    if res3.status_code == 200 and res3.json().get("status") == "success":
        print(f"    ✅ Segment '{segment_name}' created (ID: {res3.json().get('Segment ID')})")
        return True
    else:
        print(f"    ❌ CT Step 3 Failed: {res3.text}")
        return False


# =====================================================
# 3. MAIN LOOP
# =====================================================
MAX_RETRIES = 3


def process_one_cohort(mb_headers, day, active_filters, label, failed_cohorts, promo_coin_sheets):
    """Fetch -> transform -> upload -> retry for ONE cohort. Shared by both
    manual and auto modes so the actual upload logic exists in exactly one
    place. Mutates failed_cohorts in place on ultimate failure.

    If this cohort's tactic is in OC_SPLIT_TACTICS, uploads up to 3
    segments (OC1/OC2/Rest) instead of 1. already_uploaded tracks which
    buckets succeeded across retry attempts within this call, so if e.g.
    OC1 succeeds and OC2 then fails, a retry only redoes OC2 -- not OC1
    again, which would otherwise create a duplicate CleverTap segment
    (CT_REPLACE_EXISTING is False).

    promo_coin_sheets: dict mutated in place, {segment_name: DataFrame} for
    the phone/Shopify-ID workbook. A segment's data is captured from the
    raw CSV BEFORE transform_csv_for_clevertap overwrites it, but only
    committed into promo_coin_sheets AFTER that segment's upload actually
    succeeds -- so a failed or not-yet-retried segment never contributes a
    sheet, and a retry can't double-commit a segment that already
    succeeded on an earlier attempt."""
    print(f"--------------------------------------------------")
    print(f"⚙️ Processing Cohort: {label}")
    print(f"    Filters: {active_filters}")

    # Same naming convention as before: 13Aug26_<cohort name>
    segment_name = f"{CT_DATE}_{label}"
    temp_csv = f"{sanitize_for_filename(label)}_temp.csv"

    tactic_value = active_filters.get(f"{day}_p1_p2_p3")
    needs_split = isinstance(tactic_value, str) and tactic_value in OC_SPLIT_TACTICS
    if needs_split:
        print(f"    ✂️  '{tactic_value}' is a split tactic -- will upload as separate OC1/OC2/Rest segments.")

    already_uploaded = set()
    known_buckets = []  # populated once splitting actually runs; used only
                         # to report which buckets are still missing if
                         # every retry attempt ultimately fails

    for attempt in range(1, MAX_RETRIES + 1):
        temp_files_to_clean = [temp_csv]
        try:
            if attempt > 1:
                print(f"    🔄 Retry Attempt {attempt} of {MAX_RETRIES}...")

            print("    ⬇️ Fetching data from Metabase...")
            fetch_metabase_csv(mb_headers, active_filters, temp_csv)

            if needs_split:
                buckets = split_by_oc_status(temp_csv)
                known_buckets = [b for b, _ in buckets]
                if not buckets:
                    print("    ⚠️ 0 rows returned. Skipping upload.")

                for bucket_label, bucket_csv in buckets:
                    temp_files_to_clean.append(bucket_csv)

                    if bucket_label in already_uploaded:
                        print(f"    ⏭️  [{bucket_label}] already uploaded on a prior attempt -- skipping.")
                        continue

                    # Captured BEFORE transform overwrites bucket_csv with
                    # just type/identity columns. Held locally, not
                    # committed to promo_coin_sheets yet -- only happens
                    # below once upload_to_clevertap actually succeeds.
                    coin_df = capture_promo_coin_data(bucket_csv)

                    bucket_row_count = transform_csv_for_clevertap(bucket_csv)
                    bucket_segment_name = f"{segment_name}_{bucket_label}"

                    if bucket_row_count > 0:
                        print(f"    📤 [{bucket_label}] Uploading {bucket_row_count:,} rows to CleverTap...")
                        upload_succeeded = upload_to_clevertap(bucket_csv, bucket_segment_name)
                        if not upload_succeeded:
                            raise RuntimeError(
                                f"CleverTap upload failed for {label} [{bucket_label}] -- "
                                f"see CT Step error above."
                            )
                        already_uploaded.add(bucket_label)
                        if coin_df is not None:
                            promo_coin_sheets[bucket_segment_name] = coin_df
                    elif bucket_row_count == 0:
                        print(f"    ⚠️ [{bucket_label}] 0 rows after transform. Skipping upload.")
                        already_uploaded.add(bucket_label)  # nothing left to retry for this bucket

            else:
                # Same capture-before-transform, commit-after-upload pattern
                # as the split path above.
                coin_df = capture_promo_coin_data(temp_csv)

                print("    🧹 Reformatting CSV for CleverTap...")
                row_count = transform_csv_for_clevertap(temp_csv)

                if row_count > 0:
                    print(f"    📤 Uploading {row_count:,} rows to CleverTap...")
                    upload_succeeded = upload_to_clevertap(temp_csv, segment_name)
                    if not upload_succeeded:
                        # upload_to_clevertap prints its own "CT Step N Failed"
                        # detail above -- this raise is what makes that failure
                        # actually COUNT. Without it, a False return here was
                        # silently treated as success: no retry, and the cohort
                        # never landed in failed_cohorts even though no segment
                        # was ever created.
                        raise RuntimeError(
                            f"CleverTap upload failed for {label} -- see CT Step error above."
                        )
                    if coin_df is not None:
                        promo_coin_sheets[segment_name] = coin_df
                elif row_count == 0:
                    print("    ⚠️ 0 rows returned. Skipping upload.")

            break  # Success! Break out of the retry loop.

        except Exception as e:
            print(f"    ⚠️ Error on attempt {attempt}: {e}")
            if attempt < MAX_RETRIES:
                print("    ⏳ Waiting 10 seconds before retrying...")
                time.sleep(10)
            else:
                if needs_split:
                    remaining = [b for b in known_buckets if b not in already_uploaded]
                    print(f"    ❌ Pipeline ultimately failed for {label} after {MAX_RETRIES} attempts "
                          f"-- buckets never uploaded: {remaining if remaining else known_buckets}.")
                    failed_cohorts.append(f"{label} (buckets: {remaining if remaining else known_buckets})")
                else:
                    print(f"    ❌ Pipeline ultimately failed for {label} after {MAX_RETRIES} attempts.")
                    failed_cohorts.append(label)

        finally:
            for f in temp_files_to_clean:
                if os.path.exists(f):
                    os.remove(f)

    print("    ⏸️ Sleeping for 5 seconds before moving to the next cohort...")
    time.sleep(5)


def run_pipeline():
    print("🔐 Authenticating with Metabase...")
    try:
        mb_headers = mb_authenticate()
    except Exception as e:
        print(f"❌ Metabase authentication failed: {e}")
        return

    failed_cohorts = []
    promo_coin_sheets = {}  # {segment_name: DataFrame(Customer_Phone, Shopify_ID)},
                             # accumulated across the whole run, emailed once at the end

    if MODE == "manual":
        print(f"🚀 Starting Pipeline (manual) for {len(FILTERS_TO_PROCESS)} slots. As-of Date: {START_DATE}\n")

        for entry in FILTERS_TO_PROCESS:
            day, active_filters = build_day_filters(entry)

            # A day with neither p1_p2_p3 nor discount filled in sends NO
            # filter to Metabase at all -- without this check the query
            # would run against the ENTIRE base for that day rather than
            # skip, silently and expensively. Filling in ONE of the two is
            # fine and expected; this only skips when BOTH are blank.
            if not active_filters:
                print(f"⏭️  Skipping {day} -- no p1_p2_p3 or discount value filled in.")
                continue

            label = build_label(day, active_filters, entry)
            process_one_cohort(mb_headers, day, active_filters, label, failed_cohorts, promo_coin_sheets)

    elif MODE == "auto":
        try:
            target_day = get_target_day()
        except Exception as e:
            print(f"❌ {e}")
            return
        print(f"📅 Target day: {target_day.capitalize()}"
              + (" (from OVERRIDE_DAY)" if os.getenv("OVERRIDE_DAY", "").strip() else " (today, IST)"))

        try:
            override_combos = get_cohort_override()
        except Exception as e:
            print(f"❌ {e}")
            return

        if override_combos is not None:
            print(f"🎯 Using {len(override_combos)} manually specified cohort(s) "
                  f"(from OVERRIDE_COHORTS) -- skipping discovery.")
            combos = [{"day": target_day, **oc} for oc in override_combos]
        else:
            print("🔎 Discovering cohorts from cohort_mapping_v2...")
            try:
                all_combos = fetch_combinations(mb_headers)
            except Exception as e:
                print(f"❌ Discovery failed: {e}")
                return
            combos = [c for c in all_combos if c["day"] == target_day]

        print(f"🚀 Starting Pipeline (auto) for {len(combos)} cohort(s) on "
              f"{target_day.capitalize()}. As-of Date: {START_DATE}\n")

        if not combos:
            print(f"⚠️ No cohorts found for {target_day.capitalize()} -- nothing to upload.")

        for combo in combos:
            day = combo["day"]
            active_filters = {
                f"{day}_p1_p2_p3": combo["p1_p2_p3"],
                f"{day}_discount": combo["discount"],
            }
            label = build_label(day, active_filters, combo)
            process_one_cohort(mb_headers, day, active_filters, label, failed_cohorts, promo_coin_sheets)

    else:
        print(f"❌ Unknown MODE: {MODE!r}. Must be \"manual\" or \"auto\".")
        return

    # =====================================================
    # PROMO COIN WORKBOOK + EMAIL
    # =====================================================
    if promo_coin_sheets:
        print(f"📊 Building promo coin workbook -- {len(promo_coin_sheets)} sheet(s)...")
        workbook_path = f"promo_coin_customers_{CT_DATE}.xlsx"
        try:
            write_promo_coin_workbook(promo_coin_sheets, workbook_path)
            print(f"📧 Emailing workbook to {', '.join(PROMO_COIN_RECIPIENTS)}...")
            send_promo_coin_email(workbook_path, len(promo_coin_sheets))
            print("📧 Email sent successfully.")
        except Exception as e:
            print(f"❌ Promo coin workbook/email failed: {e}")
            print("    (CleverTap uploads above are unaffected by this -- only the coin-tracking email failed.)")
        finally:
            if os.path.exists(workbook_path):
                os.remove(workbook_path)
    else:
        print("📊 No promo coin data captured this run -- skipping workbook/email.")

    # =====================================================
    # FINAL SUMMARY LOG
    # =====================================================
    print("\n==================================================")
    print("🎉 Pipeline Execution Complete!")

    if failed_cohorts:
        print(f"⚠️ The following {len(failed_cohorts)} cohort(s) failed after {MAX_RETRIES} attempts:")
        for cohort in failed_cohorts:
            print(f"   - {cohort}")
        print("\n💡 TIP: re-run to retry -- auto mode will rediscover the same combos; "
              "manual mode needs those entries put back in FILTERS_TO_PROCESS.")
    else:
        print("✅ All cohorts processed successfully with 0 failures!")
    print("==================================================\n")


if __name__ == "__main__":
    run_pipeline()
