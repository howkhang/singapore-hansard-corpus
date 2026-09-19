"""Validate Singapore Hansard JSON payloads in both SPRS formats."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime
from typing import Mapping, Tuple

ERROR_KEYS = ("error", "errorCode")
SITTING_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
SIT_DATE_META_RE = re.compile(
    r"<Meta name=['\"]Sit_Date['\"] content=['\"]([^'\"]+)['\"]",
    re.I,
)
SITTING_DATE_LABEL_RE = re.compile(
    r"Sitting Date:</(?:font|span)>\s*(?:</p>)?\s*(?:</td>)?\s*"
    r".{0,400}?(?:<font[^>]*>|<span[^>]*>)\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}-\d{4})",
    re.I | re.S,
)
LEGACY_STRUCTURE_RE = re.compile(
    r"Sit_Date|"
    r"Sitting Date|"
    r"OFFICIAL REPORT|"
    r"PARLIAMENTARY DEBATES|"
    r"LEGISLATIVE ASSEMBLY DEBATES",
    re.I,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def dumps_json_object(obj) -> str:
    """Canonical JSON for json_object locators.

    UTF-8 code units of this string are hashed. Separators are the JSON
    defaults: comma-space and colon-space. No indent, no trailing newline.
    """
    import json

    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(", ", ": "),
    )


def atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _parse_date(value: str) -> date:
    text = value.strip()
    compact = re.match(r"^(\d{4})(\d{2})-(\d{2})$", text)
    if compact:
        text = f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised sitting date {value!r}")


def sitting_date_from_payload(data: Mapping) -> date:
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        raw = metadata.get("sittingDate")
        if raw:
            return _parse_date(str(raw))
    html = data.get("htmlFullContent") or ""
    if isinstance(html, str) and html:
        match = SIT_DATE_META_RE.search(html)
        if match:
            try:
                return _parse_date(match.group(1))
            except ValueError:
                pass
        match = SITTING_DATE_LABEL_RE.search(html)
        if match:
            return _parse_date(match.group(1))
    raise ValueError("payload has no sitting date")


def is_sitting_filename(name: str) -> bool:
    return bool(SITTING_FILENAME_RE.fullmatch(name or ""))


def sitting_date_from_filename(name: str) -> date:
    match = SITTING_FILENAME_RE.fullmatch(name or "")
    if not match:
        raise ValueError(f"not a sitting filename: {name!r}")
    return date.fromisoformat(match.group(1))


def sitting_filenames(directory: str):
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory) if is_sitting_filename(name))


def report_format(data: Mapping) -> str:
    if isinstance(data.get("takesSectionVOList"), list) and isinstance(
        data.get("metadata"), dict
    ):
        return "modern"
    html = data.get("htmlFullContent")
    if not isinstance(html, str) or len(html) < 200:
        raise ValueError("unrecognised Hansard payload")
    if not re.search(r"<html\b", html, re.I):
        raise ValueError("legacy payload is not HTML")
    if not LEGACY_STRUCTURE_RE.search(html):
        raise ValueError("legacy HTML lacks Hansard report structure")
    return "legacy"


def validate_hansard_payload(data, expected_date: date) -> str:
    if not isinstance(data, dict):
        raise ValueError("Hansard response must be a JSON object")
    for key in ERROR_KEYS:
        if data.get(key) not in (None, "", "null"):
            raise ValueError(f"error payload ({key}={data.get(key)!r})")
    fmt = report_format(data)
    sitting = sitting_date_from_payload(data)
    if sitting != expected_date:
        raise ValueError(
            f"payload date {sitting.isoformat()} does not match "
            f"{expected_date.isoformat()}"
        )
    if fmt == "modern":
        sections = data.get("takesSectionVOList")
        if not isinstance(sections, list) or not sections:
            raise ValueError("modern report has empty takesSectionVOList")
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                raise ValueError(f"section {index} is not an object")
    return fmt


def load_validated_report(path: str, expected_date: date) -> Tuple[dict, str]:
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw.strip():
        raise ValueError("empty file")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    fmt = validate_hansard_payload(data, expected_date)
    return data, fmt
