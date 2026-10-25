import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests

import parl_monitor as monitor


START, END = date(2026, 7, 8), date(2026, 9, 20)
TITLE = "Order Paper for the sitting on 10 September 2026"


def response(payload, status=200):
    return SimpleNamespace(status_code=status, content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})


def row(identity="release-1", title=TITLE):
    return {"MediaReleaseId": identity, "Title": title, "DateTime": "2026-09-09T22:20:58.58",
            "Url": "/media_releases/parl/press_release/P-20260909-2",
            "SgpcAgencyAgency": {"AgencyAbbr": "PARL"}}


def page(rows, number=1, total=None):
    return response({"status": 200, "IsSuccess": False,
                     "result": {"currentPage": number, "totalPagesCount": len(rows) if total is None else total, "items": rows}})


def detail(title=TITLE, text=""):
    return response({"status": 200, "IsSuccess": False, "result": {"Title": title,
        "DocumentContent": text, "RelatedDocuments": [{"FileName": "Order Paper.pdf",
        "Path": "/sgpcmedia/media_releases/parl/press_release/P-20260909-2/attachment/Order Paper.pdf"}]}})


def report(day="2026-09-10"):
    return {"metadata": {"sittingDate": date.fromisoformat(day).strftime("%d-%m-%Y")},
            "takesSectionVOList": [{"title": "Example Bill", "sectionType": "BP", "content": "Order for Second Reading read."}]}


