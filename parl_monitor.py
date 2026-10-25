#!/usr/bin/env python3
"""Discover PARL announcements and acquire candidate Hansard sittings locally."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests

from hansard_report import atomic_write_bytes, sha256_bytes, validate_hansard_payload

SCHEMA = "parl-monitor-v1"
SGPC = "https://www.sgpc.gov.sg"
LISTING = SGPC + "/api/ApiMainListing/SubmitData"
VOTES = "https://www.parliament.gov.sg/parliamentary-business/votes-and-proceedings"
SPRS = "https://sprs.parl.gov.sg/search/getHansardReport/"
MONTHS = "January February March April May June July August September October November December".split()
DATE_RE = r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})"
SITTING_RE = re.compile(
    r"\bsitting\s+on\s+(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    + DATE_RE, re.I,
)


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(value=None):
    return (value or utcnow()).isoformat().replace("+00:00", "Z")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def write_json(path, value):
    atomic_write_bytes(str(path), encoded(value))


def date_match(match):
    day, month, year = match.groups()
    return date(int(year), next(i for i, m in enumerate(MONTHS, 1) if m.lower() == month.lower()), int(day))


def candidate(title):
    """Only explicit sitting titles nominate dates; publication dates never do."""
    if re.search(r"cancel|postpon|reschedul", title, re.I):
        return None, "requires_review"
    if not re.search(r"order paper|notice of parliament sitting", title, re.I):
        return None, "not_sitting_notice"
    matches = list(SITTING_RE.finditer(title))
    if len(matches) != 1:
        return None, "requires_review"
    # Do not silently turn ranges or multiple dates into a single sitting.
    tail = title[matches[0].end():].strip()
    if tail and not re.match(r"^(?:at\b|[.(])", tail, re.I):
        return None, "requires_review"
    try:
        return date_match(matches[0]).isoformat(), "candidate_sitting"
    except ValueError:
        return None, "requires_review"


def listing_url(start, end, page):
    def api_date(d):
        return f"{d.day:02d}-{MONTHS[d.month - 1][:3]}-{d.year}"
    return LISTING + "?" + urlencode({"Agency": "PARL", "StartDate": api_date(start), "EndDate": api_date(end), "page": page})


def parse_page(body, page, start, end):
    data = json.loads(body.decode("utf-8-sig"))
    if not isinstance(data, dict) or data.get("status") != 200:
        raise ValueError("SGPC response lacks status=200")
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise ValueError("SGPC response lacks result.items")
    # SGPC calls the ROW count totalPagesCount; the page size is nine.
    total = int(result["totalPagesCount"])
    if int(result["currentPage"]) != page or total < 0:
        raise ValueError("invalid page number or total")
    expected_rows = max(0, min(9, total - (page - 1) * 9))
    if len(result["items"]) != expected_rows:
        raise ValueError("page row count does not agree with total")
    items = []
    for row in result["items"]:
        if not isinstance(row, dict):
            raise ValueError("non-object listing row")
        agency_record = row.get("SgpcAgencyAgency") or {}
        if not isinstance(agency_record, dict):
            raise ValueError("invalid agency object")
        agency = agency_record.get("AgencyAbbr") or row.get("AgencyAbbr")
        if agency != "PARL":
            raise ValueError("PARL agency filter was not honored")
        for key in ("MediaReleaseId", "Title", "DateTime", "Url"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"missing listing field {key}")
        published = date.fromisoformat(row["DateTime"][:10])
        if not start <= published <= end:
            raise ValueError("publication date outside requested window")
        url = urljoin(SGPC, row["Url"])
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "www.sgpc.gov.sg" or not parsed.path.startswith("/media_releases/parl/"):
            raise ValueError("unexpected PARL release URL")
        sitting, disposition = candidate(row["Title"])
        items.append({"release_id": row["MediaReleaseId"], "title": row["Title"],
                      "published_local": row["DateTime"], "url": url, "agency": agency,
                      "sitting_date": sitting, "disposition": disposition,
                      "listing_sha256": sha256_bytes(encoded(row)), "source_row": row})
    # IsSuccess=false also occurs on valid live results; retain it, do not use it as a success gate.
    return items, total, data.get("IsSuccess")


def parse_detail(body, title):
    data = json.loads(body.decode("utf-8-sig"))
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("status") != 200 or not isinstance(result, dict) or result.get("Title") != title:
        raise ValueError("invalid detail response or listing/detail title mismatch")
    related = result.get("RelatedDocuments") or []
    if not isinstance(related, list):
        raise ValueError("invalid attachment inventory")
    attachments = []
    for document in related:
        if not isinstance(document, dict):
            raise ValueError("invalid attachment object")
        name, path = document.get("FileName"), document.get("Path")
        if not isinstance(name, str) or not isinstance(path, str) or not path.startswith("/sgpcmedia/media_releases/parl/"):
            raise ValueError("invalid PARL attachment reference")
        attachments.append({"file_name": name, "source_path": path,
                            "url": SGPC + "/api/file/getfile/" + quote(name, safe="") + "?" + urlencode({"path": path}),
                            "capture_status": "reference_only"})
    return attachments


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def votes_dates(body):
    parser = VisibleText()
    parser.feed(body.decode("utf-8"))
    text = " ".join(parser.parts)
    if "Votes and Proceedings" not in text:
        raise ValueError("not a Votes and Proceedings page")
    dates = {date_match(m).isoformat() for m in re.finditer(r"Sitting on\s+" + DATE_RE, text, re.I)}
    if not dates:
        raise ValueError("no visible sitting dates; source unavailable, not zero sittings")
    return sorted(dates)


class HTTP:
    def __init__(self, timeout=30, delay=0.25):
        self.timeout, self.delay = timeout, delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "singapore-hansard-corpus-parl-monitor/1.0"})

    def __call__(self, url, payload=None):
        for attempt in range(3):
            try:
                if payload is None:
                    response = self.session.get(url, timeout=self.timeout)
                else:
                    response = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** (attempt + 1))
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                time.sleep(self.delay)
                return response
            try:
                delay = max(2 ** (attempt + 1), int(response.headers.get("Retry-After", 0)))
            except ValueError:
                delay = 2 ** (attempt + 1)
            time.sleep(min(delay, 30))


@contextlib.contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another PARL monitor command is running") from None
        yield


def connect(root):
    connection = sqlite3.connect(root / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL,
            status TEXT NOT NULL, summary_json TEXT);
        CREATE TABLE IF NOT EXISTS versions (
            release_id TEXT NOT NULL, version_sha256 TEXT NOT NULL, record_json TEXT NOT NULL,
            first_seen_run TEXT NOT NULL REFERENCES runs(run_id),
            PRIMARY KEY(release_id, version_sha256));
        CREATE TABLE IF NOT EXISTS releases (
            release_id TEXT PRIMARY KEY, version_sha256 TEXT NOT NULL,
            last_seen_run TEXT NOT NULL REFERENCES runs(run_id));
        CREATE TABLE IF NOT EXISTS sittings (
            sitting_date TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'candidate',
            attempts INTEGER NOT NULL DEFAULT 0, next_check_at TEXT,
            last_checked_at TEXT, error TEXT, report_sha256 TEXT);
        CREATE TABLE IF NOT EXISTS evidence (
            sitting_date TEXT NOT NULL REFERENCES sittings(sitting_date),
            kind TEXT NOT NULL, identity TEXT NOT NULL, version_sha256 TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES runs(run_id), active INTEGER NOT NULL,
            PRIMARY KEY(sitting_date, kind, identity, version_sha256));
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
            sitting_date TEXT NOT NULL REFERENCES sittings(sitting_date), result_json TEXT NOT NULL);
    """)
    return connection


