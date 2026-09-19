import json
import os
import tempfile
import unittest
from datetime import date

from hansard_report import (
    atomic_write_bytes,
    dumps_json_object,
    is_sitting_filename,
    load_validated_report,
    sitting_filenames,
    validate_hansard_payload,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


class JsonObjectSerializationTests(unittest.TestCase):
    def test_contract_example_hash(self):
        encoded = dumps_json_object({"b": 1, "a": "café"})
        self.assertEqual(encoded, '{"a": "café", "b": 1}')
        self.assertFalse(encoded.endswith("\n"))
        self.assertEqual(
            __import__("hashlib").sha256(encoded.encode("utf-8")).hexdigest(),
            "292eafeb3584e5e68d7ca7aa6780d68c22b47914aab0b9d3584b31acf8285632",
        )


class ValidateReportTests(unittest.TestCase):
    def test_modern_report(self):
        data = load_fixture("modern_opening.json")
        self.assertEqual(
            validate_hansard_payload(data, date(2025, 1, 7)), "modern"
        )

    def test_legacy_report(self):
        data = load_fixture("legacy_sitting.json")
        self.assertEqual(
            validate_hansard_payload(data, date(1955, 10, 12)), "legacy"
        )

    def test_malformed_sit_date_meta(self):
        data = {
            "htmlFullContent": (
                "<html><Meta name='Sit_Date' content='200310-16'></head>"
                "<body>" + ("x" * 200) +
                "Sitting Date:</font></td><td width='85%'><font size='2'>16-10-2003</font>"
                "</body></html>"
            )
        }
        self.assertEqual(
            validate_hansard_payload(data, date(2003, 10, 16)), "legacy"
        )

    def test_error_payload_rejected(self):
        data = load_fixture("error_payload.json")
        with self.assertRaises(ValueError):
            validate_hansard_payload(data, date(2025, 1, 7))

    def test_wrong_date_rejected(self):
        data = load_fixture("modern_opening.json")
        with self.assertRaises(ValueError):
            validate_hansard_payload(data, date(2025, 1, 8))

    def test_arbitrary_html_with_date_is_rejected(self):
        data = {
            "htmlFullContent": "<html><body>"
            + ("The sitting on 07-01-2025 was mentioned. " * 20)
            + "</body></html>"
        }
        with self.assertRaises(ValueError):
            validate_hansard_payload(data, date(2025, 1, 7))

    def test_sitting_filename_ignores_sidecars(self):
        self.assertTrue(is_sitting_filename("2025-01-07.json"))
        self.assertFalse(is_sitting_filename("2025-01-07.meta.json"))
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "2025-01-07.json"), "w").close()
            open(os.path.join(tmp, "2025-01-07.meta.json"), "w").close()
            self.assertEqual(sitting_filenames(tmp), ["2025-01-07.json"])

    def test_empty_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2025-01-07.json")
            atomic_write_bytes(path, b"")
            with self.assertRaises(ValueError):
                load_validated_report(path, date(2025, 1, 7))


if __name__ == "__main__":
    unittest.main()
