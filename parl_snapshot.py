"""Build a complete local release from validated monitor captures."""

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from hansard_report import load_validated_report, sha256_bytes, sitting_filenames
from hansard_report import sitting_date_from_filename, atomic_write_bytes


def run_build(repo, workspace):
    commands = [
        ["normalize_hansard.py", "normalize", "--all"],
        ["build_second_readings_index.py"],
        ["validate.py"],
        ["normalize_hansard.py", "validate"],
        ["pack_hansard_shards.py", "pack"],
        ["normalize_hansard.py", "export"],
    ]
    with (workspace / "build.log").open("w") as log:
        for script, *args in commands:
            print("snapshot: " + " ".join([script, *args]), file=sys.stderr, flush=True)
            subprocess.run([sys.executable, "-u", str(repo / script), *args], cwd=workspace,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    export = workspace / "data/hansard/export/hansard-normalized-v1"
    types, statuses, formats = Counter(), Counter(), Counter()
    for path in (workspace / "data/hansard/normalized/v1/reports").glob("*.json"):
        report = json.loads(path.read_bytes())
        formats[report["source"]["format"]] += 1
        for section in report["sections"]:
            types[section["proceeding_type"]] += 1
            statuses[section["proceeding_status"]] += 1
    coverage = {"reports": sum(formats.values()), **formats,
                "sections_by_type": dict(types.most_common()),
                "sections_by_status": dict(statuses),
                "note": "Unresolved classification keeps full text in the section. Attachment PDFs are listed without bytes."}
    atomic_write_bytes(str(export / "coverage.json"), (json.dumps(coverage, indent=2) + "\n").encode())


def refresh_snapshot(repo, root, builder=run_build):
    """Caller holds the monitor lock; existing source files are never replaced."""
    repo, root = Path(repo).resolve(), Path(root).resolve()
    live = repo / "data/hansard"
    workspace, previous = root / "snapshot.build", root / "snapshot.previous"
    # Recover a process interruption between the two directory renames.
    if previous.exists() and not live.exists():
        os.replace(previous, live)
    if previous.exists():
        shutil.rmtree(previous)
    corpus = live / "json"
    additions = []
    for name in sitting_filenames(str(root / "reports")):
        source = root / "reports" / name
        raw = source.read_bytes()
        load_validated_report(str(source), sitting_date_from_filename(name))
        sidecar = source.with_suffix(".meta.json")
        meta = json.loads(sidecar.read_bytes())
        if (meta.get("raw_sha256") != sha256_bytes(raw) or meta.get("sitting_date") != name[:10]
                or meta.get("bytes") != len(raw) or meta.get("http_status") != 200
                or not meta.get("captured_at")):
            raise ValueError(f"invalid capture sidecar: {sidecar}")
        target = corpus / name
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError(f"capture conflicts with existing sitting: {name}")
            continue
        additions.append((source, sidecar))
    manifest_path = live / "export/hansard-normalized-v1/EXPORT_MANIFEST.json"
    old_hash = sha256_bytes(manifest_path.read_bytes()) if manifest_path.exists() else None
    if not additions:
        return {"status": "success", "added_dates": [], "manifest_sha256": old_hash, "changed": False}
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    staged = workspace / "data/hansard"
    shutil.copytree(live, staged)
    shutil.copytree(repo / "docs", workspace / "docs")
    for source, sidecar in additions:
        shutil.copy2(source, staged / "json" / source.name)
        shutil.copy2(sidecar, staged / "json" / sidecar.name)
    builder(repo, workspace)
    candidate = json.loads((staged / "export/hansard-normalized-v1/EXPORT_MANIFEST.json").read_bytes())
    expected = {n[:10] for n in sitting_filenames(str(staged / "json"))}
    if not candidate.get("complete") or {r["sitting_date"] for r in candidate["inventory"]} != expected:
        raise ValueError("built export does not cover the complete source inventory")
    # Keep the previous release until the fully checked candidate takes its place.
    os.replace(live, previous)
    try:
        os.replace(staged, live)
    except BaseException:
        os.replace(previous, live)
        raise
    result = {"status": "success", "changed": True,
              "added_dates": [p.name[:10] for p, _ in additions],
              "sittings": candidate["sittings"], "previous_manifest_sha256": old_hash,
              "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
              "build_log": str(workspace / "build.log")}
    # The old release is retained until the next refresh for inspection/recovery.
    return result
