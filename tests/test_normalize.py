import json
import os
import tempfile
import unittest
from datetime import date

from hansard_norm import PARSER_VERSION, SCHEMA_VERSION
from hansard_report import sha256_bytes
from hansard_norm.report import normalize_payload, records_from_report
from hansard_norm.retrieve import RetrievalError, retrieve_section
from hansard_norm.check import validate_snapshot
from hansard_norm.store import normalize_corpus, report_path
from hansard_norm.report import canonical_dumps
from hansard_report import sha256_text
from hansard_report import atomic_write_bytes, sha256_bytes, sha256_text, validate_hansard_payload
from hansard_norm.spans import strip_html

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


class NormalizeSchemaTests(unittest.TestCase):
    def test_legacy_two_bills_have_distinct_spans(self):
        data = load_fixture("legacy_two_bills.json")
        report = normalize_payload(
            "1955-05-25.json",
            data,
            "data/hansard/json/1955-05-25.json",
            "abc",
            "legacy",
        )
        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        seconds = [
            s
            for s in report["sections"]
            if s["proceeding_type"] == "second_reading_opening"
        ]
        self.assertEqual(len(seconds), 2)
        self.assertNotEqual(seconds[0]["locator"]["start"], seconds[1]["locator"]["start"])
        self.assertIn("powers and privileges uniquely", seconds[0]["text"].lower())
        self.assertIn("overtime rules", seconds[1]["text"].lower())
        html = data["htmlFullContent"]
        for section in seconds:
            loc = section["locator"]
            fragment = html[loc["start"] : loc["end"]]
            self.assertEqual(sha256_text(fragment), loc["source_fragment_sha256"])
            self.assertEqual(sha256_text(section["text"]), loc["normalized_text_sha256"])

    def test_modern_retains_take_order_and_extras(self):
        data = load_fixture("modern_opening.json")
        data["annexureList"] = [
            {"annexureTitle": "Annex A", "fileName": "a.pdf"}
        ]
        report = normalize_payload(
            "2025-01-07.json", data, "x.json", "abc", "modern"
        )
        takes = [s for s in report["sections"] if s["section_id"].split(":")[2] == "takes"]
        self.assertGreaterEqual(len(takes), 2)
        types = [s["proceeding_type"] for s in takes]
        self.assertIn("first_reading", types)
        self.assertIn("second_reading_opening", types)
        self.assertTrue(any(s["proceeding_type"] == "annexure" for s in report["sections"]))

    def test_error_payload_rejected(self):
        with self.assertRaises(ValueError):
            validate_hansard_payload(load_fixture("error_payload.json"), date(2025, 1, 7))

    def test_wrong_date_rejected(self):
        with self.assertRaises(ValueError):
            validate_hansard_payload(load_fixture("modern_opening.json"), date(1999, 1, 1))

    def test_unsupported_schema_retrieve(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "reports"))
            path = os.path.join(tmp, "reports", "2025-01-07.json")
            atomic_write_bytes(
                path,
                json.dumps(
                    {
                        "schema_version": "nope",
                        "sitting_date": "2025-01-07",
                        "sections": [
                            {"section_id": "sitting:2025-01-07:takes:0", "text": "x", "locator": {}}
                        ],
                    }
                ).encode(),
            )
            with self.assertRaises(RetrievalError):
                retrieve_section(tmp, "sitting:2025-01-07:takes:0")

    def test_missing_normalized_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "reports"))
            with self.assertRaises(RetrievalError):
                retrieve_section(tmp, "sitting:1999-01-01:takes:0")

    def test_legacy_section_text_is_not_truncated(self):
        unique = ("UNIQUE_TOKEN_%s " % "X" * 20) * 200
        html = (
            "<html><head><Meta name='Sit_Date' content='1955-10-12'>"
            "<Meta name='Parl_No' content='1'></head><body>"
            "<p>Sitting Date:</font></td><td><font>12-10-1955</font></p>"
            "<p>LONG BILL</p>"
            '<p>"to do a thing", presented by the Minister; read the First time.</p>'
            "<p>LONG BILL</p><p>Order for Second Reading read.</p>"
            f"<p>The Minister: Sir, I beg to move, \"That the Bill be now read a Second time.\" {unique}</p>"
            "</body></html>"
        )
        data = {"htmlFullContent": html}
        report = normalize_payload("1955-10-12.json", data, "x.json", "abc", "legacy")
        section = next(
            s
            for s in report["sections"]
            if s["proceeding_type"] == "second_reading_opening"
        )
        self.assertGreater(len(section["text"]), 2500)
        self.assertIn("UNIQUE_TOKEN", section["text"][-80:])
        self.assertEqual(sha256_text(section["text"]), section["locator"]["normalized_text_sha256"])

    def test_modern_text_hash_is_direct(self):
        data = load_fixture("modern_opening.json")
        data["attendanceList"] = [{"mpName": "A", "attendance": True}]
        report = normalize_payload("2025-01-07.json", data, "x.json", "abc", "modern")
        for section in report["sections"]:
            self.assertEqual(
                sha256_text(section["text"]),
                section["locator"]["normalized_text_sha256"],
            )
            self.assertIn(
                section["locator"]["unit"],
                {"json_string", "json_object", "json_fields"},
            )
        take = next(s for s in report["sections"] if s["proceeding_type"] == "second_reading_opening")
        self.assertEqual(take["locator"]["unit"], "json_string")
        content = data["takesSectionVOList"][1]["content"]
        self.assertEqual(sha256_text(content), take["locator"]["source_fragment_sha256"])
        attend = next(s for s in report["sections"] if s["proceeding_type"] == "attendance")
        self.assertEqual(attend["locator"]["unit"], "json_object")

    def test_empty_sections_fail_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_dir = os.path.join(tmp, "json")
            out_dir = os.path.join(tmp, "norm")
            os.makedirs(json_dir)
            payload = load_fixture("modern_opening.json")
            atomic_write_bytes(
                os.path.join(json_dir, "2025-01-07.json"),
                json.dumps(payload).encode(),
            )
            normalize_corpus(json_dir, out_dir)
            path = report_path(out_dir, "2025-01-07")
            with open(path, encoding="utf-8") as handle:
                report = json.load(handle)
            report["sections"] = []
            atomic_write_bytes(path, canonical_dumps(report).encode())
            snapshot = json.load(open(os.path.join(out_dir, "SNAPSHOT.json")))
            snapshot["inventory"][0]["normalized_sha256"] = sha256_bytes(
                canonical_dumps(report).encode()
            )
            snapshot["inventory"][0]["sections"] = 0
            atomic_write_bytes(
                os.path.join(out_dir, "SNAPSHOT.json"),
                canonical_dumps(snapshot).encode(),
            )
            errors, _snap, _complete = validate_snapshot(out_dir, json_dir)
            self.assertTrue(any("no sections" in err for err in errors))

    def test_failed_snapshot_cannot_export(self):
        from normalize_hansard import cmd_export
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "norm")
            os.makedirs(os.path.join(out_dir, "reports"))
            atomic_write_bytes(
                os.path.join(out_dir, "SNAPSHOT.json"),
                canonical_dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "parser_version": PARSER_VERSION,
                        "sittings": 0,
                        "complete": False,
                        "failures": [{"sitting": "x", "error": "boom"}],
                        "inventory": [],
                    }
                ).encode(),
            )
            args = Namespace(
                out_dir=out_dir,
                json_dir=tmp,
                export_dir=os.path.join(tmp, "export"),
                index=os.path.join(tmp, "missing-index.json"),
                contract=os.path.join(tmp, "missing-contract.md"),
                handoff=os.path.join(tmp, "missing-handoff.md"),
                conformance=os.path.join(tmp, "conf"),
            )
            with self.assertRaises(SystemExit):
                cmd_export(args)

    def test_subset_normalize_preserves_other_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_dir = os.path.join(tmp, "json")
            out_dir = os.path.join(tmp, "norm")
            os.makedirs(json_dir)
            for name, payload in (
                ("2025-01-07.json", load_fixture("modern_opening.json")),
                ("2025-01-08.json", load_fixture("modern_resumed.json")),
            ):
                atomic_write_bytes(os.path.join(json_dir, name), json.dumps(payload).encode())
            normalize_corpus(json_dir, out_dir)
            self.assertEqual(len(os.listdir(os.path.join(out_dir, "reports"))), 2)
            normalize_corpus(json_dir, out_dir, dates=["2025-01-07"])
            names = sorted(os.listdir(os.path.join(out_dir, "reports")))
            self.assertEqual(names, ["2025-01-07.json", "2025-01-08.json"])

    def test_sidecar_not_normalized_as_sitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_dir = os.path.join(tmp, "json")
            out_dir = os.path.join(tmp, "norm")
            os.makedirs(json_dir)
            payload = load_fixture("modern_opening.json")
            atomic_write_bytes(
                os.path.join(json_dir, "2025-01-07.json"),
                json.dumps(payload).encode(),
            )
            atomic_write_bytes(
                os.path.join(json_dir, "2025-01-07.meta.json"),
                b'{"sitting_date":"2025-01-07"}',
            )
            snapshot = normalize_corpus(json_dir, out_dir)
            self.assertEqual(snapshot["sittings"], 1)


