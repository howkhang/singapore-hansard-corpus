#!/usr/bin/env python3
"""Build an index of Second Reading debates from the Hansard JSON corpus."""

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from html import unescape

DATA_DIR = "./data/hansard/json"
JSON_OUT = "./data/hansard/second_readings_index.json"
MD_OUT = "./data/hansard/second_readings_index.md"

QUOTE_CHARS = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\xa0": " ",
}


def strip_html(html):
    if not html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n", text)
    text = re.sub(r"(?i)<strong>", "", text)
    text = re.sub(r"(?i)</strong>", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    for src, dst in QUOTE_CHARS.items():
        text = text.replace(src, dst)
    text = re.sub(r"\[\(\s*proc text\s*\)\s*", "", text, flags=re.I)
    text = re.sub(r"\s*\(\s*proc text\s*\)\]", "", text, flags=re.I)
    text = re.sub(r"Page:\s*\d+", " ", text)
    text = re.sub(r"Column(?: No)?\s*:?\s*\d+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_title(title):
    text = strip_html(title or "")
    text = text.upper()
    text = text.replace("NO.", "NO")
    text = re.sub(r"\s*\(NO\s*(\d+)\)", r" (NO \1)", text)
    text = re.sub(r"[.\s]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text and "BILL" not in text:
        text = f"{text} BILL"
    return text


def display_title(title):
    text = collapse_space(strip_html(title or ""))
    letters = re.sub(r"[^A-Za-z]", "", text)
    if letters and letters.isupper() and len(letters) > 4:
        text = text.title()
        text = text.replace("'S", "'s")
        for word in ("Of", "And", "The", "For", "To", "In", "On"):
            text = re.sub(rf"\b{word}\b", word.lower(), text)
        text = text[0].upper() + text[1:]
        text = re.sub(r"\bNo\b", "No.", text)
    text = re.sub(r"\s+bill$", " Bill", text, flags=re.I)
    return text


def classify(title, long_title=""):
    title_u = (title or "").upper()
    if re.search(r"\bSUPPLY\b", title_u) or "APPROPRIATION" in title_u:
        return "supply"
    if "MISCELLANEOUS AMENDMENT" in title_u:
        return "miscellaneous_amendment"
    if re.search(r"\(AMENDMENT", title_u) or " AMENDMENT BILL" in title_u:
        return "amendment"
    if re.search(r"\(ABOLITION", title_u) or re.search(r"\(REPEAL", title_u):
        return "repeal"
    return "new_act"


KIND_LABEL = {
    "new_act": "New Act",
    "amendment": "Amendment",
    "miscellaneous_amendment": "Miscellaneous amendment",
    "repeal": "Repeal / abolition",
    "supply": "Supply",
}


def collapse_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def extract_long_title(text):
    text = collapse_space(text)
    match = re.search(r'"((?:to |for )[^"]{8,1200})"', text, re.I)
    if match:
        return collapse_space(match.group(1)).rstrip(" .,")
    match = re.search(
        r"((?:to |for )(?:amend|provide|establish|govern|give effect|"
        r"reconstitute|protect|make|repeal|implement|create|enable|"
        r"introduce|set out)[^.]{8,800})",
        text,
        re.I,
    )
    if match:
        return collapse_space(match.group(1)).rstrip(" .,")
    return ""


def extract_presented_by(text):
    text = collapse_space(text)
    match = re.search(
        r"presented by\s+(.+?)(?:;|,?\s*read the First time)",
        text,
        re.I,
    )
    if not match:
        return ""
    return collapse_space(match.group(1)).strip(" ;,")


def extract_moved_by(text):
    match = re.search(
        r"((?:The |Acting |Senior |Deputy |Second |Coordinating )"
        r"[^\n:]{5,180}?)\s*:\s*"
        r"(?:Mr Speaker|Madam|Sir|Mr Deputy Speaker)?"
        r"[^.]{0,180}?"
        r"(?:on behalf of [^,]+,\s*)?(?:I (?:beg to |now )?move)",
        text,
        re.I | re.S,
    )
    if not match:
        return ""
    mover = collapse_space(match.group(1))
    mover = re.sub(r"^\d+\.\d+\s*(?:am|pm)\s*", "", mover, flags=re.I)
    return mover.strip(" .")


def extract_opening(text, limit=900):
    match = re.search(
        r'(?:That |The )?the Bill be now read a Second time\.?"?\s*(.*)',
        text,
        re.I | re.S,
    )
    body = match.group(1) if match else text
    stop = re.search(
        r"\n(?:Mr|Mrs|Ms|Miss|Dr|Prof\.?|Assoc\.? Prof\.?|The Honourable) "
        r"[A-Z][^\n:]{2,80}:",
        body,
    )
    if stop and stop.start() > 60:
        body = body[: stop.start()]
    body = collapse_space(body)
    body = re.sub(
        r"^(?:Order for Second Reading read\.?\s*)?"
        r"(?:Mr Speaker|Madam|Sir|Mr Deputy Speaker)[^.]{0,80}\.\s*"
        r"(?:\d+\.\d+\s*(?:am|pm)\s*)?",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        r"^(?:The |Acting |Senior |Deputy |Second |Coordinating )[^:]{5,180}:\s*",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        r"^(?:Mr Speaker|Madam|Sir|Mr Deputy Speaker)[,.]?\s*(?:Sir[,.]?\s*)?",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(r"^on behalf of [^,]+,\s*", "", body, flags=re.I)
    body = re.sub(
        r"^I (?:beg to |now )?move,?\s*\"[^\"]{0,80}\"\.?\s*",
        "",
        body,
        flags=re.I,
    )
    body = collapse_space(body).lstrip(" .")
    if len(body) <= limit:
        return body
    cut = body[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def sitting_date_from_name(filename):
    return filename[: -len(".json")]


def parse_parliament(data, html=""):
    meta = data.get("metadata") or {}
    for key in ("parlimentNO", "parliamentNO", "parliamentNo"):
        value = meta.get(key)
        if value not in (None, "", "null"):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    value = data.get("parlNo")
    if value not in (None, "", "null"):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    if html:
        match = re.search(
            r"<Meta name=['\"]Parl_No['\"] content=['\"](\d+)['\"]",
            html,
            re.I,
        )
        if match:
            return int(match.group(1))
    return None


def relative_file(filename):
    return f"data/hansard/json/{filename}"


def is_second_reading_text(text):
    if re.search(r"Question again proposed", text, re.I) and (
        len(text) < 500
        or not re.search(r"I (?:beg to |now )?move", text, re.I)
    ):
        return False
    return bool(
        re.search(r"Order for Second Reading read", text, re.I)
        or re.search(r"be now read a Second time", text, re.I)
    )


def parse_modern(filename, data):
    first, second = [], []
    sitting = sitting_date_from_name(filename)
    parl = parse_parliament(data)
    source = relative_file(filename)
    for take in data.get("takesSectionVOList") or []:
        section = (take.get("sectionType") or "").strip()
        title = (take.get("title") or "").strip()
        if not title:
            continue
        content = take.get("content") or ""
        text = strip_html(content)
        if section == "BI":
            first.append(
                {
                    "title": display_title(title),
                    "norm": normalize_title(title),
                    "date": sitting,
                    "file": source,
                    "parliament": parl,
                    "long_title": extract_long_title(text),
                    "presented_by": extract_presented_by(text),
                    "source": "json",
                }
            )
        elif section == "BP" and is_second_reading_text(text):
            second.append(
                {
                    "title": display_title(title),
                    "norm": normalize_title(title),
                    "date": sitting,
                    "file": source,
                    "parliament": parl,
                    "moved_by": extract_moved_by(text),
                    "opening": extract_opening(text),
                    "source": "json",
                }
            )
    return first, second


FIRST_READING_RE = re.compile(
    r"(?:^|\n)\s*([A-Z][A-Z0-9 ,.'()/\-&]{4,140} BILL)\s+"
    r'"((?:to |for )[^"]{8,1200})"\s*[,.]?\s*'
    r"(?:recommendation of President signified\s*;?\s*)?"
    r"presented by\s+(.+?);\s*read the First time",
    re.S,
)


def parse_legacy_html(filename, data):
    html = data.get("htmlFullContent") or ""
    if not html:
        return [], []
    sitting = sitting_date_from_name(filename)
    parl = parse_parliament(data, html)
    source = relative_file(filename)
    text = strip_html(html)

    first = []
    seen_first = set()
    for match in FIRST_READING_RE.finditer(text):
        raw_title = collapse_space(match.group(1))
        norm = normalize_title(raw_title)
        key = (norm, sitting)
        if key in seen_first:
            continue
        seen_first.add(key)
        first.append(
            {
                "title": display_title(raw_title),
                "norm": norm,
                "date": sitting,
                "file": source,
                "parliament": parl,
                "long_title": collapse_space(match.group(2)).rstrip(" .,"),
                "presented_by": collapse_space(match.group(3)).strip(" ;,"),
                "source": "html",
            }
        )

    second = []
    parts = re.split(r"Order for Second Reading read\.?", text, flags=re.I)
    for index, part in enumerate(parts[1:], 1):
        before = parts[index - 1]
        tail = collapse_space(before[-800:])
        titles = list(
            re.finditer(r"([A-Z][A-Z0-9 ,.'()/\-&]{4,140} BILL)\s*$", tail)
        )
        if not titles:
            titles = list(
                re.finditer(r"([A-Z][A-Z0-9 ,.'()/\-&]{4,140} BILL)", tail)
            )
        if not titles:
            continue
        raw_title = titles[-1].group(1)
        # Ignore "BILLS" catch-alls and very short fragments.
        if raw_title.strip() in {"BILL", "THE BILL"} or raw_title.startswith(")"):
            continue
        if re.match(r"^(AND |OF |THE )", raw_title):
            continue
        after = collapse_space(part[:2500])
        if not re.search(r"Second time", after, re.I):
            # Committee/third-reading remnants; skip.
            if not re.search(r"I beg to move|I move", after, re.I):
                continue
        second.append(
            {
                "title": display_title(raw_title),
                "norm": normalize_title(raw_title),
                "date": sitting,
                "file": source,
                "parliament": parl,
                "moved_by": extract_moved_by(after),
                "opening": extract_opening(after),
                "source": "html",
            }
        )
    return first, second


def group_key(record):
    parl = record.get("parliament")
    return (record["norm"], parl if parl is not None else "unknown")


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
        return (item["norm"], item["date"], item["file"])

    for key, firsts in first_groups.items():
        seconds = second_groups.get(key, [])
        cursor = 0
        for first in firsts:
            while cursor < len(seconds) and seconds[cursor]["date"] < first["date"]:
                cursor += 1
            if cursor < len(seconds):
                second = seconds[cursor]
                matched_second_ids.add(second_id(second))
                entries.append(make_entry(first, second))
                cursor += 1
            else:
                if not re.match(r"^(AMENDMENT|NO \d+)\)", first["norm"]):
                    pending.append(make_pending(first))

    for key, seconds in second_groups.items():
        for second in seconds:
            if second_id(second) in matched_second_ids:
                continue
            # Unmatched second reading: attach a later-or-same first reading
            # from the same title if parliament was unknown on one side.
            entries.append(make_entry(None, second))

    entries.sort(key=lambda item: (item["second_reading"]["date"], item["title"]))
    pending.sort(key=lambda item: (item["first_reading"]["date"], item["title"]))
    return merge_continuations(entries), pending


def merge_continuations(entries):
    groups = defaultdict(list)
    for entry in entries:
        key = (normalize_title(entry["title"]), entry.get("parliament"))
        groups[key].append(entry)

    merged = []
    for items in groups.values():
        items.sort(key=lambda item: item["second_reading"]["date"])
        current = None
        for entry in items:
            if current is None:
                current = entry
                continue
            first = entry.get("first_reading") or {}
            prev_date = current["second_reading"]["date"]
            if not first or first.get("date", "") <= prev_date:
                current.setdefault("continued_on", []).append(
                    {
                        "date": entry["second_reading"]["date"],
                        "file": entry["second_reading"]["file"],
                    }
                )
            else:
                merged.append(current)
                current = entry
        if current:
            merged.append(current)
    merged.sort(key=lambda item: (item["second_reading"]["date"], item["title"]))
    return [
        item
        for item in merged
        if not re.search(
            r"Question again proposed",
            item["second_reading"].get("opening") or "",
            re.I,
        )
    ]


def make_entry(first, second):
    title = (first or second)["title"]
    long_title = (first or {}).get("long_title") or ""
    kind = classify(title, long_title)
    parliament = (second or first).get("parliament")
    if first and first.get("parliament") is not None:
        parliament = first["parliament"]
    entry = {
        "title": title,
        "kind": kind,
        "long_title": long_title,
        "parliament": parliament,
        "first_reading": None,
        "second_reading": {
            "date": second["date"],
            "file": second["file"],
            "moved_by": second.get("moved_by") or "",
            "opening": second.get("opening") or "",
        },
    }
    if first:
        entry["first_reading"] = {
            "date": first["date"],
            "file": first["file"],
            "presented_by": first.get("presented_by") or "",
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
        "first_reading": {
            "date": first["date"],
            "file": first["file"],
            "presented_by": first.get("presented_by") or "",
        },
        "second_reading": None,
    }


def scan_corpus():
    first_readings = []
    second_readings = []
    names = sorted(
        name for name in os.listdir(DATA_DIR) if name.endswith(".json")
    )
    for index, name in enumerate(names, 1):
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
                    f"{item['date']} (`{item['file']}`)" for item in continued
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
