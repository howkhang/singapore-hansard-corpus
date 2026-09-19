"""Exact section retrieval from a normalized report."""

from __future__ import annotations

import json
import os

from hansard_norm.check import reconstruct_source_fragment
from hansard_report import sha256_text


class RetrievalError(ValueError):
    pass


def load_report(normalized_dir, sitting_date):
    path = os.path.join(normalized_dir, "reports", f"{sitting_date}.json")
    if not os.path.isfile(path):
        raise RetrievalError(f"missing normalized report {path}")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def retrieve_section(normalized_dir, section_id, source_dir=None):
    if not section_id.startswith("sitting:"):
        raise RetrievalError(f"unsupported section id {section_id!r}")
    sitting = section_id.split(":")[1]
    report = load_report(normalized_dir, sitting)
    if report.get("schema_version") != "hansard-normalized-v1":
        raise RetrievalError(
            f"unsupported schema_version {report.get('schema_version')!r}"
        )
    match = next(
        (section for section in report.get("sections") or [] if section.get("section_id") == section_id),
        None,
    )
    if match is None:
        raise RetrievalError(f"section not found {section_id}")
    locator = match.get("locator") or {}
    text = match.get("text") or ""
    if sha256_text(text) != locator.get("normalized_text_sha256"):
        raise RetrievalError("normalized_text_sha256 mismatch")
    if source_dir:
        source_path = os.path.join(source_dir, f"{sitting}.json")
        with open(source_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        try:
            fragment = reconstruct_source_fragment(payload, locator)
        except Exception as exc:
            raise RetrievalError(f"source reconstruct failed: {exc}") from exc
        if sha256_text(fragment) != locator.get("source_fragment_sha256"):
            raise RetrievalError("source_fragment_sha256 mismatch")
    return match
