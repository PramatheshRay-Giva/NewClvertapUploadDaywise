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
START_DATE = datetime.now().strftime("%Y-%m-%d")  # Metabase format (e.g., 2026-08-13)
CT_DATE = datetime.now().strftime("%d%b%y")        # CleverTap format (e.g., 13Aug26)

CT_ACCOUNT_ID = "R78-Z5K-847Z"
CT_PASSCODE = os.getenv("CT_PASSCODE")  # Set in repo Settings > Secrets and variables > Actions
CT_REGION = "in1"
CT_ADMIN_EMAIL = "shah.neil@giva.co"
CT_CREATOR_NAME = "Pramathesh Ray"
CT_REPLACE_EXISTING = False

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
}

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
    empty in the Metabase UI."""
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

    url = f"{MB_BASE_URL}/api/card/{MB_CARD_ID}/query/csv"
    resp = requests.post(
        url, headers=headers, json={"parameters": parameters},
        stream=True, timeout=900,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Query failed ({resp.status_code}): {resp.text[:500]}")

    with open(output_file, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


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


def process_one_cohort(mb_headers, day, active_filters, label, failed_cohorts):
    """Fetch -> transform -> upload -> retry for ONE cohort. Shared by both
    manual and auto modes so the actual upload logic exists in exactly one
    place. Mutates failed_cohorts in place on ultimate failure."""
    print(f"--------------------------------------------------")
    print(f"⚙️ Processing Cohort: {label}")
    print(f"    Filters: {active_filters}")

    # Same naming convention as before: 13Aug26_<cohort name>
    segment_name = f"{CT_DATE}_{label}"
    temp_csv = f"{sanitize_for_filename(label)}_temp.csv"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                print(f"    🔄 Retry Attempt {attempt} of {MAX_RETRIES}...")

            print("    ⬇️ Fetching data from Metabase...")
            fetch_metabase_csv(mb_headers, active_filters, temp_csv)

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
            elif row_count == 0:
                print("    ⚠️ 0 rows returned. Skipping upload.")

            break  # Success! Break out of the retry loop.

        except Exception as e:
            print(f"    ⚠️ Error on attempt {attempt}: {e}")
            if attempt < MAX_RETRIES:
                print("    ⏳ Waiting 10 seconds before retrying...")
                time.sleep(10)
            else:
                print(f"    ❌ Pipeline ultimately failed for {label} after {MAX_RETRIES} attempts.")
                failed_cohorts.append(label)

        finally:
            if os.path.exists(temp_csv):
                os.remove(temp_csv)

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
            process_one_cohort(mb_headers, day, active_filters, label, failed_cohorts)

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
            process_one_cohort(mb_headers, day, active_filters, label, failed_cohorts)

    else:
        print(f"❌ Unknown MODE: {MODE!r}. Must be \"manual\" or \"auto\".")
        return

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