class ParsingTests(unittest.TestCase):
    def test_sprs_request_uses_json_post(self):
        client = monitor.HTTP(timeout=7, delay=0)
        client.session = Mock()
        client.session.post.return_value = response(report())
        payload = {"sittingDate": "10-09-2026"}
        client(monitor.SPRS, payload)
        client.session.post.assert_called_once_with(monitor.SPRS, json=payload, timeout=7)
        client.session.get.assert_not_called()

    def test_publication_and_sitting_dates_are_distinct(self):
        rows, total, marker = monitor.parse_page(page([row()]).content, 1, START, END)
        self.assertEqual(rows[0]["sitting_date"], "2026-09-10")
        self.assertEqual(rows[0]["published_local"][:10], "2026-09-09")
        self.assertEqual(total, 1)
        self.assertFalse(marker)  # This misleading field is false on valid live SGPC results.

    def test_revised_notice_and_non_sitting_dispositions(self):
        self.assertEqual(monitor.candidate("Revised Order Paper for the sitting on 8 September 2026")[0], "2026-09-08")
        self.assertEqual(monitor.candidate("NOTICE OF PARLIAMENT SITTING ON TUESDAY, 4 AUGUST 2026 AT 12 NOON")[0], "2026-08-04")
        self.assertEqual(monitor.candidate("Press Release on the Report of the Estimates Committee"), (None, "not_sitting_notice"))
        for title in ["Notice of Parliament sitting on 10 September 2026 postponed", "Order Paper for the sitting on 10 September 2026 and 11 September 2026", "Order Paper for the sitting on 31 February 2026"]:
            self.assertEqual(monitor.candidate(title), (None, "requires_review"))

    def test_wrong_agency_date_and_page_count_rejected(self):
        bad = row()
        bad["SgpcAgencyAgency"]["AgencyAbbr"] = "MOF"
        bodies = [page([bad]).content, page([row()], number=2).content, page([row()], total=10).content,
                  page([{**row(), "SgpcAgencyAgency": "PARL"}]).content,
                  page([{**row(), "DateTime": "2020-01-01"}]).content]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                monitor.parse_page(body, 1, START, END)

    def test_detail_identity_and_attachment_reference(self):
        docs = monitor.parse_detail(detail().content, TITLE)
        self.assertEqual(docs[0]["capture_status"], "reference_only")
        self.assertIn("Order%20Paper.pdf", docs[0]["url"])
        for body in [response([]).content, detail("Wrong title").content,
                     response({"status": 200, "result": {"Title": TITLE, "RelatedDocuments": [None]}}).content]:
            with self.assertRaises(ValueError):
                monitor.parse_detail(body, TITLE)

    def test_visible_votes_ignore_script_data_and_reject_empty(self):
        body = b'<title>Votes and Proceedings</title><div>Sitting on 7 July 2026</div><script>Sitting on 10 September 2026</script>'
        self.assertEqual(monitor.votes_dates(body), ["2026-07-07"])
        with self.assertRaises(ValueError):
            monitor.votes_dates(b"<html>Votes and Proceedings: temporarily unavailable</html>")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "monitor"
        self.root.mkdir()
        self.corpus = Path(self.tmp.name) / "corpus"
        self.corpus.mkdir()
        self.db = monitor.connect(self.root)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def discover(self, rows=None, detail_text=""):
        rows = [row()] if rows is None else rows
        def fetch(url):
            if "SubmitData" in url:
                return page(rows)
            return detail(rows[0]["Title"], detail_text)
        return monitor.discover(self.db, self.root, START, END, fetch)

    def sitting(self, day="2026-09-10"):
        return self.db.execute("SELECT * FROM sittings WHERE sitting_date=?", (day,)).fetchone()

    def test_discovery_and_repeat_are_idempotent_with_raw_provenance(self):
        first = self.discover()
        second = self.discover()
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["candidate_dates"], ["2026-09-10"])
        for table in ("versions", "releases", "sittings", "evidence"):
            self.assertEqual(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
        version = json.loads(self.db.execute("SELECT record_json FROM versions").fetchone()[0])
        self.assertTrue((self.root / "raw" / version["detail_sha256"]).is_file())
        self.assertEqual(version["attachments"][0]["capture_status"], "reference_only")

    def test_detail_changes_preserve_versions(self):
        self.discover(detail_text="original")
        self.discover(detail_text="revised")
        self.assertEqual(self.db.execute("SELECT count(*) FROM versions").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM evidence WHERE active=1").fetchone()[0], 1)

    def test_revised_date_retains_old_evidence_for_review(self):
        self.discover()
        self.discover([row(title="Revised Order Paper for the sitting on 11 September 2026")])
        monitor.reconcile(self.db, self.root, self.corpus)
        self.assertEqual(self.sitting()["state"], "needs_review")
        self.assertEqual(self.sitting("2026-09-11")["state"], "candidate")
        self.assertEqual(self.db.execute("SELECT active FROM evidence WHERE sitting_date='2026-09-10'").fetchone()[0], 0)

    def test_partial_pagination_does_not_admit_or_erase_candidates(self):
        self.discover()
        first = page([row(f"new-{i}") for i in range(9)], total=10)
        def fetch(url):
            if "page=1" in url:
                return first
            raise requests.ConnectionError("fixture interrupted")
        result = monitor.discover(self.db, self.root, START, END, fetch)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["listing_complete"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM releases").fetchone()[0], 1)
        self.assertEqual(self.sitting()["state"], "candidate")
        self.assertTrue(list((self.root / "runs" / result["run_id"]).glob("listing-*.json")))

    def test_duplicate_pagination_and_page_limit_fail(self):
        first = page([row(f"id-{i}") for i in range(9)], total=10)
        for limit in (1, 2):
            result = monitor.discover(self.db, self.root, START, END,
                lambda url: first if "page=1" in url else page([row("id-0")], number=2, total=10), max_pages=limit)
            self.assertEqual(result["status"], "failed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM releases").fetchone()[0], 0)

    def test_successful_two_page_inventory(self):
        first = page([row(f"id-{i}") for i in range(9)], total=10)
        def fetch(url):
            if "Detail" in url:
                return detail()
            return first if "page=1" in url else page([row("last")], number=2, total=10)
        result = monitor.discover(self.db, self.root, START, END, fetch)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["release_count"], 10)
        self.assertEqual(result["captured_pages"], 2)

    def test_empty_complete_window_is_not_absence_of_sittings(self):
        self.discover()
        result = self.discover([])
        self.assertTrue(result["zero_result"])
        self.assertTrue(result["listing_complete"])
        self.assertEqual(self.sitting()["state"], "candidate")

    def test_detail_failure_and_votes_lag_are_explicit(self):
        def fetch(url):
            if url == monitor.VOTES:
                return SimpleNamespace(status_code=200, content=b"<title>Votes and Proceedings</title><div>Sitting on 7 July 2026</div>", headers={})
            return page([row()]) if "SubmitData" in url else response({}, 503)
        result = monitor.discover(self.db, self.root, START, END, fetch, with_votes=True)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["listing_complete"])
        self.assertEqual(result["votes_visible_dates"], ["2026-07-07"])
        self.assertEqual(self.sitting()["state"], "candidate")

    def test_staged_capture_does_not_mutate_corpus_and_is_not_refetched(self):
        self.discover()
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        def fetch(url, payload):
            self.assertEqual(url, monitor.SPRS)
            self.assertEqual(payload, {"sittingDate": "10-09-2026"})
            return response(report())
        with contextlib.redirect_stderr(io.StringIO()):
            result = monitor.acquire(self.db, self.root, self.corpus, fetch, now=now)
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.sitting()["state"], "acquired")
        self.assertFalse(list(self.corpus.iterdir()))
        self.assertTrue((self.root / "reports/2026-09-10.meta.json").exists())
        self.assertEqual(result["results"][0]["response"]["request_json"], {"sittingDate": "10-09-2026"})
        result = monitor.acquire(self.db, self.root, self.corpus, lambda _: self.fail("refetched"), now=now)
        self.assertEqual(result["attempted"], 0)

    def test_existing_corpus_validated_and_not_overwritten(self):
        self.discover()
        target = self.corpus / "2026-09-10.json"
        original = json.dumps(report()).encode()
        target.write_bytes(original)
        result = monitor.acquire(self.db, self.root, self.corpus, lambda _: self.fail("network call"))
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(self.sitting()["state"], "present")
        target.write_text("corrupt")
        monitor.reconcile(self.db, self.root, self.corpus)
        self.assertEqual(self.sitting()["state"], "invalid_local")

    def test_unavailable_error_and_wrong_date_are_never_success(self):
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.discover()
        for payload, state in [(None, "awaiting_report"), ({"errorCode": 500}, "fetch_failed"), (report("2026-09-09"), "fetch_failed")]:
            with contextlib.redirect_stderr(io.StringIO()):
                result = monitor.acquire(self.db, self.root, self.corpus, lambda url, request: response(payload), retry_now=True, now=now)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(self.sitting()["state"], state)
            self.assertIsNotNone(self.sitting()["next_check_at"])
            self.assertFalse((self.root / "reports/2026-09-10.json").exists())
        result = monitor.acquire(self.db, self.root, self.corpus, lambda _: self.fail("retried before due"), now=now)
        self.assertEqual(result["attempted"], 0)

    def test_future_sittings_and_unrecognized_dates_not_fetched(self):
        self.discover()
        result = monitor.acquire(self.db, self.root, self.corpus, lambda _: self.fail("future sitting"), now=datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(result["attempted"], 0)
        with self.assertRaises(ValueError):
            monitor.acquire(self.db, self.root, self.corpus, lambda _: None, dates=["2000-01-01"])

    def test_process_lock_and_interrupted_run(self):
        with monitor.locked(self.root):
            with self.assertRaises(RuntimeError), monitor.locked(self.root):
                pass
        old = monitor.start_run(self.db, "discover")
        self.discover()
        self.assertEqual(self.db.execute("SELECT status FROM runs WHERE run_id=?", (old,)).fetchone()[0], "interrupted")


if __name__ == "__main__":
    unittest.main()