class CorpusNormalizeTests(unittest.TestCase):
    def _report(self, day):
        path = os.path.join("data", "hansard", "json", f"{day}.json")
        if not os.path.isfile(path):
            self.skipTest(f"missing {path}")
        report, _raw = __import__(
            "hansard_norm.report", fromlist=["normalize_file"]
        ).normalize_file(path)
        return report

    def test_1955_privileges_and_labour(self):
        report = self._report("1955-05-25")
        seconds = [
            s
            for s in report["sections"]
            if s["proceeding_type"] == "second_reading_opening"
        ]
        titles = [s["normalized_title"] for s in seconds]
        self.assertIn("Legislative Assembly (Powers and Privileges) Bill", titles)
        self.assertIn("Labour Bill", titles)
        labour = next(s for s in seconds if s["normalized_title"] == "Labour Bill")
        self.assertEqual(labour["locator"]["start"], 17659)
        self.assertIn("labour", labour["text"].lower())

    def test_2005_iras_and_nurses(self):
        report = self._report("2005-04-21")
        by_title = {
            s["normalized_title"]: s
            for s in report["sections"]
            if s["proceeding_type"].startswith("second_reading")
        }
        self.assertEqual(
            by_title["Inland Revenue Authority of Singapore (Amendment) Bill"][
                "locator"
            ]["start"],
            283106,
        )
        self.assertEqual(
            by_title["Nurses and Midwives (Amendment) Bill"]["locator"]["start"],
            286638,
        )

    def test_2010_constitution_events(self):
        report = self._report("2010-04-26")
        rows = [
            s
            for s in report["sections"]
            if s["normalized_title"]
            == "Constitution of the Republic of Singapore (Amendment) Bill"
            and s["proceeding_type"].startswith("second_reading")
        ]
        types = [s["proceeding_type"] for s in rows]
        self.assertIn("second_reading_opening", types)
        self.assertIn("second_reading_resumed", types)
        opening = next(s for s in rows if s["proceeding_type"] == "second_reading_opening")
        resumed = next(s for s in rows if s["proceeding_type"] == "second_reading_resumed")
        self.assertEqual(opening["locator"]["start"], 170404)
        self.assertEqual(resumed["locator"]["start"], 227389)

    def test_2012_legal_profession_and_immigration(self):
        report = self._report("2012-02-14")
        legal = [
            s
            for s in report["sections"]
            if s["normalized_title"] == "Legal Profession (Amendment) Bill"
            and s["proceeding_type"].startswith("second_reading")
        ]
        self.assertEqual(
            {s["proceeding_type"] for s in legal},
            {"second_reading_opening", "second_reading_resumed"},
        )
        report = self._report("2012-08-13")
        imm = [
            s
            for s in report["sections"]
            if s["normalized_title"] == "Immigration (Amendment) Bill"
            and s["proceeding_type"].startswith("second_reading")
        ]
        self.assertEqual(
            {s["proceeding_type"] for s in imm},
            {"second_reading_opening", "second_reading_resumed"},
        )

    def test_1981_judges_boundary(self):
        report = self._report("1981-03-17")
        item = next(
            s
            for s in report["sections"]
            if s["normalized_title"] == "Judges' Remuneration (Amendment) Bill"
            and s["proceeding_type"] == "second_reading_opening"
        )
        self.assertEqual(item["locator"]["start"], 56081)
        self.assertEqual(item["locator"]["end"], 59386)
