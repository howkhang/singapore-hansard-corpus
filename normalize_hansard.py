#!/usr/bin/env python3
"""Normalize, validate, retrieve, and export Hansard sittings."""

from __future__ import annotations

import argparse
import json
import os
import sys

from hansard_norm import PARSER_VERSION, SCHEMA_VERSION
from hansard_norm.check import validate_snapshot
from hansard_norm.report import canonical_dumps
from hansard_norm.retrieve import RetrievalError, retrieve_section
from hansard_norm.store import normalize_corpus
from hansard_report import atomic_write_bytes, sha256_bytes

JSON_DIR = "./data/hansard/json"
NORM_DIR = "./data/hansard/normalized/v1"
EXPORT_DIR = "./data/hansard/export/hansard-normalized-v1"


def cmd_normalize(args):
    dates = args.dates or None
    if args.all:
        dates = None
    elif not dates:
        raise SystemExit("pass --all or --dates YYYY-MM-DD ...")
    return normalize_corpus(args.json_dir, args.out_dir, dates=dates)


def cmd_validate(args):
    errors, snapshot, complete = validate_snapshot(
        args.out_dir, args.json_dir, require_complete=not args.allow_partial
    )
    if errors:
        for item in errors[:40]:
            print("ERROR", item)
        if len(errors) > 40:
            print(f"... {len(errors) - 40} more")
        raise SystemExit(1)
    print(
        f"OK {snapshot.get('sittings')} normalized reports "
        f"schema={snapshot.get('schema_version')} "
        f"parser={snapshot.get('parser_version')} complete={complete}"
    )


def cmd_failures(args):
    path = os.path.join(args.out_dir, "FAILURES.jsonl")
    if not os.path.isfile(path):
        print("no FAILURES.jsonl")
        return
    with open(path, encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    if not lines:
        print("no failures")
        return
    for line in lines:
        print(line.rstrip())
    raise SystemExit(1)


def cmd_retrieve(args):
    try:
        section = retrieve_section(args.out_dir, args.section_id, source_dir=args.json_dir)
    except RetrievalError as exc:
        raise SystemExit(str(exc)) from exc
    json.dump(section, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _sha256_file(path):
    with open(path, "rb") as handle:
        return sha256_bytes(handle.read())


def cmd_export(args):
    errors, snapshot, complete = validate_snapshot(
        args.out_dir, args.json_dir, require_complete=True
    )
    if errors or not complete or snapshot.get("failures"):
        raise SystemExit("export refused: snapshot is incomplete or invalid")
    required = {
        "debate_index": args.index,
        "contract": args.contract,
        "handoff": args.handoff,
    }
    artifacts = {}
    for key, path in required.items():
        if not os.path.isfile(path):
            raise SystemExit(f"export refused: missing {key} at {path}")
        artifacts[key] = {"path": path, "sha256": _sha256_file(path)}
    if os.path.isfile(args.index):
        with open(args.index, encoding="utf-8") as handle:
            index = json.load(handle)
        artifacts["debate_index"]["parser_version"] = index.get("parser_version")
    conformance = {}
    if os.path.isdir(args.conformance):
        for name in sorted(os.listdir(args.conformance)):
            path = os.path.join(args.conformance, name)
            if os.path.isfile(path):
                conformance[name] = _sha256_file(path)
    artifacts["conformance"] = conformance
    snapshot_path = os.path.join(args.out_dir, "SNAPSHOT.json")
    artifacts["snapshot"] = {"path": snapshot_path, "sha256": _sha256_file(snapshot_path)}
    os.makedirs(args.export_dir, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "complete": True,
        "git_head": None,
        "note": "working tree is uncommitted; git HEAD does not identify this export",
        "normalized_root": args.out_dir,
        "source_root": args.json_dir,
        "sittings": snapshot.get("sittings"),
        "inventory": snapshot.get("inventory"),
        "artifacts": artifacts,
        "hashing": "SHA-256 of file bytes. Report files are canonical UTF-8 JSON (sort_keys, indent=2).",
    }
    out = os.path.join(args.export_dir, "EXPORT_MANIFEST.json")
    atomic_write_bytes(out, canonical_dumps(manifest).encode("utf-8"))
    print(f"wrote {out} sittings={manifest['sittings']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-dir", default=JSON_DIR)
    parser.add_argument("--out-dir", default=NORM_DIR)
    parser.add_argument("--export-dir", default=EXPORT_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    n = sub.add_parser("normalize")
    n.add_argument("--all", action="store_true")
    n.add_argument("--dates", nargs="*")
    v = sub.add_parser("validate")
    v.add_argument("--allow-partial", action="store_true")
    sub.add_parser("failures")
    r = sub.add_parser("retrieve")
    r.add_argument("section_id")
    e = sub.add_parser("export")
    e.add_argument("--index", default="./data/hansard/second_readings_index.json")
    e.add_argument("--contract", default="./docs/normalized-hansard-v1.md")
    e.add_argument("--handoff", default="./docs/CONSUMER_HANDOFF.md")
    e.add_argument(
        "--conformance",
        default="./data/hansard/export/hansard-normalized-v1/conformance",
    )
    args = parser.parse_args()
    if args.command == "normalize":
        cmd_normalize(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "failures":
        cmd_failures(args)
    elif args.command == "retrieve":
        cmd_retrieve(args)
    else:
        cmd_export(args)


if __name__ == "__main__":
    main()
