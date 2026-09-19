import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

from hansard_report import (
    atomic_write_bytes,
    load_validated_report,
    sha256_bytes,
    validate_hansard_payload,
)

URL = "https://sprs.parl.gov.sg/search/getHansardReport/"
DATES_FILE = "./sitting_dates.txt"
DATA_DIR = "./data/hansard/json"
FAILED_LOG = "./data/hansard/failed_dates.txt"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 2
REQUEST_DELAY = 0.25
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
USER_AGENT = "singapore-hansard-corpus/1.0"


def load_sitting_dates(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Sitting dates file not found: {path}")

    dates = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                dates.append(datetime.strptime(text, "%Y-%m-%d").date())
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: invalid date {text!r}") from exc

    if not dates:
        raise ValueError(f"No sitting dates found in {path}")
    return dates


def fetch_hansard(session, sitting_date):
    payload = {"sittingDate": sitting_date.strftime("%d-%m-%Y")}
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.post(URL, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                sleep_for = RETRY_BACKOFF**attempt
                print(
                    f"{sitting_date.isoformat()}: {exc}; "
                    f"retry {attempt}/{MAX_RETRIES} in {sleep_for}s"
                )
                time.sleep(sleep_for)
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
            retry_after = response.headers.get("Retry-After")
            try:
                sleep_for = max(RETRY_BACKOFF**attempt, int(retry_after))
            except (TypeError, ValueError):
                sleep_for = RETRY_BACKOFF**attempt
            print(
                f"{sitting_date.isoformat()}: HTTP {response.status_code}; "
                f"retry {attempt}/{MAX_RETRIES} in {sleep_for}s"
            )
            time.sleep(sleep_for)
            last_error = requests.HTTPError(
                f"HTTP {response.status_code} for {URL}",
                response=response,
            )
            continue

        return response

    raise last_error


def append_failed(sitting_date, reason):
    os.makedirs(os.path.dirname(FAILED_LOG), exist_ok=True)
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{sitting_date.isoformat()}\t{reason}\n")


def write_capture_meta(file_name, sitting_date, raw, fmt, http_status):
    meta_path = file_name[: -len(".json")] + ".meta.json"
    payload = {
        "sitting_date": sitting_date.isoformat(),
        "captured_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "http_status": http_status,
        "format": fmt,
        "raw_sha256": sha256_bytes(raw),
        "bytes": len(raw),
    }
    atomic_write_bytes(
        meta_path, json.dumps(payload, indent=2).encode("utf-8") + b"\n"
    )


def cached_report_is_valid(file_name, sitting_date):
    try:
        load_validated_report(file_name, sitting_date)
    except (OSError, ValueError):
        return False
    return True


def store_response(file_name, sitting_date, response):
    raw = response.content
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    fmt = validate_hansard_payload(payload, sitting_date)
    atomic_write_bytes(file_name, raw)
    write_capture_meta(file_name, sitting_date, raw, fmt, response.status_code)
    return fmt


def main(dates_file=DATES_FILE):
    sitting_dates = load_sitting_dates(dates_file)
    os.makedirs(DATA_DIR, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    written = skipped = failed = 0
    total = len(sitting_dates)
    print(f"Fetching {total} sitting dates from {dates_file}")

    for index, sitting_date in enumerate(sitting_dates, 1):
        prefix = f"[{index}/{total}] {sitting_date.isoformat()}"
        file_name = os.path.join(DATA_DIR, f"{sitting_date.isoformat()}.json")
        if os.path.exists(file_name) and cached_report_is_valid(
            file_name, sitting_date
        ):
            print(f"{prefix}: skipping valid cached file")
            skipped += 1
            continue

        try:
            response = fetch_hansard(session, sitting_date)
        except requests.RequestException as exc:
            print(f"{prefix}: request failed after retries: {exc}")
            append_failed(sitting_date, str(exc))
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue

        if response.status_code != 200:
            reason = f"HTTP {response.status_code} ({response.reason})"
            print(f"{prefix}: {reason}")
            append_failed(sitting_date, reason)
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue

        try:
            fmt = store_response(file_name, sitting_date, response)
        except ValueError as exc:
            print(f"{prefix}: invalid report: {exc}")
            append_failed(sitting_date, f"invalid report: {exc}")
            failed += 1
        else:
            print(f"{prefix}: writing {file_name} ({fmt})")
            written += 1

        time.sleep(REQUEST_DELAY)

    print(
        f"Done. written={written} skipped={skipped} failed={failed} total={total}"
    )
    if failed:
        print(f"Failed dates logged to {FAILED_LOG}")
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DATES_FILE)