def start_run(connection, kind):
    # Caller holds the process lock. A previous running entry is an interrupted run.
    connection.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
    run = uuid.uuid4().hex
    connection.execute("INSERT INTO runs VALUES (?,?,?,?,NULL)", (run, kind, stamp(), "running"))
    connection.commit()
    return run


def finish_run(connection, root, summary):
    summary = {"schema_version": SCHEMA, "finished_at": stamp(), **summary}
    connection.execute("UPDATE runs SET status=?, summary_json=? WHERE run_id=?",
                       (summary["status"], json.dumps(summary), summary["run_id"]))
    connection.commit()
    write_json(root / "runs" / summary["run_id"] / "summary.json", summary)
    return summary


def fetch_record(root, run, label, url, fetch, payload=None):
    """Preserve every delivered response, including invalid JSON/error pages."""
    response = fetch(url) if payload is None else fetch(url, payload)
    body = response.content
    digest = sha256_bytes(body)
    target = root / "raw" / digest
    if not target.exists():
        atomic_write_bytes(str(target), body)
    record = {"url": url, "method": "GET" if payload is None else "POST", "request_json": payload,
              "observed_at": stamp(), "http_status": response.status_code,
              "sha256": digest, "raw_path": str(target.relative_to(root)), "bytes": len(body),
              "content_type": response.headers.get("Content-Type", "")}
    write_json(root / "runs" / run / (label + ".json"), record)
    return response, record


