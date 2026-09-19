"""Atomic snapshot IO for normalized reports."""

from __future__ import annotations

import json
import os
import shutil

from hansard_norm import PARSER_VERSION, SCHEMA_VERSION
from hansard_norm.report import canonical_dumps, normalize_file
from hansard_report import atomic_write_bytes, sitting_filenames, sha256_bytes


def report_path(out_dir, sitting_date):
    return os.path.join(out_dir, "reports", f"{sitting_date}.json")


def write_report(out_dir, report):
    path = report_path(out_dir, report["sitting_date"])
    payload = canonical_dumps(report).encode("utf-8")
    atomic_write_bytes(path, payload)
    return path, sha256_bytes(payload)


def _inventory_from_reports(reports_dir):
    inventory = []
    if not os.path.isdir(reports_dir):
        return inventory
    for name in sorted(os.listdir(reports_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(reports_dir, name)
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        with open(path, "rb") as handle:
            digest = sha256_bytes(handle.read())
        inventory.append(
            {
                "sitting_date": report["sitting_date"],
                "report_id": report["report_id"],
                "source_sha256": report["source"]["sha256"],
                "normalized_sha256": digest,
                "format": report["source"]["format"],
                "sections": len(report.get("sections") or []),
            }
        )
    return inventory


def normalize_corpus(json_dir, out_dir, dates=None):
    staging = out_dir.rstrip("/") + ".staging"
    backup = out_dir.rstrip("/") + ".bak"
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    reports_staging = os.path.join(staging, "reports")
    os.makedirs(reports_staging, exist_ok=True)
    names = sitting_filenames(json_dir)
    if dates:
        wanted = {d + ".json" if not d.endswith(".json") else d for d in dates}
        selected = [name for name in names if name in wanted]
        missing = wanted - set(names)
        if missing:
            raise SystemExit(f"requested sittings not in inventory: {sorted(missing)[:8]}")
        existing = os.path.join(out_dir, "reports")
        if os.path.isdir(existing):
            for name in os.listdir(existing):
                if name.endswith(".json") and name not in wanted:
                    shutil.copy2(os.path.join(existing, name), os.path.join(reports_staging, name))
    else:
        selected = names
    failures = []
    try:
        for index, name in enumerate(selected, 1):
            path = os.path.join(json_dir, name)
            try:
                report, _raw = normalize_file(path, source_root=json_dir)
                write_report(staging, report)
            except Exception as exc:
                failures.append({"sitting": name, "error": str(exc)})
                print(f"FAIL {name}: {exc}")
            if index % 100 == 0 or index == len(selected):
                print(f"normalized {index}/{len(selected)}")
        inventory = _inventory_from_reports(reports_staging)
        source_dates = {name[: -len(".json")] for name in names}
        complete = not failures and {item["sitting_date"] for item in inventory} == source_dates
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "sittings": len(inventory),
            "complete": complete,
            "failures": failures,
            "inventory": inventory,
        }
        atomic_write_bytes(
            os.path.join(staging, "SNAPSHOT.json"),
            canonical_dumps(snapshot).encode("utf-8"),
        )
        atomic_write_bytes(
            os.path.join(staging, "FAILURES.jsonl"),
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures).encode(
                "utf-8"
            ),
        )
        if failures:
            raise SystemExit(
                f"{len(failures)} sittings failed normalization; "
                f"left {staging} for inspection"
            )
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        if os.path.isdir(out_dir):
            os.rename(out_dir, backup)
        os.rename(staging, out_dir)
        if os.path.isdir(backup):
            shutil.rmtree(backup)
    except BaseException:
        if os.path.isdir(backup) and not os.path.isdir(out_dir):
            os.rename(backup, out_dir)
        raise
    print(f"Wrote {len(inventory)} reports to {out_dir} complete={complete}")
    return snapshot
