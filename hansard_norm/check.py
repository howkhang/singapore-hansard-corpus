"""Validate normalized reports against source files."""

from __future__ import annotations

import json
import os

from hansard_norm import PARSER_VERSION, SCHEMA_VERSION
from hansard_norm.store import report_path
from hansard_report import load_validated_report, sha256_bytes, sha256_text, sitting_filenames
from datetime import date as date_cls


def resolve_field(data, path):
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def reconstruct_source_fragment(data, locator):
    unit = locator.get("unit")
    if unit == "htmlFullContent_unicode_offset":
        html = data.get("htmlFullContent") or ""
        start, end = locator.get("start"), locator.get("end")
        if start is None or end is None:
            raise ValueError("html locator missing start/end")
        return html[start:end]
    if unit == "json_string":
        value = resolve_field(data, locator["field"])
        if not isinstance(value, str):
            raise ValueError(f"{locator['field']} is not a string")
        start = locator.get("start", 0)
        end = locator.get("end", len(value))
        return value[start:end]
    if unit == "json_object":
        value = resolve_field(data, locator["field"])
        from hansard_report import dumps_json_object

        return dumps_json_object(value)
    if unit == "json_fields":
        parts = []
        for item in locator.get("fields") or []:
            value = resolve_field(data, item["field"])
            if not isinstance(value, str):
                raise ValueError(f"{item['field']} is not a string")
            start = item.get("start", 0)
            end = item.get("end", len(value))
            parts.append(value[start:end])
        return "".join(parts)
    raise ValueError(f"unknown locator unit {unit!r}")


def check_report(path, json_dir):
    with open(path, encoding="utf-8") as handle:
        report = json.load(handle)
    errors = []
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema {report.get('schema_version')}")
    sitting = report.get("sitting_date")
    source = os.path.join(json_dir, f"{sitting}.json")
    if not os.path.isfile(source):
        return ["missing source"], report
    expected = date_cls.fromisoformat(sitting)
    data, fmt = load_validated_report(source, expected)
    with open(source, "rb") as handle:
        digest = sha256_bytes(handle.read())
    if report["source"]["sha256"] != digest:
        errors.append("source hash mismatch")
    if report["source"]["format"] != fmt:
        errors.append("format mismatch")
    sections = report.get("sections") or []
    if not sections:
        errors.append("no sections")
    seen = []
    for section in sections:
        loc = section.get("locator") or {}
        sid = section.get("section_id")
        text = section.get("text") or ""
        if sha256_text(text) != loc.get("normalized_text_sha256"):
            errors.append(f"{sid}: normalized_text_sha256 != sha256(text)")
        try:
            fragment = reconstruct_source_fragment(data, loc)
        except Exception as exc:
            errors.append(f"{sid}: source reconstruct {exc}")
            continue
        if sha256_text(fragment) != loc.get("source_fragment_sha256"):
            errors.append(f"{sid}: source_fragment_sha256 mismatch")
        if loc.get("unit") == "htmlFullContent_unicode_offset":
            start, end = loc.get("start"), loc.get("end")
            if start is None or end is None or end < start:
                errors.append(f"{sid}: bad span")
            else:
                for other in seen:
                    if start < other[1] and end > other[0]:
                        errors.append(f"{sid}: overlap")
                seen.append((start, end))
    return errors, report


def validate_snapshot(out_dir, json_dir, require_complete=True):
    snapshot_path = os.path.join(out_dir, "SNAPSHOT.json")
    if not os.path.isfile(snapshot_path):
        raise SystemExit(f"missing {snapshot_path}")
    with open(snapshot_path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    errors = []
    if snapshot.get("failures"):
        errors.append(f"snapshot records {len(snapshot['failures'])} failures")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        errors.append("snapshot schema mismatch")
    names = sitting_filenames(json_dir)
    inv = {item["sitting_date"]: item for item in snapshot.get("inventory") or []}
    source_dates = {name[: -len(".json")] for name in names}
    if require_complete:
        extra = set(inv) - source_dates
        missing = source_dates - set(inv)
        if extra:
            errors.append(f"extra normalized sittings {sorted(extra)[:5]}")
        if missing:
            errors.append(f"missing normalized sittings {len(missing)}")
    for sitting, item in inv.items():
        path = report_path(out_dir, sitting)
        if not os.path.isfile(path):
            errors.append(f"missing report file {sitting}")
            continue
        with open(path, "rb") as handle:
            digest = sha256_bytes(handle.read())
        if digest != item.get("normalized_sha256"):
            errors.append(f"{sitting}: inventory hash mismatch")
        local, report = check_report(path, json_dir)
        errors.extend(f"{sitting}: {err}" for err in local)
        if item.get("sections") != len(report.get("sections") or []):
            errors.append(f"{sitting}: inventory section count mismatch")
    complete = not errors and not snapshot.get("failures")
    if require_complete:
        complete = complete and source_dates <= set(inv)
    return errors, snapshot, complete
