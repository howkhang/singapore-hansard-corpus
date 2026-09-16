#!/usr/bin/env python3
"""Shard, compress, and checksum the Hansard JSON corpus."""

import argparse
import hashlib
import json
import os
import tarfile
from datetime import date

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
    for name in sorted(os.listdir(json_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(json_dir, name)
        year = int(name[:4])
        files.append(
            {
                "name": name,
                "path": path,
                "year": year,
                "bytes": os.path.getsize(path),
                "date": name[: -len(".json")],
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
    with tarfile.open(dest_path, "w:gz", compresslevel=6) as tar:
        for item in members:
            tar.add(item["path"], arcname=item["name"])


def write_checksums(shards_meta, shard_dir):
    lines = []
    for shard in shards_meta:
        lines.append(f"{shard['sha256']}  {shard['file']}\n")
    path = os.path.join(shard_dir, CHECKSUM_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return path


def pack(json_dir, shard_dir):
    files = sitting_files(json_dir)
    groups = plan_shards(files)
    os.makedirs(shard_dir, exist_ok=True)
    for name in os.listdir(shard_dir):
        if name.endswith(".tar.gz") or name in {MANIFEST_NAME, CHECKSUM_NAME}:
            os.remove(os.path.join(shard_dir, name))

    shards_meta = []
    for index, members in enumerate(groups, 1):
        filename = shard_filename(members)
        dest = os.path.join(shard_dir, filename)
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
        shards_meta.append(
            {
                "file": filename,
                "sha256": digest,
                "from": members[0]["date"],
                "to": members[-1]["date"],
                "years": sorted({item["year"] for item in members}),
                "files": len(members),
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
    manifest_path = os.path.join(shard_dir, MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    checksum_path = write_checksums(shards_meta, shard_dir)
    print(f"wrote {manifest_path}")
    print(f"wrote {checksum_path}")
    print(
        f"Done. {len(files)} sittings in {len(shards_meta)} shards, "
        f"{manifest['compressed_bytes'] / 1e6:.1f} MB compressed"
    )
    return manifest


def load_expected(shard_dir):
    checksum_path = os.path.join(shard_dir, CHECKSUM_NAME)
    manifest_path = os.path.join(shard_dir, MANIFEST_NAME)
    expected = {}
    if os.path.isfile(checksum_path):
        with open(checksum_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                digest, name = line.split(None, 1)
                expected[name.lstrip("*")] = digest.lower()
    elif os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        for shard in manifest["shards"]:
            expected[shard["file"]] = shard["sha256"].lower()
    else:
        raise SystemExit(f"No {CHECKSUM_NAME} or {MANIFEST_NAME} in {shard_dir}")
    return expected


def verify(shard_dir):
    expected = load_expected(shard_dir)
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
        else:
            print(f"OK       {name}")
    extras = [
        name
        for name in os.listdir(shard_dir)
        if name.endswith(".tar.gz") and name not in expected
    ]
    for name in extras:
        print(f"EXTRA    {name}")
    if failed or missing:
        raise SystemExit(
            f"Integrity check failed ({len(failed)} mismatch, {len(missing)} missing)"
        )
    print(f"Verified {len(expected)} shards.")


def unpack(shard_dir, dest_dir, check=True):
    if check:
        verify(shard_dir)
    os.makedirs(dest_dir, exist_ok=True)
    expected = load_expected(shard_dir)
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
        verify(args.shard_dir)
    elif args.command == "verify":
        verify(args.shard_dir)
    else:
        unpack(args.shard_dir, args.dest, check=not args.skip_verify)


if __name__ == "__main__":
    main()
