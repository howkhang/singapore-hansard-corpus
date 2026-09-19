"""Normalize one sitting payload into the versioned schema."""

from __future__ import annotations

import json
import os
from datetime import date

from hansard_norm.legacy import normalize_legacy
from hansard_norm.modern import normalize_modern
from hansard_norm.spans import (
    extract_long_title,
    extract_moved_by,
    extract_opening,
    extract_presented_by,
    normalize_title,
    strip_html,
)
from hansard_report import load_validated_report, sitting_date_from_filename, sha256_bytes


def load_capture_sidecar(sitting_path):
    meta_path = sitting_path[: -len(".json")] + ".meta.json"
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path, encoding="utf-8") as handle:
        meta = json.load(handle)
    return {
        "captured_at": meta.get("captured_at"),
        "http_status": meta.get("http_status"),
        "raw_sha256": meta.get("raw_sha256"),
        "note": "sidecar present; hashes identify preserved local bytes",
    }


def canonical_dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def normalize_payload(filename, data, source_path, source_sha256, source_format, capture=None):
    if source_format == "legacy":
        return normalize_legacy(filename, data, source_path, source_sha256, capture)
    if source_format == "modern":
        return normalize_modern(filename, data, source_path, source_sha256, capture)
    raise ValueError(f"unsupported source format {source_format!r}")


def normalize_file(path, source_root="data/hansard/json"):
    name = os.path.basename(path)
    expected = sitting_date_from_filename(name)
    with open(path, "rb") as handle:
        raw = handle.read()
    data, fmt = load_validated_report(path, expected)
    rel = os.path.join(source_root, name).replace("\\", "/")
    capture = load_capture_sidecar(path)
    report = normalize_payload(name, data, rel, sha256_bytes(raw), fmt, capture)
    return report, raw


def records_from_report(report):
    """Index records consumed by the Second Reading matcher."""
    first, second = [], []
    sitting = report["sitting_date"]
    source = report["source"]["path"]
    parl = (report.get("legislature") or {}).get("parliament", {}).get("value")
    session = (report.get("legislature") or {}).get("session", {}).get("value")
    for section in report.get("sections") or []:
        ptype = section.get("proceeding_type")
        heading = section.get("heading") or section.get("normalized_title") or ""
        if not str(heading).strip():
            continue
        text = section.get("text") or ""
        plain = strip_html(text)
        locator = {
            "format": "normalized",
            "file": source,
            "section_id": section.get("section_id"),
            "html_offset": section.get("locator", {}).get("start")
            if report["source"]["format"] == "legacy"
            else None,
            "html_end": section.get("locator", {}).get("end")
            if report["source"]["format"] == "legacy"
            else None,
            "section_index": int(section["section_id"].rsplit(":", 1)[-1])
            if section.get("section_id", "").startswith(f"sitting:{sitting}:takes:")
            else None,
            "unit": section.get("locator", {}).get("unit"),
            "start": section.get("locator", {}).get("start"),
            "end": section.get("locator", {}).get("end"),
            "field": section.get("locator", {}).get("field"),
            "source_fragment_sha256": section.get("locator", {}).get(
                "source_fragment_sha256"
            ),
            "normalized_text_sha256": section.get("locator", {}).get(
                "normalized_text_sha256"
            ),
            "passage_sha256": section.get("locator", {}).get("normalized_text_sha256"),
        }
        base = {
            "title": section.get("normalized_title") or heading,
            "norm": normalize_title(heading or section.get("normalized_title") or ""),
            "date": sitting,
            "file": source,
            "parliament": parl,
            "session": session,
            "locator": locator,
            "section_id": section.get("section_id"),
        }
        if ptype == "first_reading":
            rec = dict(base)
            rec.update(
                {
                    "long_title": extract_long_title(plain),
                    "presented_by": extract_presented_by(plain),
                    "source": report["source"]["format"],
                }
            )
            first.append(rec)
        elif ptype in {
            "second_reading_opening",
            "second_reading_resumed",
            "second_reading_completion",
        }:
            event = {
                "second_reading_opening": "opening",
                "second_reading_resumed": "resumed",
                "second_reading_completion": "completion",
            }[ptype]
            rec = dict(base)
            rec.update(
                {
                    "event": event,
                    "moved_by": extract_moved_by(plain),
                    "opening": extract_opening(plain),
                    "source": report["source"]["format"],
                }
            )
            second.append(rec)
    return first, second
