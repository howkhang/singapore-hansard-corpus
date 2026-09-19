"""Modern JSON adapter: preserve takes and extra collections."""

from __future__ import annotations

import json

from hansard_norm import (
    LOCATOR_UNIT_JSON_FIELDS,
    LOCATOR_UNIT_JSON_OBJECT,
    LOCATOR_UNIT_JSON_STRING,
    PARSER_VERSION,
    SCHEMA_VERSION,
)
from hansard_norm.spans import (
    MODERN_SECTION_TYPE,
    classify_second_reading_event,
    collapse_space,
    display_title,
    extract_moved_by,
    parse_legislature,
    strip_html,
)
from hansard_report import sha256_text


def _meta_field(value, origin="source"):
    if value in (None, "", "null"):
        return {"value": None, "origin": "absent"}
    return {"value": value, "origin": origin}


def _speakers(text):
    import re

    found = []
    for match in re.finditer(
        r"<strong>([^<]{3,160})</strong>\s*:",
        text or "",
        re.I,
    ):
        raw = collapse_space(match.group(1))
        if raw and raw not in found:
            found.append(raw)
    if found:
        return [{"raw": name, "origin": "inferred"} for name in found[:12]]
    mover = extract_moved_by(strip_html(text or ""))
    if mover:
        return [{"raw": mover, "origin": "inferred"}]
    return []


def _bp_type(text):
    event = classify_second_reading_event(text)
    if event == "opening":
        return "second_reading_opening"
    if event == "resumed":
        return "second_reading_resumed"
    if event == "completion":
        return "second_reading_completion"
    lowered = (text or "").lower()
    if "committee of the whole" in lowered:
        return "committee_stage"
    if "third time" in lowered or "third reading" in lowered:
        return "third_reading"
    return "bill_proceeding_unclassified"


def _string_section(
    sitting,
    collection,
    index,
    heading,
    ptype,
    status,
    source_string,
    field_path,
    source_path,
    uncertainty=None,
    speakers=None,
):
    text = strip_html(source_string or "")
    return {
        "section_id": f"sitting:{sitting}:{collection}:{index}",
        "ordinal": None,
        "heading": heading or "",
        "normalized_title": display_title(heading) if heading else "",
        "proceeding_type": ptype,
        "proceeding_status": status,
        "text": text,
        "speakers": speakers or [],
        "locator": {
            "unit": LOCATOR_UNIT_JSON_STRING,
            "field": field_path,
            "start": 0,
            "end": len(source_string or ""),
            "path": source_path,
            "source_fragment_sha256": sha256_text(source_string or ""),
            "normalized_text_sha256": sha256_text(text),
        },
        "uncertainty": uncertainty or [],
        "stability": (
            "section_id is sitting:{date}:{collection}:{original_index} "
            "for this parser version"
        ),
    }


def _object_section(
    sitting,
    collection,
    index,
    heading,
    ptype,
    status,
    obj,
    field_path,
    source_path,
    uncertainty=None,
):
    from hansard_report import dumps_json_object

    canonical = dumps_json_object(obj)
    return {
        "section_id": f"sitting:{sitting}:{collection}:{index}",
        "ordinal": None,
        "heading": heading or "",
        "normalized_title": display_title(heading) if heading else "",
        "proceeding_type": ptype,
        "proceeding_status": status,
        "text": canonical,
        "speakers": [],
        "locator": {
            "unit": LOCATOR_UNIT_JSON_OBJECT,
            "field": field_path,
            "path": source_path,
            "source_fragment_sha256": sha256_text(canonical),
            "normalized_text_sha256": sha256_text(canonical),
        },
        "uncertainty": uncertainty or [],
        "stability": (
            "section_id is sitting:{date}:{collection}:{original_index} "
            "for this parser version"
        ),
    }


def _fields_section(
    sitting,
    collection,
    index,
    heading,
    ptype,
    status,
    field_items,
    source_path,
    uncertainty=None,
    speakers=None,
):
    field_locs = []
    text_parts = []
    source_parts = []
    for item in field_items:
        source_string = item.get("source_string") or ""
        source_parts.append(source_string)
        text_parts.append(strip_html(source_string) if item.get("strip", True) else source_string)
        field_locs.append(
            {
                "field": item["field"],
                "start": 0,
                "end": len(source_string),
                "source_fragment_sha256": sha256_text(source_string),
            }
        )
    text = "\n".join(part for part in text_parts if part)
    return {
        "section_id": f"sitting:{sitting}:{collection}:{index}",
        "ordinal": None,
        "heading": heading or "",
        "normalized_title": display_title(heading) if heading else "",
        "proceeding_type": ptype,
        "proceeding_status": status,
        "text": text,
        "speakers": speakers or [],
        "locator": {
            "unit": LOCATOR_UNIT_JSON_FIELDS,
            "fields": field_locs,
            "path": source_path,
            "source_fragment_sha256": sha256_text("".join(source_parts)),
            "normalized_text_sha256": sha256_text(text),
        },
        "uncertainty": uncertainty or [],
        "stability": (
            "section_id is sitting:{date}:{collection}:{original_index} "
            "for this parser version"
        ),
    }


