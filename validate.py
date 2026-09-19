#!/usr/bin/env python3
"""Check index metadata consistency against the sitting files."""

import json
import os
import sys
from collections import Counter

from hansard_report import load_validated_report, sitting_date_from_filename, sitting_filenames

DATA_DIR = "./data/hansard/json"
INDEX_PATH = "./data/hansard/second_readings_index.json"


def main():
    errors = []
    if not os.path.isfile(INDEX_PATH):
        raise SystemExit(f"missing {INDEX_PATH}")
    with open(INDEX_PATH, encoding="utf-8") as handle:
        index = json.load(handle)
    entries = index.get("second_readings") or []
    pending = index.get("pending_second_reading") or []
    coverage = index.get("coverage") or {}
    if coverage.get("second_readings") != len(entries):
        errors.append(
            f"coverage.second_readings={coverage.get('second_readings')} "
            f"but entries={len(entries)}"
        )
    kinds = Counter(entry.get("kind") for entry in entries)
    reported = coverage.get("by_kind") or {}
    if dict(sorted(kinds.items())) != dict(sorted(reported.items())):
        errors.append(f"by_kind mismatch: entries={dict(kinds)} coverage={reported}")
    if coverage.get("pending_first_readings") != len(pending):
        errors.append(
            f"pending count {coverage.get('pending_first_readings')} != {len(pending)}"
        )

    long_span = []
    for entry in entries:
        start = (entry.get("second_reading") or {}).get("date")
        for cont in entry.get("continued_on") or []:
            if cont.get("link_confidence") != "explicit_resumption":
                errors.append(f"{entry.get('title')}: continuation without explicit resumption")
            if start and cont.get("date") and int(cont["date"][:4]) - int(start[:4]) >= 2:
                long_span.append((entry.get("title"), start, cont.get("date")))
    if long_span:
        print(
            f"NOTE: {len(long_span)} explicit resumptions span at least two calendar years"
        )

    offset_titles = {}
    for entry in entries:
        sr = entry.get("second_reading") or {}
        loc = sr.get("locator") or {}
        if loc.get("format") != "legacy":
            continue
        if loc.get("html_offset") is None:
            errors.append(
                f"{entry.get('title')} {sr.get('date')}: missing html_offset"
            )
        key = (sr.get("file"), loc.get("html_offset"))
        offset_titles.setdefault(key, set()).add(entry.get("title"))
        for cont in entry.get("continued_on") or []:
            cloc = cont.get("locator") or {}
            if cloc.get("format") == "legacy" and cloc.get("html_offset") is None:
                errors.append(
                    f"{entry.get('title')} continuation {cont.get('date')}: missing html_offset"
                )
            ckey = (cont.get("file"), cloc.get("html_offset"))
            offset_titles.setdefault(ckey, set()).add(entry.get("title"))
    collisions = {
        key: titles
        for key, titles in offset_titles.items()
        if key[1] is not None and len(titles) > 1
    }
    if collisions:
        sample = next(iter(collisions.items()))
        errors.append(
            f"{len(collisions)} legacy file/offset groups mix Bill titles "
            f"(example {sample[0]} -> {sorted(sample[1])[:4]})"
        )

    if os.path.isdir(DATA_DIR):
        names = sitting_filenames(DATA_DIR)
        modern = legacy = invalid = 0
        for name in names:
            sitting = sitting_date_from_filename(name)
            path = os.path.join(DATA_DIR, name)
            try:
                _data, fmt = load_validated_report(path, sitting)
            except ValueError:
                invalid += 1
                continue
            if fmt == "modern":
                modern += 1
            else:
                legacy += 1
        print(f"sittings modern={modern} legacy={legacy} invalid={invalid}")
        if invalid:
            errors.append(f"{invalid} sitting files failed validation")

    if errors:
        for item in errors:
            print("ERROR:", item)
        raise SystemExit(1)
    print(
        f"OK index entries={len(entries)} pending={len(pending)} "
        f"parser_version={index.get('parser_version')}"
    )


if __name__ == "__main__":
    main()
