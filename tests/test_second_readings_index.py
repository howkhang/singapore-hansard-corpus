import json
import os
import unittest
from datetime import date

from build_second_readings_index import (
    bill_title_from_prefix,
    display_title,
    classify_second_reading_event,
    collapse_space,
    find_html_offset,
    match_readings,
    parse_legacy_html,
    parse_modern,
    passage_from_html,
    sha256_text,
    strip_html,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


class TitleExtractionTests(unittest.TestCase):
    def test_chair_prefix_is_not_part_of_title(self):
        self.assertEqual(
            bill_title_from_prefix(
                "[Mr Speaker in the Chair] CONSTITUTION OF THE REPUBLIC OF SINGAPORE (AMENDMENT) BILL"
            ),
            "CONSTITUTION OF THE REPUBLIC OF SINGAPORE (AMENDMENT) BILL",
        )
        self.assertEqual(
            display_title(
                bill_title_from_prefix(
                    "Sitting resumed at 4.15 pm [ Mr Speaker in the Chair] IMMIGRATION (AMENDMENT) BILL"
                )
            ),
            "Immigration (Amendment) Bill",
        )


class EventClassificationTests(unittest.TestCase):
    def test_opening(self):
        text = 'Order for Second Reading read. I move, "That the Bill be now read a Second time."'
        self.assertEqual(classify_second_reading_event(text), "opening")

    def test_question_again_proposed_is_resumed(self):
        text = 'Question again proposed, "That the Bill be now read a Second Time."'
        self.assertEqual(classify_second_reading_event(text), "resumed")

    def test_debate_resumed(self):
        self.assertEqual(classify_second_reading_event("Debate resumed."), "resumed")


class IndexerTests(unittest.TestCase):
    def test_modern_keeps_same_day_resumption(self):
        data = load_fixture("modern_opening.json")
        first, second = parse_modern("2025-01-07.json", data)
        self.assertEqual(len(first), 1)
        events = [item["event"] for item in second]
        self.assertEqual(events, ["opening", "resumed"])

    def test_resumed_sitting_is_linked_not_dropped(self):
        opening = load_fixture("modern_opening.json")
        resumed = load_fixture("modern_resumed.json")
        first, second = parse_modern("2025-01-07.json", opening)
        first2, second2 = parse_modern("2025-01-08.json", resumed)
        self.assertEqual(first2, [])
        self.assertEqual(second2[0]["event"], "resumed")
        entries, pending = match_readings(first, second + second2)
        self.assertEqual(pending, [])
        self.assertEqual(len(entries), 1)
        dates = [item["date"] for item in entries[0].get("continued_on") or []]
        self.assertIn("2025-01-08", dates)
        self.assertTrue(
            any(item.get("event") == "resumed" for item in entries[0]["continued_on"])
        )

    def test_distinct_amendments_are_not_merged(self):
        first_a = {
            "title": "Police Force (Amendment) Bill",
            "norm": "POLICE FORCE (AMENDMENT) BILL",
            "date": "1955-09-01",
            "file": "data/hansard/json/1955-09-01.json",
            "parliament": 1,
            "session": 1,
            "long_title": "to amend the Police Force Ordinance",
            "presented_by": "the Chief Secretary",
            "locator": {"format": "legacy", "file": "x", "html_offset": 1},
        }
        second_a = {
            "title": "Police Force (Amendment) Bill",
            "norm": "POLICE FORCE (AMENDMENT) BILL",
            "date": "1955-10-12",
            "file": "data/hansard/json/1955-10-12.json",
            "parliament": 1,
            "session": 1,
            "event": "opening",
            "moved_by": "The Chief Secretary",
            "opening": "This amends section 44.",
            "locator": {"format": "legacy", "file": "a", "html_offset": 10},
        }
        second_b = {
            "title": "Police Force (Amendment) Bill",
            "norm": "POLICE FORCE (AMENDMENT) BILL",
            "date": "1967-11-02",
            "file": "data/hansard/json/1967-11-02.json",
            "parliament": 1,
            "session": 1,
            "event": "opening",
            "moved_by": "The Minister",
            "opening": "A later amendment.",
            "locator": {"format": "legacy", "file": "b", "html_offset": 20},
        }
        entries, _pending = match_readings([first_a], [second_a, second_b])
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(not entry.get("continued_on") for entry in entries))

    def test_distant_resumption_language_does_not_merge(self):
        opening = {
            "title": "Trade Unions (Amendment) Bill",
            "norm": "TRADE UNIONS (AMENDMENT) BILL",
            "date": "1959-08-13",
            "file": "a.json",
            "parliament": 1,
            "session": 1,
            "event": "opening",
            "moved_by": "",
            "opening": "1959 speech",
            "locator": {"html_offset": 1},
        }
        later = dict(opening)
        later.update(
            {
                "date": "1965-12-23",
                "file": "b.json",
                "event": "resumed",
                "opening": "later sitting",
                "locator": {"html_offset": 2},
            }
        )
        entries, _pending = match_readings([], [opening, later])
        self.assertEqual(len(entries), 2)

    def test_first_reading_window_does_not_cross_later_introduction(self):
        def rec(kind, title, day, parl=14, session=1, event="opening"):
            item = {
                "title": title,
                "norm": title.upper(),
                "date": day,
                "file": f"{day}.json",
                "parliament": parl,
                "session": session,
                "long_title": "",
                "presented_by": "",
                "event": event,
                "moved_by": "",
                "opening": "",
                "locator": {"section_index": 0, "file": f"{day}.json"},
            }
            return item

        first_2020 = rec("first", "Housing and Development (Amendment) Bill", "2020-09-03")
        first_2022 = rec("first", "Housing and Development (Amendment) Bill", "2022-10-20")
        second_2020 = rec(
            "second",
            "Housing and Development Board (Amendment) Bill",
            "2020-10-06",
        )
        second_2023 = rec(
            "second",
            "Housing and Development (Amendment) Bill",
            "2023-01-10",
        )
        entries, pending = match_readings(
            [first_2020, first_2022], [second_2020, second_2023]
        )
        confirmed = [
            (e["first_reading"]["date"], e["second_reading"]["date"], e["link_confidence"])
            for e in entries
            if e.get("first_reading")
        ]
        self.assertIn(("2022-10-20", "2023-01-10", "first_and_second_matched"), confirmed)
        self.assertNotIn(("2020-09-03", "2023-01-10", "first_and_second_matched"), confirmed)
        variant = [
            e
            for e in entries
            if e["second_reading"]["date"] == "2020-10-06"
        ]
        self.assertEqual(len(variant), 1)
        self.assertEqual(variant[0]["link_confidence"], "title_variant_unconfirmed")
        self.assertEqual(variant[0]["first_reading"]["date"], "2020-09-03")
        pending_dates = [e["first_reading"]["date"] for e in pending]
        self.assertNotIn("2022-10-20", pending_dates)

    def test_legacy_html_offset_points_into_html(self):
        junk = "padding <b>not the marker</b> " * 80
        html = (
            "<html><head><Meta name='Sit_Date' content='1955-10-12'>"
            "<Meta name='Parl_No' content='1'><Meta name='Sess_No' content='1'></head>"
            f"<body>{junk}"
            "<p>LABOUR BILL</p>"
            '<p>"to amend labour law", presented by the Minister; read the First time.</p>'
            "<p>LABOUR BILL</p>"
            "<p>Order for <b>Second</b> Reading read.</p>"
            "<p>The Minister for Labour: Mr Speaker, Sir, I beg to move, "
            '"That the Bill be now read a Second time." The Bill does X.</p>'
            "</body></html>"
        )
        first, second = parse_legacy_html("1955-10-12.json", {"htmlFullContent": html})
        self.assertEqual(len(second), 1)
        offset = second[0]["locator"]["html_offset"]
        self.assertIsNotNone(offset)
        snippet = html[offset : offset + 80]
        self.assertIn("Order for", snippet)
        self.assertNotEqual(offset, second[0]["locator"].get("normalized_offset"))
        self.assertGreaterEqual(find_html_offset(html, "Order for Second Reading read"), 0)

    def test_legacy_multi_bill_locators_are_occurrence_aware(self):
        data = load_fixture("legacy_two_bills.json")
        html = data["htmlFullContent"]
        _first, second = parse_legacy_html("1955-05-25.json", data)
        self.assertEqual(
            [item["norm"] for item in second],
            [
                "LEGISLATIVE ASSEMBLY (POWERS AND PRIVILEGES) BILL",
                "LABOUR BILL",
            ],
        )
        offsets = [item["locator"]["html_offset"] for item in second]
        self.assertEqual(len(set(offsets)), 2)
        self.assertLess(offsets[0], offsets[1])
        rebuilt = [
            passage_from_html(
                html,
                item["locator"]["html_offset"],
                item["locator"].get("html_end"),
            )
            for item in second
        ]
        self.assertIn("powers and privileges uniquely", rebuilt[0].lower())
        self.assertNotIn("overtime rules", rebuilt[0].lower())
        self.assertIn("overtime rules", rebuilt[1].lower())
        self.assertNotIn("powers and privileges uniquely", rebuilt[1].lower())
        for item, text in zip(second, rebuilt):
            self.assertEqual(sha256_text(text), item["locator"]["passage_sha256"])
            self.assertIsNotNone(item["locator"]["html_offset"])

    def _parse_corpus_sitting(self, day):
        path = os.path.join("data", "hansard", "json", f"{day}.json")
        if not os.path.isfile(path):
            self.skipTest(f"missing {path}")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        html = data["htmlFullContent"]
        _first, second = parse_legacy_html(f"{day}.json", data)
        self.assertTrue(second)
        self.assertTrue(all(item["locator"]["html_offset"] is not None for item in second))
        return html, second

    def _assert_span(self, html, item, offset, exact_title, event, marker_phrase):
        self.assertEqual(item["title"], exact_title)
        self.assertEqual(item["locator"]["html_offset"], offset)
        self.assertEqual(item["event"], event)
        rebuilt = passage_from_html(
            html, offset, item["locator"].get("html_end")
        )
        self.assertEqual(sha256_text(rebuilt), item["locator"]["passage_sha256"])
        rebuilt_default = passage_from_html(html, offset)
        self.assertEqual(sha256_text(rebuilt_default), item["locator"]["passage_sha256"])
        self.assertIn(marker_phrase.lower(), rebuilt.lower()[:80])

    def _linked_opening(self, second, exact_title):
        entries, _pending = match_readings([], second)
        matches = [
            entry
            for entry in entries
            if entry["title"] == exact_title
            and entry["second_reading"]["event"] == "opening"
        ]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def test_corpus_2005_04_21_iras_not_nurses(self):
        html, second = self._parse_corpus_sitting("2005-04-21")
        by_title = {item["norm"]: item for item in second}
        self._assert_span(
            html,
            by_title["INLAND REVENUE AUTHORITY OF SINGAPORE (AMENDMENT) BILL"],
            283106,
            "Inland Revenue Authority of Singapore (Amendment) Bill",
            "opening",
            "order for second reading",
        )
        self._assert_span(
            html,
            by_title["NURSES AND MIDWIVES (AMENDMENT) BILL"],
            286638,
            "Nurses and Midwives (Amendment) Bill",
            "opening",
            "order for second reading",
        )

    def test_corpus_2010_04_26_constitution_opening_not_resume(self):
        html, second = self._parse_corpus_sitting("2010-04-26")
        title = "Constitution of the Republic of Singapore (Amendment) Bill"
        constitution = [item for item in second if item["title"] == title]
        self.assertEqual(len(constitution), 2)
        opening = next(item for item in constitution if item["event"] == "opening")
        resumed = next(item for item in constitution if item["event"] == "resumed")
        self._assert_span(
            html, opening, 170404, title, "opening", "order for second reading"
        )
        self._assert_span(
            html, resumed, 227389, title, "resumed", "debate resumed"
        )
        linked = self._linked_opening(second, title)
        self.assertEqual(
            [item["locator"]["html_offset"] for item in linked.get("continued_on") or []],
            [227389],
        )

    def test_corpus_2012_02_14_resume_not_opening(self):
        html, second = self._parse_corpus_sitting("2012-02-14")
        title = "Legal Profession (Amendment) Bill"
        legal = [item for item in second if item["title"] == title]
        self.assertEqual(len(legal), 2)
        opening = next(item for item in legal if item["event"] == "opening")
        resumed = next(item for item in legal if item["event"] == "resumed")
        self._assert_span(
            html, opening, 194381, title, "opening", "order for second reading"
        )
        self._assert_span(
            html, resumed, 228388, title, "resumed", "debate resumed"
        )
        linked = self._linked_opening(second, title)
        self.assertEqual(
            [item["locator"]["html_offset"] for item in linked.get("continued_on") or []],
            [228388],
        )

    def test_corpus_2012_08_13_resume_not_opening(self):
        html, second = self._parse_corpus_sitting("2012-08-13")
        title = "Immigration (Amendment) Bill"
        immigration = [item for item in second if item["title"] == title]
        self.assertEqual(len(immigration), 2)
        opening = next(item for item in immigration if item["event"] == "opening")
        resumed = next(item for item in immigration if item["event"] == "resumed")
        self._assert_span(
            html, opening, 221161, title, "opening", "order for second reading"
        )
        self._assert_span(
            html, resumed, 290204, title, "resumed", "debate resumed"
        )
        linked = self._linked_opening(second, title)
        self.assertEqual(
            [item["locator"]["html_offset"] for item in linked.get("continued_on") or []],
            [290204],
        )

    def test_corpus_1981_03_17_judges_hash_uses_filtered_boundary(self):
        html, second = self._parse_corpus_sitting("1981-03-17")
        title = "Judges' Remuneration (Amendment) Bill"
        item = next(row for row in second if row["title"] == title)
        self.assertEqual(item["locator"]["html_offset"], 56081)
        self.assertEqual(item["locator"]["html_end"], 59386)
        rebuilt = passage_from_html(html, 56081)
        self.assertEqual(sha256_text(rebuilt), item["locator"]["passage_sha256"])
        unfiltered_end = 59107
        wrong = collapse_space(strip_html(html[56081:unfiltered_end]))[:2500]
        self.assertNotEqual(sha256_text(wrong), item["locator"]["passage_sha256"])

    def test_legacy_extracts_first_and_second_reading(self):
        data = load_fixture("legacy_sitting.json")
        first, second = parse_legacy_html("1955-10-12.json", data)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["event"], "opening")
        self.assertEqual(first[0]["parliament"], 1)


if __name__ == "__main__":
    unittest.main()
