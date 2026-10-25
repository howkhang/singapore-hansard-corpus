import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import parl_snapshot
from hansard_report import sha256_bytes
from tests.test_parl_monitor import report


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.root = self.repo / "monitor"
        self.live = self.repo / "data/hansard"
        (self.live / "json").mkdir(parents=True)
        (self.root / "reports").mkdir(parents=True)
        self.scripts = Path(parl_snapshot.__file__).resolve().parent
        shutil.copytree(self.scripts / "docs", self.repo / "docs")
        self.old = self.live / "json/2026-09-09.json"
        self.old.write_text(json.dumps(report("2026-09-09")))
        self.new = self.root / "reports/2026-09-10.json"
        self.new.write_text(json.dumps(report()))
        self.sidecar = self.new.with_suffix(".meta.json")
        self.sidecar.write_text(json.dumps({"sitting_date": "2026-09-10",
            "captured_at": "2026-09-20T00:00:00Z", "http_status": 200,
            "raw_sha256": sha256_bytes(self.new.read_bytes()), "bytes": self.new.stat().st_size}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_build_publishes_complete_snapshot_and_rerun_is_noop(self):
        def build(_, workspace):
            with contextlib.redirect_stderr(io.StringIO()):
                parl_snapshot.run_build(self.scripts, workspace)
        result = parl_snapshot.refresh_snapshot(self.repo, self.root, builder=build)
        self.assertEqual(result["sittings"], 2)
        self.assertEqual(result["added_dates"], ["2026-09-10"])
        self.assertEqual((self.live / "json" / self.new.name).read_bytes(), self.new.read_bytes())
        self.assertEqual((self.live / "json" / self.sidecar.name).read_bytes(), self.sidecar.read_bytes())
        manifest = self.live / "export/hansard-normalized-v1/EXPORT_MANIFEST.json"
        self.assertEqual(sha256_bytes(manifest.read_bytes()), result["manifest_sha256"])
        normalized = json.loads((self.live / "normalized/v1/reports/2026-09-10.json").read_bytes())
        self.assertEqual(normalized["capture"]["captured_at"], "2026-09-20T00:00:00Z")
        repeat = parl_snapshot.refresh_snapshot(self.repo, self.root, builder=lambda *_: self.fail("rebuilt"))
        self.assertFalse(repeat["changed"])
        self.assertEqual(repeat["manifest_sha256"], result["manifest_sha256"])

    def test_invalid_capture_blocks_publication(self):
        self.sidecar.write_text("{}")
        with self.assertRaises(ValueError):
            parl_snapshot.refresh_snapshot(self.repo, self.root)
        self.assertFalse((self.live / "json" / self.new.name).exists())
        self.assertTrue(self.old.exists())

    def test_build_failure_keeps_previous_release(self):
        original = self.old.read_bytes()
        def fail(_, workspace):
            (workspace / "data/hansard/json/2026-09-09.json").write_text("changed candidate")
            raise ValueError("fixture validation failure")
        with self.assertRaisesRegex(ValueError, "validation failure"):
            parl_snapshot.refresh_snapshot(self.repo, self.root, builder=fail)
        self.assertEqual(self.old.read_bytes(), original)
        self.assertFalse((self.live / "json" / self.new.name).exists())

    def test_swap_failure_restores_previous_release(self):
        original = self.old.read_bytes()
        replace = parl_snapshot.os.replace
        def fail_swap(source, target):
            if Path(source).resolve() == (self.root / "snapshot.build/data/hansard").resolve():
                raise OSError("fixture rename failure")
            return replace(source, target)
        def build(_, workspace):
            export = workspace / "data/hansard/export/hansard-normalized-v1"
            export.mkdir(parents=True)
            (export / "EXPORT_MANIFEST.json").write_text(json.dumps({"complete": True,
                "sittings": 2, "inventory": [{"sitting_date": d} for d in ("2026-09-09", "2026-09-10")]}))
        with patch.object(parl_snapshot.os, "replace", side_effect=fail_swap), self.assertRaises(OSError):
            parl_snapshot.refresh_snapshot(self.repo, self.root, builder=build)
        self.assertEqual(self.old.read_bytes(), original)
        self.assertFalse((self.live / "json" / self.new.name).exists())

    def test_conflicting_report_is_not_overwritten(self):
        target = self.live / "json" / self.new.name
        target.write_text("existing distinct bytes")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            parl_snapshot.refresh_snapshot(self.repo, self.root)
        self.assertEqual(target.read_text(), "existing distinct bytes")


if __name__ == "__main__":
    unittest.main()