def add_evidence(connection, sitting, kind, identity, version, run):
    connection.execute("INSERT OR IGNORE INTO sittings(sitting_date) VALUES (?)", (sitting,))
    connection.execute("INSERT INTO evidence VALUES (?,?,?,?,?,1) ON CONFLICT DO UPDATE SET active=1,run_id=excluded.run_id",
                       (sitting, kind, identity, version, run))


def discover(connection, root, start, end, fetch, max_pages=40, with_votes=False):
    if start > end or max_pages < 1:
        raise ValueError("invalid discovery range or page limit")
    run = start_run(connection, "discover")
    summary = {"run_id": run, "kind": "discover", "status": "failed",
               "query": {"agency": "PARL", "start": start.isoformat(), "end": end.isoformat()},
               "listing_complete": False, "detail_failures": [], "votes_status": "not_requested"}
    staged, pages, total, ids = [], 0, None, set()
    try:
        page = 1
        while total is None or page <= max(1, math.ceil(total / 9)):
            if page > max_pages:
                raise ValueError("declared pagination exceeds max-pages")
            response, record = fetch_record(root, run, f"listing-{page:04d}", listing_url(start, end, page), fetch)
            if response.status_code != 200:
                raise ValueError(f"listing HTTP {response.status_code}")
            items, current_total, marker = parse_page(response.content, page, start, end)
            if total is not None and total != current_total:
                raise ValueError("listing totals changed during pagination")
            total = current_total
            for item in items:
                if item["release_id"] in ids:
                    raise ValueError("duplicate release across pages; unstable pagination")
                ids.add(item["release_id"])
                item["listing_response_sha256"] = record["sha256"]
                staged.append(item)
            pages += 1
            summary["api_success_marker"] = marker
            page += 1
        if len(staged) != total:
            raise ValueError("listing inventory does not reconcile")
        summary["listing_complete"] = True
        # Details carry attachment references and content changes beyond listing metadata.
        for index, item in enumerate(staged):
            url = SGPC + "/api/ApiMainListing/Detail?" + urlencode({"url": urlparse(item["url"]).path})
            try:
                response, record = fetch_record(root, run, f"detail-{index:04d}", url, fetch)
                if response.status_code != 200:
                    raise ValueError(f"detail HTTP {response.status_code}")
                item["attachments"] = parse_detail(response.content, item["title"])
                item["detail_sha256"] = record["sha256"]
                item["detail_status"] = "captured"
            except (ValueError, KeyError, TypeError, requests.RequestException) as exc:
                item["detail_status"] = "unavailable"
                item["detail_error"] = str(exc)
                summary["detail_failures"].append({"release_id": item["release_id"], "error": str(exc)})
            # Run-specific listing response hashes are provenance, not release-version identity.
            identity = {k: item.get(k) for k in ("listing_sha256", "detail_sha256", "detail_status")}
            version = sha256_bytes(encoded(identity))
            item["version_sha256"] = version
        with connection:
            for item in staged:
                identity, version = item["release_id"], item["version_sha256"]
                connection.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?)", (identity, version, json.dumps(item), run))
                connection.execute("INSERT INTO releases VALUES (?,?,?) ON CONFLICT DO UPDATE SET version_sha256=excluded.version_sha256,last_seen_run=excluded.last_seen_run", (identity, version, run))
                connection.execute("UPDATE evidence SET active=0 WHERE kind='sgpc' AND identity=?", (identity,))
                if item["sitting_date"]:
                    add_evidence(connection, item["sitting_date"], "sgpc", identity, version, run)
        summary.update(status="partial" if summary["detail_failures"] else "success",
                       release_count=len(staged), zero_result=(total == 0),
                       candidate_dates=sorted({i["sitting_date"] for i in staged if i["sitting_date"]}),
                       requires_review=[i["release_id"] for i in staged if i["disposition"] == "requires_review"])
    except (ValueError, KeyError, TypeError, requests.RequestException) as exc:
        summary["error"] = str(exc)
        # No listing rows are admitted before the entire window passes validation.
    summary.update(captured_pages=pages, reported_items=total)
    if with_votes:
        try:
            response, record = fetch_record(root, run, "votes-visible", VOTES, fetch)
            if response.status_code != 200:
                raise ValueError(f"Votes and Proceedings HTTP {response.status_code}")
            dates = votes_dates(response.content)
            with connection:
                for sitting in dates:
                    if start.isoformat() <= sitting <= end.isoformat():
                        add_evidence(connection, sitting, "votes_visible", VOTES, record["sha256"], run)
            summary.update(votes_status="success", votes_visible_dates=dates,
                           votes_scope="Visible rolling page only; absence does not disprove a sitting.")
        except (ValueError, requests.RequestException) as exc:
            summary.update(votes_status="failed", votes_error=str(exc))
            if summary["status"] == "success":
                summary["status"] = "partial"
    return finish_run(connection, root, summary)


