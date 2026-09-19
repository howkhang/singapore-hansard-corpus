"""Shared Hansard text, title, and legacy span helpers."""

import re
from html import unescape

from hansard_report import sha256_text

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


def _int_field(value):
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_legislature(data, html=""):
    meta = data.get("metadata") or {}
    parl = None
    session = None
    for key in ("parlimentNO", "parliamentNO", "parliamentNo"):
        parl = _int_field(meta.get(key))
        if parl is not None:
            break
    if parl is None:
        parl = _int_field(data.get("parlNo"))
    for key in ("sessionNO", "sessionNo", "session"):
        session = _int_field(meta.get(key))
        if session is not None:
            break
    if html:
        if parl is None:
            match = re.search(
                r"<Meta name=['\"]Parl_No['\"] content=['\"](\d+)['\"]",
                html,
                re.I,
            )
            if match:
                parl = int(match.group(1))
        if session is None:
            match = re.search(
                r"<Meta name=['\"]Sess_No['\"] content=['\"](\d+)['\"]",
                html,
                re.I,
            )
            if match:
                session = int(match.group(1))
    return parl, session


def relative_file(filename):
    return f"data/hansard/json/{filename}"


_HTML_GAP = r"(?:\s|&nbsp;|&NBSP;|&#160;|&#xa0;|&#xA0;|<[^>]+>)+"
HTML_MARKER_RE = re.compile(
    r"Order" + _HTML_GAP + r"for" + _HTML_GAP + r"Second" + _HTML_GAP
    + r"Reading" + _HTML_GAP + r"read\.?|"
    r"Question" + _HTML_GAP + r"again" + _HTML_GAP + r"proposed|"
    r"Debate" + _HTML_GAP + r"resumed\.?|"
    r"Resumption" + _HTML_GAP + r"of" + _HTML_GAP + r"Debate",
    re.I,
)


def find_html_offset(html, marker, after=0):
    if not html or not marker:
        return None
    start = max(0, after)
    region = html[start:]
    direct = region.find(marker)
    if direct >= 0:
        return start + direct
    parts = [
        re.escape(part)
        for part in re.sub(r"\s+", " ", marker).strip().split()
        if part
    ]
    if not parts:
        return None
    pattern = r"(?:\s|<[^>]+>)*".join(parts)
    match = re.search(pattern, region, re.I)
    return start + match.start() if match else None


def html_marker_spans(html):
    spans = []
    for match in HTML_MARKER_RE.finditer(html or ""):
        prefix = collapse_space(
            strip_html(html[max(0, match.start() - 60) : match.start()])
        ).lower()
        if prefix.endswith("for") or prefix.endswith("order read for"):
            continue
        spans.append((match.start(), match.end(), match.group(0)))
    return spans


def classify_html_marker(marker_html):
    text = collapse_space(strip_html(marker_html)).lower().replace("\xa0", " ")
    if "order for second reading" in text:
        return "opening"
    if (
        "question again proposed" in text
        or "debate resumed" in text
        or text.startswith("resumption of debate")
    ):
        return "resumed"
    return None


def _is_title_boundary_token(token):
    if "[" in token or "]" in token:
        return True
    stripped = re.sub(r"[^A-Za-z]", "", token)
    return stripped.upper() in {
        "CHAIR",
        "SPEAKER",
        "DEPUTY",
        "SITTING",
        "RESUMED",
        "SUSPENDED",
        "ACCORDINGLY",
    }


def bill_title_from_prefix(prefix_text):
    tail = collapse_space(prefix_text)[-800:]
    tokens = tail.split()
    bill_index = None
    for index in range(len(tokens) - 1, -1, -1):
        if re.fullmatch(r"[Bb][Ii][Ll][Ll]\.?", tokens[index]):
            bill_index = index
            break
    if bill_index is None:
        return None
    start = bill_index
    while start > 0:
        prev = tokens[start - 1]
        if prev.upper() == "BILLS" and start >= 2 and tokens[start - 2].upper() == "READING":
            break
        if _is_title_boundary_token(prev):
            break
        if re.match(r"^[A-Z(]", prev) or prev in {"&", "-"}:
            start -= 1
            continue
        break
    while start <= bill_index and _is_title_boundary_token(tokens[start]):
        start += 1
    raw_title = " ".join(tokens[start : bill_index + 1])
    raw_title = re.sub(r"\s+", " ", raw_title).strip(" .")
    if raw_title.upper() in {
        "BILL",
        "THE BILL",
        "THIS BILL",
        "THAT BILL",
        "SAID BILL",
        "A BILL",
    } or raw_title.startswith(")"):
        return None
    if raw_title.upper().startswith("CHAIR"):
        return None
    if re.match(r"^(AND |OF |THE )", raw_title.upper()):
        return None
    if "BILL" not in raw_title.upper() or len(raw_title) < 8:
        return None
    return raw_title


def next_filtered_marker_offset(html, after):
    for span_start, _span_end, _marker in html_marker_spans(html):
        if span_start > after:
            return span_start
    return len(html)


def passage_from_html(html, start, end=None):
    if start is None or start < 0:
        return ""
    if end is None or end <= start:
        end = next_filtered_marker_offset(html, start)
    return collapse_space(strip_html(html[start:end]))


def title_core(title):
    text = normalize_title(title)
    text = re.sub(r"\b(BOARD|THE|OF)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def titles_are_variants(left, right):
    return (
        normalize_title(left) != normalize_title(right)
        and title_core(left) == title_core(right)
    )


RESUMPTION_RE = re.compile(
    r"Question again proposed|"
    r"Debate resumed|"
    r"Resumption of Debate",
    re.I,
)


def classify_second_reading_event(text):
    if not text:
        return None
    if re.search(r"Order for Second Reading read", text, re.I):
        return "opening"
    if RESUMPTION_RE.search(text):
        return "resumed"
    if re.search(r"I (?:beg to |now )?move", text, re.I) and re.search(
        r"be now read a Second time", text, re.I
    ):
        return "opening"
    if re.search(r"Bill (?:accordingly )?read a Second time", text, re.I):
        return "completion"
    return None


def is_second_reading_text(text):
    return classify_second_reading_event(text) is not None


FIRST_READING_RE = re.compile(
    r"(?:^|\n)\s*([A-Z][A-Z0-9 ,.'()/\-&]{4,140} BILL)\s+"
    r'"((?:to |for )[^"]{8,1200})"\s*[,.]?\s*'
    r"(?:recommendation of President signified\s*;?\s*)?"
    r"presented by\s+(.+?);\s*read the First time",
    re.S,
)

KIND_LABEL = {
    "new_act": "New Act",
    "amendment": "Amendment",
    "miscellaneous_amendment": "Miscellaneous amendment",
    "repeal": "Repeal / abolition",
    "supply": "Supply",
}

MODERN_SECTION_TYPE = {
    "BI": "first_reading",
    "OA": "oral_answers",
    "WA": "written_answers",
    "WANA": "written_answers_not_orally_asked",
    "OS": "other_speech",
    "WS": "written_statement",
    "BP": "bill_proceeding",
}


