"""Legacy HTML adapter: partition htmlFullContent into ordered sections."""

from __future__ import annotations

import re

from hansard_norm import LOCATOR_UNIT_HTML, PARSER_VERSION, SCHEMA_VERSION
from hansard_norm.spans import (
    FIRST_READING_RE,
    bill_title_from_prefix,
    classify_html_marker,
    collapse_space,
    display_title,
    extract_moved_by,
    extract_presented_by,
    find_html_offset,
    html_marker_spans,
    parse_legislature,
    passage_from_html,
    strip_html,
)
from hansard_report import sha256_text


def _meta_field(value, origin="source"):
    if value in (None, "", "null"):
        return {"value": None, "origin": "absent"}
    return {"value": value, "origin": origin}


def _speakers(text):
    found = []
    for match in re.finditer(
        r"((?:The |Acting |Senior |Deputy |Mr |Mrs |Ms |Dr |Prof )[^:\n]{3,160}):",
        text or "",
    ):
        raw = collapse_space(match.group(1))
        if raw and raw not in found:
            found.append(raw)
    return [{"raw": name, "origin": "inferred"} for name in found[:12]]


def _section(
    *,
    sitting_date,
    source_path,
    ordinal,
    start,
    end,
    heading,
    proceeding_type,
    proceeding_status,
    text,
    html,
    uncertainty=None,
    speakers=None,
):
    fragment = html[start:end] if html is not None else ""
    return {
        "section_id": f"sitting:{sitting_date}:htmlspan:{start}",
        "ordinal": ordinal,
        "heading": heading or "",
        "normalized_title": display_title(heading) if heading else "",
        "proceeding_type": proceeding_type,
        "proceeding_status": proceeding_status,
        "text": text or "",
        "speakers": speakers or [],
        "locator": {
            "unit": LOCATOR_UNIT_HTML,
            "start": start,
            "end": end,
            "path": source_path,
            "source_fragment_sha256": sha256_text(fragment),
            "normalized_text_sha256": sha256_text(text or ""),
        },
        "uncertainty": uncertainty or [],
        "stability": (
            "section_id is sitting:{date}:htmlspan:{start} where start is a "
            "unicode offset into decoded htmlFullContent for this parser version"
        ),
    }


def normalize_legacy(filename, data, source_path, source_sha256, capture=None):
    html = data.get("htmlFullContent") or ""
    sitting = filename[: -len(".json")]
    parl, session = parse_legislature(data, html)
    volume = None
    sitting_no = None
    vol = re.search(r"<Meta name=['\"]Vol_No['\"] content=['\"](\d+)['\"]", html, re.I)
    if vol:
        volume = int(vol.group(1))
    sitn = re.search(r"<Meta name=['\"]Sit_No['\"] content=['\"](\d+)['\"]", html, re.I)
    if sitn:
        sitting_no = int(sitn.group(1))

    cuts = []
    for start, end, marker_html in html_marker_spans(html):
        event = classify_html_marker(marker_html)
        if not event:
            continue
        ptype = {
            "opening": "second_reading_opening",
            "resumed": "second_reading_resumed",
            "completion": "second_reading_completion",
        }.get(event, "unresolved")
        cuts.append((start, "second_reading", ptype, marker_html))

    first_cursor = 0
    for match in FIRST_READING_RE.finditer(strip_html(html)):
        raw_title = collapse_space(match.group(1))
        title_off = find_html_offset(html, raw_title, after=first_cursor)
        first_time_off = find_html_offset(
            html,
            "read the First time",
            after=title_off if title_off is not None else first_cursor,
        )
        start = title_off if title_off is not None else first_time_off
        if start is None:
            continue
        first_cursor = start + 1
        cuts.append((start, "first_reading", "first_reading", raw_title))

    cuts.sort(key=lambda item: (item[0], 0 if item[1] == "first_reading" else 1))
    deduped = []
    seen_starts = set()
    for cut in cuts:
        if cut[0] in seen_starts:
            continue
        seen_starts.add(cut[0])
        deduped.append(cut)
    cuts = deduped

    sections = []
    starts = [c[0] for c in cuts] + [len(html)]
    if not cuts:
        text = passage_from_html(html, 0, len(html))
        sections.append(
            _section(
                sitting_date=sitting,
                source_path=source_path,
                ordinal=0,
                start=0,
                end=len(html),
                heading="",
                proceeding_type="unresolved",
                proceeding_status="unresolved",
                text=text,
                html=html,
                uncertainty=["no_structural_cuts"],
            )
        )
    else:
        if cuts[0][0] > 0:
            text = passage_from_html(html, 0, cuts[0][0])
            sections.append(
                _section(
                    sitting_date=sitting,
                    source_path=source_path,
                    ordinal=0,
                    start=0,
                    end=cuts[0][0],
                    heading="",
                    proceeding_type="sitting_front_matter",
                    proceeding_status="preserved",
                    text=text,
                    html=html,
                    uncertainty=["front_matter_includes_headers_and_prior_business"],
                )
            )
        for index, cut in enumerate(cuts):
            start = cut[0]
            end = starts[index + 1]
            kind, ptype, extra = cut[1], cut[2], cut[3]
            text = passage_from_html(html, start, end)
            prefix = strip_html(html[starts[index - 1] if index else 0 : start])
            heading = extra if kind == "first_reading" else bill_title_from_prefix(prefix)
            if kind == "second_reading" and not heading:
                ptype = "unresolved"
                status = "unresolved"
                uncertainty = ["title_not_recovered"]
            else:
                status = "classified"
                uncertainty = []
            if kind == "first_reading":
                presented = extract_presented_by(text)
                if presented:
                    uncertainty.append("presented_by_inferred")
            speakers = _speakers(text)
            if kind == "second_reading" and extract_moved_by(text):
                speakers = speakers or [
                    {"raw": extract_moved_by(text), "origin": "inferred"}
                ]
            sections.append(
                _section(
                    sitting_date=sitting,
                    source_path=source_path,
                    ordinal=len(sections),
                    start=start,
                    end=end,
                    heading=heading or "",
                    proceeding_type=ptype,
                    proceeding_status=status,
                    text=text,
                    html=html,
                    uncertainty=uncertainty,
                    speakers=speakers,
                )
            )

    for ordinal, section in enumerate(sections):
        section["ordinal"] = ordinal

    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "report_id": f"sitting:{sitting}",
        "sitting_date": sitting,
        "source": {
            "format": "legacy",
            "path": source_path,
            "sha256": source_sha256,
            "locator_unit": LOCATOR_UNIT_HTML,
        },
        "legislature": {
            "parliament": _meta_field(parl),
            "session": _meta_field(session),
            "volume": _meta_field(volume),
            "sitting_no": _meta_field(sitting_no),
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
            "source_chars": len(html),
            "mapped_chars": sum(
                section["locator"]["end"] - section["locator"]["start"]
                for section in sections
            ),
        },
    }