def validated_hash(path, sitting):
    body = path.read_bytes()
    validate_hansard_payload(json.loads(body), date.fromisoformat(sitting))
    return sha256_bytes(body)


def reconcile(connection, root, corpus):
    changes = []
    for row in connection.execute("SELECT * FROM sittings ORDER BY sitting_date").fetchall():
        sitting = row["sitting_date"]
        state, digest, error = None, None, None
        for folder, success in ((corpus, "present"), (root / "reports", "acquired")):
            path = folder / (sitting + ".json")
            if not path.exists():
                continue
            try:
                digest = validated_hash(path, sitting)
                state = success
            except (OSError, ValueError) as exc:
                state, error = "invalid_local", str(exc)
            break
        if state is None:
            supported = connection.execute("SELECT 1 FROM evidence WHERE sitting_date=? AND active=1", (sitting,)).fetchone()
            state = (row["state"] if row["state"] in {"awaiting_report", "fetch_failed"} else "candidate") if supported else "needs_review"
        if state != row["state"] or digest != row["report_sha256"]:
            connection.execute("UPDATE sittings SET state=?,report_sha256=?,error=? WHERE sitting_date=?", (state, digest, error, sitting))
            changes.append({"sitting_date": sitting, "state": state})
    connection.commit()
    return changes


def acquire(connection, root, corpus, fetch, budget=5, dates=None, retry_now=False, now=None):
    if budget < 1:
        raise ValueError("budget must be positive")
    now = now or utcnow()
    reconcile(connection, root, corpus)
    rows = connection.execute("SELECT * FROM sittings ORDER BY sitting_date").fetchall()
    wanted = set(dates or [])
    if wanted - {r["sitting_date"] for r in rows}:
        raise ValueError("requested dates have no discovery evidence")
    eligible = [r for r in rows if (not wanted or r["sitting_date"] in wanted)
                and r["state"] in {"candidate", "awaiting_report", "fetch_failed"}
                and r["sitting_date"] <= now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
                and (retry_now or not r["next_check_at"] or r["next_check_at"] <= stamp(now))][:budget]
    run = start_run(connection, "acquire")
    results = []
    for row in eligible:
        sitting = row["sitting_date"]
        day = date.fromisoformat(sitting)
        url = SPRS
        payload = {"sittingDate": day.strftime("%d-%m-%Y")}
        result = {"sitting_date": sitting, "state": "fetch_failed", "checked_at": stamp(now)}
        try:
            response, record = fetch_record(root, run, sitting, url, fetch, payload)
            result["response"] = record
            if response.status_code in {404, 204}:
                result.update(state="awaiting_report", error=f"HTTP {response.status_code}; availability unconfirmed")
            elif response.status_code != 200:
                result["error"] = f"HTTP {response.status_code}"
            else:
                payload = json.loads(response.content)
                if payload is None or payload == {}:
                    result.update(state="awaiting_report", error="SPRS returned an empty report; retry required")
                else:
                    fmt = validate_hansard_payload(payload, day)
                    # Stage captures separately; changing an existing export is a separate build.
                    target = root / "reports" / (sitting + ".json")
                    atomic_write_bytes(str(target), response.content)
                    write_json(target.with_suffix(".meta.json"), {
                        "sitting_date": sitting, "captured_at": record["observed_at"],
                        "http_status": 200, "format": fmt, "raw_sha256": record["sha256"],
                        "bytes": len(response.content), "source_url": url,
                        "request_method": "POST", "request_json": record["request_json"],
                    })
                    result.update(state="acquired", report_sha256=record["sha256"], error=None)
        except (ValueError, requests.RequestException) as exc:
            result["error"] = str(exc)
        attempts = row["attempts"] + 1
        next_check = None if result["state"] == "acquired" else stamp(now + timedelta(hours=min(24 * 2 ** min(attempts - 1, 3), 168)))
        result["next_check_at"] = next_check
        with connection:
            connection.execute("UPDATE sittings SET state=?,attempts=?,next_check_at=?,last_checked_at=?,error=?,report_sha256=? WHERE sitting_date=?",
                               (result["state"], attempts, next_check, stamp(now), result.get("error"), result.get("report_sha256"), sitting))
            connection.execute("INSERT INTO attempts VALUES (?,?,?,?)", (uuid.uuid4().hex, run, sitting, json.dumps(result)))
        results.append(result)
        print(f"{sitting}: {result['state']}", file=sys.stderr, flush=True)
    return finish_run(connection, root, {"run_id": run, "kind": "acquire",
                      "status": "partial" if any(r["state"] != "acquired" for r in results) else "success",
                      "attempted": len(results), "results": results})


