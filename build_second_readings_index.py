#!/usr/bin/env python3
"""Build an index of Second Reading debates from normalized Hansard reports."""

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime

from hansard_norm.report import normalize_payload, records_from_report
from hansard_norm.spans import (
    KIND_LABEL,
    bill_title_from_prefix,
    classify,
    classify_html_marker,
    classify_second_reading_event,
    collapse_space,
    display_title,
    extract_long_title,
    extract_moved_by,
    extract_opening,
    extract_presented_by,
    find_html_offset,
    html_marker_spans,
    is_second_reading_text,
    normalize_title,
    parse_legislature,
    passage_from_html,
    relative_file,
    sitting_date_from_name,
    strip_html,
    titles_are_variants,
)
from hansard_report import sha256_bytes, sha256_text, sitting_filenames

PARSER_VERSION = "second-readings-index-v7"
NORM_DIR = "./data/hansard/normalized/v1"

DATA_DIR = "./data/hansard/json"
JSON_OUT = "./data/hansard/second_readings_index.json"
MD_OUT = "./data/hansard/second_readings_index.md"


def parse_modern(filename, data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    report = normalize_payload(
        filename,
        data,
        source_path=relative_file(filename),
        source_sha256=sha256_bytes(raw),
        source_format="modern",
    )
    return records_from_report(report)


def parse_legacy_html(filename, data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    report = normalize_payload(
        filename,
        data,
        source_path=relative_file(filename),
        source_sha256=sha256_bytes(raw),
        source_format="legacy",
    )
    return records_from_report(report)


def group_key(record):
    parl = record.get("parliament")
    session = record.get("session")
    return (
        record["norm"],
        parl if parl is not None else "unknown",
        session if session is not None else "unspecified",
    )


def match_readings(first_readings, second_readings):
    first_groups = defaultdict(list)
    second_groups = defaultdict(list)
    for item in first_readings:
        first_groups[group_key(item)].append(item)
    for item in second_readings:
        second_groups[group_key(item)].append(item)
    for group in first_groups.values():
        group.sort(key=lambda item: item["date"])
    for group in second_groups.values():
        group.sort(key=lambda item: item["date"])

    matched_second_ids = set()
    entries = []
    pending = []

    def second_id(item):
        loc = item.get("locator") or {}
        return (
            item["norm"],
            item["date"],
            item["file"],
            loc.get("section_index"),
            loc.get("html_offset"),
        )

    leftover_firsts = []
    for key, firsts in first_groups.items():
        seconds = second_groups.get(key, [])
        cursor = 0
        for index, first in enumerate(firsts):
            window_end = (
                firsts[index + 1]["date"] if index + 1 < len(firsts) else "9999-12-31"
            )
            while cursor < len(seconds) and seconds[cursor]["date"] < first["date"]:
                cursor += 1
            if (
                cursor < len(seconds)
                and seconds[cursor]["date"] < window_end
            ):
                second = seconds[cursor]
                matched_second_ids.add(second_id(second))
                entries.append(make_entry(first, second))
                cursor += 1
            else:
                leftover_firsts.append(first)

    unmatched_seconds = []
    for key, seconds in second_groups.items():
        for second in seconds:
            if second_id(second) in matched_second_ids:
                continue
            unmatched_seconds.append(second)

    used_variant_firsts = set()
    for second in unmatched_seconds:
        variant = _nearest_title_variant(second, leftover_firsts, used_variant_firsts)
        if variant:
            used_variant_firsts.add(id(variant))
            entry = make_entry(variant, second)
            entry["link_confidence"] = "title_variant_unconfirmed"
            entries.append(entry)
        else:
            entries.append(make_entry(None, second))

    for first in leftover_firsts:
        if id(first) in used_variant_firsts:
            continue
        if not re.match(r"^(AMENDMENT|NO \d+)\)", first["norm"]):
            pending.append(make_pending(first))

    entries.sort(key=lambda item: (item["second_reading"]["date"], item["title"]))
    pending.sort(key=lambda item: (item["first_reading"]["date"], item["title"]))
    return merge_continuations(entries), pending


def _nearest_title_variant(second, leftover_firsts, used_ids):
    best = None
    for first in leftover_firsts:
        if id(first) in used_ids:
            continue
        if first.get("parliament") != second.get("parliament"):
            continue
        if first.get("session") != second.get("session"):
            continue
        if not titles_are_variants(first["title"], second["title"]):
            continue
        if first["date"] > second["date"]:
            continue
        gap = (
            datetime.strptime(second["date"], "%Y-%m-%d").date()
            - datetime.strptime(first["date"], "%Y-%m-%d").date()
        ).days
        if gap > 180:
            continue
        if best is None or first["date"] > best["date"]:
            best = first
    return best


def _locator_payload(record):
    locator = dict(record.get("locator") or {})
    locator.setdefault("file", record.get("file"))
    return locator


def merge_continuations(entries):
    groups = defaultdict(list)
    for entry in entries:
        sr = entry["second_reading"]
        key = (
            normalize_title(entry["title"]),
            entry.get("parliament"),
            entry.get("session"),
        )
        groups[key].append(entry)

    merged = []
    for items in groups.values():
        items.sort(
            key=lambda item: (
                item["second_reading"]["date"],
                item["second_reading"].get("section_index") or 0,
            )
        )
        current = None
        for entry in items:
            if current is None:
                current = entry
                continue
            event = entry["second_reading"].get("event")
            first = entry.get("first_reading") or {}
            prev_date = current["second_reading"]["date"]
            later = entry["second_reading"]["date"]
            try:
                gap_days = (
                    datetime.strptime(later, "%Y-%m-%d").date()
                    - datetime.strptime(prev_date, "%Y-%m-%d").date()
                ).days
            except ValueError:
                gap_days = None
            explicit_resumption = (
                event == "resumed"
                and (not first or first.get("date", "") <= prev_date)
                and gap_days is not None
                and 0 <= gap_days <= 120
            )
            if explicit_resumption:
                current.setdefault("continued_on", []).append(
                    {
                        "date": entry["second_reading"]["date"],
                        "file": entry["second_reading"]["file"],
                        "event": event,
                        "link_confidence": "explicit_resumption",
                        "locator": entry["second_reading"].get("locator"),
                    }
                )
            else:
                merged.append(current)
                current = entry
        if current:
            merged.append(current)
    merged.sort(key=lambda item: (item["second_reading"]["date"], item["title"]))
    return merged


def make_entry(first, second):
    title = (first or second)["title"]
    long_title = (first or {}).get("long_title") or ""
    kind = classify(title, long_title)
    parliament = (second or first).get("parliament")
    session = (second or first).get("session")
    if first and first.get("parliament") is not None:
        parliament = first["parliament"]
    if first and first.get("session") is not None:
        session = first["session"]
    entry = {
        "title": title,
        "kind": kind,
        "long_title": long_title,
        "parliament": parliament,
        "session": session,
        "parser_version": PARSER_VERSION,
        "first_reading": None,
        "second_reading": {
            "date": second["date"],
            "file": second["file"],
            "event": second.get("event") or "opening",
            "section_index": (second.get("locator") or {}).get("section_index"),
            "moved_by": second.get("moved_by") or "",
            "opening": second.get("opening") or "",
            "locator": _locator_payload(second),
        },
        "link_confidence": "first_and_second_matched" if first else "second_reading_only",
        "captured_at": None,
    }
    if first:
        entry["first_reading"] = {
            "date": first["date"],
            "file": first["file"],
            "presented_by": first.get("presented_by") or "",
            "locator": _locator_payload(first),
        }
        if not entry["long_title"]:
            entry["long_title"] = first.get("long_title") or ""
    return entry


def make_pending(first):
    return {
        "title": first["title"],
        "kind": classify(first["title"], first.get("long_title") or ""),
        "long_title": first.get("long_title") or "",
        "parliament": first.get("parliament"),
        "session": first.get("session"),
        "parser_version": PARSER_VERSION,
        "first_reading": {
            "date": first["date"],
            "file": first["file"],
            "presented_by": first.get("presented_by") or "",
            "locator": _locator_payload(first),
        },
        "second_reading": None,
        "link_confidence": "first_reading_only",
        "captured_at": None,
    }


def scan_corpus():
    first_readings = []
    second_readings = []
    names = sitting_filenames(DATA_DIR)
    reports_dir = os.path.join(NORM_DIR, "reports")
    use_norm = os.path.isdir(reports_dir)
    if use_norm:
        print(f"indexing from {reports_dir}")
    else:
        print(f"normalized reports missing; indexing from raw JSON in {DATA_DIR}")
    for index, name in enumerate(names, 1):
        if use_norm:
            npath = os.path.join(reports_dir, name)
            if not os.path.isfile(npath):
                raise SystemExit(f"missing normalized report {npath}")
            with open(npath, encoding="utf-8") as handle:
                report = json.load(handle)
            first, second = records_from_report(report)
        else:
            path = os.path.join(DATA_DIR, name)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("takesSectionVOList"):
                first, second = parse_modern(name, data)
            else:
                first, second = parse_legacy_html(name, data)
        first_readings.extend(first)
        second_readings.extend(second)
        if index % 250 == 0 or index == len(names):
            print(f"scanned {index}/{len(names)} sittings")
    return first_readings, second_readings


def write_markdown(entries, pending, stats):
    lines = [
        "# Second Readings of Bills",
        "",
        "Index of **Second Reading** debates in the Hansard corpus. "
        "Second Reading is when the Minister explains why a Bill is needed — "
        "use the long title (purpose in one sentence) and the opening of the "
        "speech to see the reason for a new Act or amendment.",
        "",
        f"Generated from `{DATA_DIR}` covering "
        f"{stats['from']} to {stats['to']}.",
        "",
        f"- Second readings indexed: **{len(entries)}**",
        f"- Bills introduced, Second Reading not yet in corpus: **{len(pending)}**",
        "",
        "## How to use",
        "",
        "- Jump to a year in the list below.",
        "- `long title` is the official purpose as read on First Reading.",
        "- `Opening` is the start of the Minister's Second Reading speech.",
        "- `file` points at the sitting JSON in this corpus.",
        "",
        "## By year",
        "",
    ]
    by_year = defaultdict(list)
    for entry in entries:
        year = entry["second_reading"]["date"][:4]
        by_year[year].append(entry)
    for year in sorted(by_year, reverse=True):
        lines.append(f"- [{year}](#{year}) ({len(by_year[year])})")
    lines.append("")

    for year in sorted(by_year, reverse=True):
        lines.append(f"## {year}")
        lines.append("")
        for entry in by_year[year]:
            sr = entry["second_reading"]
            fr = entry.get("first_reading")
            lines.append(f"### {entry['title']}")
            lines.append("")
            lines.append(f"- **Kind:** {KIND_LABEL.get(entry['kind'], entry['kind'])}")
            if entry.get("parliament") is not None:
                lines.append(f"- **Parliament:** {entry['parliament']}")
            if entry.get("long_title"):
                lines.append(f"- **Purpose (long title):** {entry['long_title']}")
            if fr:
                presented = f" — presented by {fr['presented_by']}" if fr.get("presented_by") else ""
                lines.append(
                    f"- **First Reading:** {fr['date']} (`{fr['file']}`){presented}"
                )
            moved = f" — moved by {sr['moved_by']}" if sr.get("moved_by") else ""
            lines.append(
                f"- **Second Reading:** {sr['date']} (`{sr['file']}`){moved}"
            )
            continued = entry.get("continued_on") or []
            if continued:
                extra = ", ".join(
                    f"{item['date']} ({item.get('event') or 'resumed'}, `{item['file']}`)"
                    for item in continued
                )
                lines.append(f"- **Continued:** {extra}")
            if sr.get("opening"):
                lines.append(f"- **Opening:** {sr['opening']}")
            lines.append("")

    if pending:
        lines.append("## Introduced, awaiting Second Reading in this corpus")
        lines.append("")
        for entry in pending:
            fr = entry["first_reading"]
            lines.append(f"### {entry['title']}")
            lines.append("")
            lines.append(f"- **Kind:** {KIND_LABEL.get(entry['kind'], entry['kind'])}")
            if entry.get("long_title"):
                lines.append(f"- **Purpose (long title):** {entry['long_title']}")
            presented = f" — presented by {fr['presented_by']}" if fr.get("presented_by") else ""
            lines.append(
                f"- **First Reading:** {fr['date']} (`{fr['file']}`){presented}"
            )
            lines.append("")

    with open(MD_OUT, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


def main():
    first_readings, second_readings = scan_corpus()
    print(
        f"extracted first_readings={len(first_readings)} "
        f"second_readings={len(second_readings)}"
    )
    entries, pending = match_readings(first_readings, second_readings)
    kind_counts = defaultdict(int)
    for entry in entries:
        kind_counts[entry["kind"]] += 1
    payload = {
        "generated": date.today().isoformat(),
        "parser_version": PARSER_VERSION,
        "source": DATA_DIR,
        "coverage": {
            "from": min((item["date"] for item in second_readings), default=""),
            "to": max((item["date"] for item in second_readings), default=""),
            "second_readings": len(entries),
            "pending_first_readings": len(pending),
            "by_kind": dict(sorted(kind_counts.items())),
        },
        "second_readings": entries,
        "pending_second_reading": pending,
    }
    os.makedirs(os.path.dirname(JSON_OUT), exist_ok=True)
    with open(JSON_OUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    stats = payload["coverage"]
    write_markdown(entries, pending, stats)
    print(f"wrote {JSON_OUT}")
    print(f"wrote {MD_OUT}")
    print("coverage", json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
