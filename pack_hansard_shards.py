#!/usr/bin/env python3
"""Shard, compress, and checksum the Hansard JSON corpus."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import tarfile
from datetime import date

from hansard_report import sitting_date_from_filename, sitting_filenames

JSON_DIR = "./data/hansard/json"
SHARD_DIR = "./data/hansard/shards"
MANIFEST_NAME = "MANIFEST.json"
CHECKSUM_NAME = "SHA256SUMS"
# ~110 MB uncompressed packs to ~35–45 MB gzip, under GitHub's 100 MB cap.
TARGET_UNCOMPRESSED = 110 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sitting_files(json_dir):
    files = []
    for name in sitting_filenames(json_dir):
        path = os.path.join(json_dir, name)
        sitting = sitting_date_from_filename(name)
        files.append(
            {
                "name": name,
                "path": path,
                "year": sitting.year,
                "bytes": os.path.getsize(path),
                "date": sitting.isoformat(),
            }
        )
    if not files:
        raise SystemExit(f"No JSON sittings in {json_dir}")
    return files


def plan_shards(files):
    shards = []
    current = []
    current_bytes = 0
    for item in files:
        would = current_bytes + item["bytes"]
        if current and would > TARGET_UNCOMPRESSED:
            shards.append(current)
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += item["bytes"]
    if current:
        shards.append(current)
    return shards


def shard_filename(members):
    first = members[0]["date"]
    last = members[-1]["date"]
    return f"hansard-json-{first}-to-{last}.tar.gz"


def write_shard(members, dest_path):
    with open(dest_path, "wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0
        ) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                for item in members:
                    info = tar.gettarinfo(item["path"], arcname=item["name"])
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mode = 0o644
                    with open(item["path"], "rb") as handle:
                        tar.addfile(info, handle)


def write_checksums(shards_meta, shard_dir):
    lines = []
    for shard in shards_meta:
        lines.append(f"{shard['sha256']}  {shard['file']}\n")
    path = os.path.join(shard_dir, CHECKSUM_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return path


def _write_release(json_dir, dest_dir):
    files = sitting_files(json_dir)
    groups = plan_shards(files)
    os.makedirs(dest_dir, exist_ok=True)
    shards_meta = []
    for index, members in enumerate(groups, 1):
        filename = shard_filename(members)
        dest = os.path.join(dest_dir, filename)
        print(
            f"[{index}/{len(groups)}] packing {members[0]['date']} → "
            f"{members[-1]['date']} ({len(members)} files, "
            f"{sum(m['bytes'] for m in members) / 1e6:.1f} MB uncompressed)"
        )
        write_shard(members, dest)
        digest = sha256_file(dest)
        compressed = os.path.getsize(dest)
        print(f"    {filename}  {compressed / 1e6:.1f} MB  sha256={digest}")
        if compressed >= 100 * 1024 * 1024:
            raise SystemExit(f"{filename} is {compressed} bytes (>= 100 MB)")
        member_names = [item["name"] for item in members]
        shards_meta.append(
            {
                "file": filename,
                "sha256": digest,
                "from": members[0]["date"],
                "to": members[-1]["date"],
                "years": sorted({item["year"] for item in members}),
                "files": len(members),
                "members": member_names,
                "uncompressed_bytes": sum(item["bytes"] for item in members),
                "compressed_bytes": compressed,
            }
        )

    manifest = {
        "generated": date.today().isoformat(),
        "algorithm": "sha256",
        "source": json_dir,
        "sittings": len(files),
        "from": files[0]["date"],
        "to": files[-1]["date"],
        "uncompressed_bytes": sum(item["bytes"] for item in files),
        "compressed_bytes": sum(item["compressed_bytes"] for item in shards_meta),
        "shards": shards_meta,
    }
    manifest_path = os.path.join(dest_dir, MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    checksum_path = write_checksums(shards_meta, dest_dir)
    print(f"wrote {manifest_path}")
    print(f"wrote {checksum_path}")
    return manifest


def pack(json_dir, shard_dir):
    parent = os.path.dirname(os.path.abspath(shard_dir)) or "."
    os.makedirs(parent, exist_ok=True)
    staging = shard_dir.rstrip("/") + ".staging"
    backup = shard_dir.rstrip("/") + ".bak"
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)
    try:
        manifest = _write_release(json_dir, staging)
        verify(staging)
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        if os.path.isdir(shard_dir):
            os.rename(shard_dir, backup)
        os.rename(staging, shard_dir)
        if os.path.isdir(backup):
            shutil.rmtree(backup)
    except BaseException:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        if not os.path.isdir(shard_dir) and os.path.isdir(backup):
            os.rename(backup, shard_dir)
        raise
    print(
        f"Done. {manifest['sittings']} sittings in {len(manifest['shards'])} shards, "
        f"{manifest['compressed_bytes'] / 1e6:.1f} MB compressed"
    )
    return manifest


def load_checksums(shard_dir):
    checksum_path = os.path.join(shard_dir, CHECKSUM_NAME)
    if not os.path.isfile(checksum_path):
        raise SystemExit(f"No {CHECKSUM_NAME} in {shard_dir}")
    expected = {}
    with open(checksum_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, name = line.split(None, 1)
            expected[name.lstrip("*")] = digest.lower()
    if not expected:
        raise SystemExit(f"{CHECKSUM_NAME} is empty")
    return expected


def load_manifest(shard_dir):
    manifest_path = os.path.join(shard_dir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        raise SystemExit(f"No {MANIFEST_NAME} in {shard_dir}")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise SystemExit("manifest has no shards")
    return manifest


def tar_member_names(path):
    with tarfile.open(path, "r:gz") as tar:
        return [member.name for member in tar.getmembers() if member.isfile()]


def verify(shard_dir):
    expected = load_checksums(shard_dir)
    manifest = load_manifest(shard_dir)
    manifest_map = {}
    for shard in manifest["shards"]:
        name = shard["file"]
        digest = str(shard["sha256"]).lower()
        if name in manifest_map:
            raise SystemExit(f"duplicate shard in manifest: {name}")
        manifest_map[name] = shard
        listed = expected.get(name)
        if listed is None:
            raise SystemExit(f"{name} is in the manifest but not {CHECKSUM_NAME}")
        if listed != digest:
            raise SystemExit(f"{name} hash mismatch between manifest and {CHECKSUM_NAME}")
    if set(expected) != set(manifest_map):
        raise SystemExit(f"{CHECKSUM_NAME} and {MANIFEST_NAME} list different archives")

    failed = []
    missing = []
    for name, digest in expected.items():
        path = os.path.join(shard_dir, name)
        if not os.path.isfile(path):
            print(f"MISSING  {name}")
            missing.append(name)
            continue
        actual = sha256_file(path)
        if actual.lower() != digest:
            print(f"FAILED   {name}")
            print(f"  expected {digest}")
            print(f"  actual   {actual}")
            failed.append(name)
            continue
        members = tar_member_names(path)
        declared = manifest_map[name].get("members")
        if declared is not None and members != declared:
            print(f"MEMBERS  {name}: archive members do not match manifest")
            failed.append(name)
            continue
        declared_count = manifest_map[name].get("files")
        if declared_count is not None and len(members) != declared_count:
            print(f"COUNT    {name}: {len(members)} members, manifest files={declared_count}")
            failed.append(name)
            continue
        print(f"OK       {name}")

    extras = [
        name
        for name in os.listdir(shard_dir)
        if name.endswith(".tar.gz") and name not in expected
    ]
    for name in extras:
        print(f"EXTRA    {name}")
    if failed or missing or extras:
        raise SystemExit(
            f"Integrity check failed ({len(failed)} mismatch, "
            f"{len(missing)} missing, {len(extras)} unexpected)"
        )
    print(f"Verified {len(expected)} shards.")


def unpack(shard_dir, dest_dir, check=True):
    if check:
        verify(shard_dir)
    os.makedirs(dest_dir, exist_ok=True)
    expected = load_checksums(shard_dir)
    written = 0
    for name in expected:
        path = os.path.join(shard_dir, name)
        print(f"extracting {name}")
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(dest_dir)
            written += len(tar.getmembers())
    print(f"Unpacked {written} files into {dest_dir}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("pack", "verify", "unpack"),
        help="pack JSON into shards, verify checksums, or unpack",
    )
    parser.add_argument("--json-dir", default=JSON_DIR)
    parser.add_argument("--shard-dir", default=SHARD_DIR)
    parser.add_argument("--dest", default=JSON_DIR, help="unpack destination")
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="unpack without checking SHA-256 first",
    )
    args = parser.parse_args()
    if args.command == "pack":
        pack(args.json_dir, args.shard_dir)
    elif args.command == "verify":
        verify(args.shard_dir)
    else:
        unpack(args.shard_dir, args.dest, check=not args.skip_verify)


if __name__ == "__main__":
    main()
