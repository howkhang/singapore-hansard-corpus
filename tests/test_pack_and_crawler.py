import json
import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

import pack_hansard_shards as packer
from hansard_report import atomic_write_bytes
from parl_crawler import cached_report_is_valid, store_response


class PackTests(unittest.TestCase):
    def test_repack_is_reproducible_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_dir = os.path.join(tmp, "json")
            shard_dir = os.path.join(tmp, "shards")
            os.makedirs(json_dir)
            atomic_write_bytes(
                os.path.join(json_dir, "2020-01-01.json"),
                json.dumps({"metadata": {"sittingDate": "01-01-2020"}, "takesSectionVOList": [{"title": "A"}]}).encode(),
            )
            atomic_write_bytes(
                os.path.join(json_dir, "2021-01-01.json"),
                json.dumps({"metadata": {"sittingDate": "01-01-2021"}, "takesSectionVOList": [{"title": "B"}]}).encode(),
            )
            first = packer.pack(json_dir, shard_dir)
            hashes = [shard["sha256"] for shard in first["shards"]]
            second = packer.pack(json_dir, shard_dir)
            self.assertEqual(hashes, [shard["sha256"] for shard in second["shards"]])
            packer.verify(shard_dir)

    def test_meta_sidecars_are_not_packed_as_sittings(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_dir = os.path.join(tmp, "json")
            shard_dir = os.path.join(tmp, "shards")
            os.makedirs(json_dir)
            atomic_write_bytes(
                os.path.join(json_dir, "2025-01-07.json"),
                json.dumps(
                    {
                        "metadata": {"sittingDate": "07-01-2025"},
                        "takesSectionVOList": [{"title": "A"}],
                    }
                ).encode(),
            )
            atomic_write_bytes(
                os.path.join(json_dir, "2025-01-07.meta.json"),
                json.dumps({"sitting_date": "2025-01-07"}).encode(),
            )
            manifest = packer.pack(json_dir, shard_dir)
            self.assertEqual(manifest["sittings"], 1)
            self.assertEqual(manifest["shards"][0]["files"], 1)
            self.assertEqual(manifest["shards"][0]["members"], ["2025-01-07.json"])

    def test_permission_bits_do_not_change_archive_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_dir = os.path.join(tmp, "json")
            shard_dir = os.path.join(tmp, "shards")
            os.makedirs(json_dir)
            path = os.path.join(json_dir, "2020-01-01.json")
            atomic_write_bytes(
                path,
                json.dumps(
                    {
                        "metadata": {"sittingDate": "01-01-2020"},
                        "takesSectionVOList": [{"title": "A"}],
                    }
                ).encode(),
            )
            os.chmod(path, 0o644)
            first = packer.pack(json_dir, shard_dir)
            os.chmod(path, 0o600)
            second = packer.pack(json_dir, shard_dir)
            self.assertEqual(
                [s["sha256"] for s in first["shards"]],
                [s["sha256"] for s in second["shards"]],
            )

    def test_failed_pack_leaves_previous_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_dir = os.path.join(tmp, "json")
            shard_dir = os.path.join(tmp, "shards")
            os.makedirs(json_dir)
            atomic_write_bytes(os.path.join(json_dir, "2020-01-01.json"), b'{"ok":true}')
            packer.pack(json_dir, shard_dir)
            before = os.listdir(shard_dir)
            original = packer.write_shard

            def boom(*_args, **_kwargs):
                raise RuntimeError("disk full")

            packer.write_shard = boom
            try:
                with self.assertRaises(RuntimeError):
                    packer.pack(json_dir, shard_dir)
            finally:
                packer.write_shard = original
            self.assertTrue(os.path.isdir(shard_dir))
            self.assertEqual(sorted(before), sorted(os.listdir(shard_dir)))

    def test_empty_checksums_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "SHA256SUMS"), "w").close()
            with open(os.path.join(tmp, "MANIFEST.json"), "w", encoding="utf-8") as handle:
                json.dump({"shards": [{"file": "x.tar.gz", "sha256": "abc"}]}, handle)
            with self.assertRaises(SystemExit):
                packer.verify(tmp)


class CrawlerStoreTests(unittest.TestCase):
    def test_error_json_is_not_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2025-01-07.json")
            response = SimpleNamespace(
                status_code=200,
                content=json.dumps({"error": "Unable to process"}).encode(),
            )
            with self.assertRaises(ValueError):
                store_response(path, date(2025, 1, 7), response)
            self.assertFalse(os.path.exists(path))

    def test_invalid_cache_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2025-01-07.json")
            atomic_write_bytes(path, b'{"error": "stale"}')
            self.assertFalse(cached_report_is_valid(path, date(2025, 1, 7)))


if __name__ == "__main__":
    unittest.main()