def normalize_modern(filename, data, source_path, source_sha256, capture=None):
    sitting = filename[: -len(".json")]
    meta = data.get("metadata") or {}
    parl, session = parse_legislature(data)
    sections = []

    def add(section):
        section["ordinal"] = len(sections)
        sections.append(section)

    for index, item in enumerate(data.get("attendanceList") or []):
        add(
            _object_section(
                sitting,
                "attendance",
                index,
                "Attendance",
                "attendance",
                "classified",
                item,
                f"attendanceList.{index}",
                source_path,
            )
        )
    for index, item in enumerate(data.get("ptbaList") or []):
        add(
            _object_section(
                sitting,
                "ptba",
                index,
                item.get("mpName") or "Permission to be absent",
                "permission_to_be_absent",
                "classified",
                item,
                f"ptbaList.{index}",
                source_path,
            )
        )
    for index, item in enumerate(data.get("a2bList") or []):
        add(
            _object_section(
                sitting,
                "a2b",
                index,
                (item.get("bill") or "Assent to bills").strip(),
                "assent_to_bills",
                "classified",
                item,
                f"a2bList.{index}",
                source_path,
            )
        )

    for index, take in enumerate(data.get("takesSectionVOList") or []):
        raw_type = take.get("sectionType") or ""
        heading = take.get("title") or ""
        content = take.get("content") or ""
        if raw_type == "BP":
            ptype = _bp_type(strip_html(content))
            status = "classified" if ptype != "bill_proceeding_unclassified" else "unresolved"
            uncertainty = [] if status == "classified" else ["bill_proceeding_unclassified"]
            lowered = strip_html(content).lower()
            if ptype.startswith("second_reading") and (
                "committee of the whole" in lowered or "third time" in lowered
            ):
                uncertainty.append("later_stages_may_be_bundled_in_same_take")
        else:
            ptype = MODERN_SECTION_TYPE.get(raw_type, "unresolved")
            status = "classified" if ptype != "unresolved" else "unresolved"
            uncertainty = [] if ptype != "unresolved" else [f"unknown_sectionType:{raw_type}"]
        field_items = [
            {
                "field": f"takesSectionVOList.{index}.content",
                "source_string": content,
            }
        ]
        for key in ("subTitle", "clarificationText", "clarificationTitle"):
            value = take.get(key)
            if value:
                field_items.append(
                    {
                        "field": f"takesSectionVOList.{index}.{key}",
                        "source_string": str(value),
                    }
                )
        if len(field_items) == 1:
            add(
                _string_section(
                    sitting,
                    "takes",
                    index,
                    heading,
                    ptype,
                    status,
                    content,
                    f"takesSectionVOList.{index}.content",
                    source_path,
                    uncertainty=uncertainty,
                    speakers=_speakers(content),
                )
            )
        else:
            add(
                _fields_section(
                    sitting,
                    "takes",
                    index,
                    heading,
                    ptype,
                    status,
                    field_items,
                    source_path,
                    uncertainty=uncertainty,
                    speakers=_speakers(content),
                )
            )

    for index, item in enumerate(data.get("annexureList") or []):
        add(
            _object_section(
                sitting,
                "annexure",
                index,
                item.get("annexureTitle") or item.get("fileName") or "Annexure",
                "annexure",
                "classified",
                item,
                f"annexureList.{index}",
                source_path,
                uncertainty=["attachment_bytes_not_in_source_json"],
            )
        )
    for index, item in enumerate(data.get("vernacularList") or []):
        add(
            _object_section(
                sitting,
                "vernacular",
                index,
                item.get("vernacularTitle") or item.get("fileName") or "Vernacular speech",
                "vernacular",
                "classified",
                item,
                f"vernacularList.{index}",
                source_path,
                uncertainty=["non_english_attachment_bytes_not_in_source_json"],
            )
        )
    for collection, field, ptype in (
        ("writtenAnswersVOList", "writtenAnswersVOList", "written_answers"),
        ("writtenAnsNAVOList", "writtenAnsNAVOList", "written_answers_not_orally_asked"),
    ):
        for index, item in enumerate(data.get(field) or []):
            if isinstance(item, dict):
                add(
                    _object_section(
                        sitting,
                        collection,
                        index,
                        item.get("title") or ptype,
                        ptype,
                        "classified",
                        item,
                        f"{field}.{index}",
                        source_path,
                    )
                )
            else:
                add(
                    _string_section(
                        sitting,
                        collection,
                        index,
                        ptype,
                        ptype,
                        "classified",
                        str(item),
                        f"{field}.{index}",
                        source_path,
                    )
                )

    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "report_id": f"sitting:{sitting}",
        "sitting_date": sitting,
        "source": {
            "format": "modern",
            "path": source_path,
            "sha256": source_sha256,
            "locator_unit": "mixed_json_locators",
        },
        "legislature": {
            "parliament": _meta_field(parl),
            "session": _meta_field(session),
            "volume": _meta_field(meta.get("volumeNO")),
            "sitting_no": _meta_field(meta.get("sittingNO")),
        },
        "capture": capture
        or {
            "captured_at": None,
            "http_status": None,
            "raw_sha256": None,
            "note": "unknown for historical reserialized files",
        },
        "sections": sections,
        "coverage": {
            "source_chars": None,
            "mapped_chars": None,
            "note": "modern coverage is by original collection membership, not a single character tape",
        },
    }