def status(connection, limit=20):
    runs = [dict(r) for r in connection.execute("SELECT run_id,kind,started_at,status FROM runs ORDER BY started_at DESC,rowid DESC LIMIT 5")]
    rows = connection.execute("SELECT * FROM sittings ORDER BY sitting_date DESC LIMIT ?", (limit,)).fetchall()
    queue = []
    for row in rows:
        item = dict(row)
        item["evidence"] = [dict(e) for e in connection.execute("SELECT kind,identity,version_sha256,run_id,active FROM evidence WHERE sitting_date=? ORDER BY kind,identity", (row["sitting_date"],))]
        queue.append(item)
    counts = {r["state"]: r["n"] for r in connection.execute("SELECT state,count(*) AS n FROM sittings GROUP BY state")}
    return {"schema_version": SCHEMA, "recent_runs": runs, "counts": counts, "queue": queue,
            "boundary": "Discovery is a candidate; acquired reports are staged, not yet part of the published corpus."}


def refresh(connection, root, corpus):
    from parl_snapshot import refresh_snapshot
    repo = Path(__file__).resolve().parent
    if corpus.resolve() != repo / "data/hansard/json":
        raise ValueError("snapshot refresh requires this repository's data/hansard/json corpus")
    run = start_run(connection, "refresh")
    try:
        result = refresh_snapshot(repo, root)
        reconcile(connection, root, corpus)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        result = {"status": "failed", "error": str(exc),
                  "build_log": str(root / "snapshot.build/build.log")}
    return finish_run(connection, root, {"run_id": run, "kind": "refresh", **result})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("var/monitoring/parl"))
    parser.add_argument("--corpus-dir", type=Path, default=Path("data/hansard/json"))
    parser.add_argument("--timeout", type=float, default=30)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "run"):
        d = sub.add_parser(name)
        d.add_argument("--start-date", type=date.fromisoformat)
        d.add_argument("--end-date", type=date.fromisoformat, default=datetime.now(timezone(timedelta(hours=8))).date())
        d.add_argument("--lookback-days", type=int, default=90)
        d.add_argument("--max-pages", type=int, default=40)
        d.add_argument("--votes", action="store_true")
        if name == "run":
            d.add_argument("--budget", type=int, default=5)
    a = sub.add_parser("acquire")
    a.add_argument("--budget", type=int, default=5)
    a.add_argument("--dates", nargs="+", type=lambda s: date.fromisoformat(s).isoformat())
    a.add_argument("--retry-now", action="store_true")
    sub.add_parser("reconcile")
    sub.add_parser("refresh")
    s = sub.add_parser("status")
    s.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if args.data_root.resolve() == args.corpus_dir.resolve():
        parser.error("monitor state and corpus must be separate directories")
    with locked(args.data_root):
        connection = connect(args.data_root)
        try:
            if args.command in {"discover", "run"}:
                if args.lookback_days < 1:
                    parser.error("lookback-days must be positive")
                result = discover(connection, args.data_root, args.start_date or args.end_date - timedelta(days=args.lookback_days), args.end_date, HTTP(args.timeout), args.max_pages, args.votes)
                result["reconciled"] = reconcile(connection, args.data_root, args.corpus_dir)
                if args.command == "run":
                    discovery = result
                    acquisition = acquire(connection, args.data_root, args.corpus_dir, HTTP(args.timeout), args.budget)
                    snapshot = refresh(connection, args.data_root, args.corpus_dir)
                    result = {"discovery": discovery, "acquisition": acquisition, "snapshot": snapshot,
                              "status": "success" if all(r["status"] == "success" for r in (discovery, acquisition, snapshot)) else "partial"}
            elif args.command == "acquire":
                result = acquire(connection, args.data_root, args.corpus_dir, HTTP(args.timeout), args.budget, args.dates, args.retry_now)
            elif args.command == "refresh":
                result = refresh(connection, args.data_root, args.corpus_dir)
            elif args.command == "reconcile":
                result = {"changes": reconcile(connection, args.data_root, args.corpus_dir)}
            else:
                if args.limit < 1:
                    parser.error("limit must be positive")
                result = status(connection, args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get("status") in {"failed", "partial"} else 0
        finally:
            connection.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
